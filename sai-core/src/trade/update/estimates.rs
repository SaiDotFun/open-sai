use num_bigint::BigUint;
use serde_json::{Value, json};

use crate::errors::SaiError;

use crate::trade::{
    LeverageCollateral, TradePreview, Trading,
    actions::invalid,
    confirmation::{plain_decimal, positive_decimal},
    estimates::{market_rejection, oracle_publication_time},
    position::{Lcd, PositionRoute, Snapshot, SnapshotError, contract_decimal, number_text},
};

const SCALE: u128 = 10u128.pow(18);
const OUTSIDE: &str = "The leverage request is outside the market's collateral or leverage limits.";
const STALE: &str = "Perp rejects the leverage change at the observed stale oracle price.";
const GUARDED: &str = "The after-hours guard can change or close this position.";
const PROSPECTIVE_LIQUIDATION: &str =
    "The observed oracle price has reached the prospective liquidation threshold.";

fn unavailable(reason: &str) -> Value {
    json!({"status": "unavailable", "reason": reason})
}

fn all_unavailable(reason: &str) -> Value {
    let unknown = unavailable(reason);
    json!({"fees": unknown, "liquidationPrice": unknown, "worstCaseFill": unknown})
}

pub(super) fn unknown() -> Value {
    all_unavailable("Cannot verify that the Perp after-hours guard is inactive.")
}

pub(in crate::trade) fn unknown_execution_inputs() -> Value {
    all_unavailable("Cannot verify the Perp execution inputs and after-hours guard at one block.")
}

pub(in crate::trade) async fn leverage_execution_estimates(
    lcd: &Lcd<'_>,
    state: &Snapshot,
    id: &Value,
    height: &str,
    target: &str,
    quoted_effective: Option<u128>,
) -> Result<(Value, Option<LeverageCollateral>), SnapshotError> {
    leverage_execution(lcd, state, id, height, target, quoted_effective)
        .await
        .transpose()
        .map(|estimates| estimates.unwrap_or_else(|| (unknown_execution_inputs(), None)))
}

async fn leverage_execution(
    lcd: &Lcd<'_>,
    state: &Snapshot,
    id: &Value,
    height: &str,
    target: &str,
    quoted_effective: Option<u128>,
) -> Option<Result<(Value, Option<LeverageCollateral>), SnapshotError>> {
    let estimated = |value| Some(Ok((value, None)));
    let raw = &state.raw;
    let read = |query| state.smart(lcd, query, height);
    let pair = read(json!({"get_market": {"index": raw.get("market_index")?}}))
        .await
        .ok()?;
    if let Some(reason) = market_rejection(&pair, true) {
        return estimated(rejected("MarketClosed", reason));
    }
    let queued = read(json!({"market_order_config": {}})).await.ok()?["fill_at_next_oracle_price"]
        .as_bool()?;
    let group_query = || {
        pair.get("group_index")
            .map(|index| json!({"get_group": {"index": index}}))
    };
    let oracle_price = if queued {
        None
    } else {
        let oracle = lcd
            .market_oracle_price(&state.route.contract_address, &pair, height)
            .await
            .ok()?;
        if let Some(published) = oracle_publication_time(&oracle)? {
            let group = read(group_query()?).await.ok()?;
            let ttl = group["stale_price_expiration_secs"].as_u64()?;
            if ttl > 0
                && i128::from(lcd.block_timestamp(height).await.ok()?) - i128::from(published)
                    > i128::from(ttl)
            {
                return estimated(rejected("OraclePriceStale", STALE));
            }
        }
        let price = contract_decimal(oracle.get("base_per_quote")?).ok()?;
        if price == 0 {
            return None;
        }
        Some(price)
    };

    let effective = state.effective_leverage(lcd, id, height).await.ok()?;
    let stored = contract_decimal(raw.get("leverage")?).ok()?;
    if effective < stored {
        if let (Some(price), Some(long)) = (oracle_price, raw["long"].as_bool()) {
            let threshold = read(json!({"get_liquidation_price": {
                "trade_id": raw["user_trade_index"],
                "trader": raw["user"],
                "include_borrowing_fees": true,
            }}))
            .await
            .ok()?;
            let threshold = contract_decimal(threshold.get("liquidation_price")?).ok()?;
            if past_liquidation(&price, &threshold, long) {
                return estimated(rejected(
                    "LiquidationReached",
                    "The after-hours guard reaches liquidation at the stored leverage.",
                ));
            }
        }
        return estimated(all_unavailable(GUARDED));
    }
    if quoted_effective.is_some_and(|quoted| quoted != effective) {
        return estimated(all_unavailable(
            "Effective leverage changed while quoting margin.",
        ));
    }

    let group = read(group_query()?).await.ok()?;
    let min = contract_decimal(group.get("min_leverage")?).ok()?;
    let max = contract_decimal(group.get("max_leverage")?).ok()?;
    let cap = read(json!({"get_pair_custom_max_leverage": {
        "index": state.market.get("marketId")?,
    }}))
    .await
    .ok()?;
    let cap = contract_decimal(&cap).ok()?;
    let max = if cap == 0 { max } else { max.min(cap) };
    let leverage = positive_decimal(target, "Leverage").ok()?;
    if leverage == effective || leverage > max || (leverage < min && leverage <= effective) {
        return estimated(position_change_estimates(unavailable(OUTSIDE)));
    }
    let schedule = read(json!({"get_fees": {"index": pair.get("fee_index")?}}))
        .await
        .ok()?;
    let open_rate = contract_decimal(schedule.get("open_fee_p")?).ok()?;
    let before = position_collateral(raw)?;
    let notional = &before * effective / SCALE;
    let collateral = &before * effective / leverage;
    if collateral <= notional * open_rate / SCALE {
        return estimated(position_change_estimates(unavailable(OUTSIDE)));
    }
    let long = raw.get("long")?.as_bool()?;
    let entry = raw.get("open_price")?.as_str()?;
    let price = positive_decimal(entry, "Entry price").ok()?;
    let threshold = liquidation_price(price, &collateral, leverage, long);
    if let Some(price) = oracle_price
        && (past_liquidation(&BigUint::from(price), &threshold, long)
            || lcd
                .leverage_quote_liquidates(
                    &state.route.contract_address,
                    json!({"quote_trade": {
                        "trader": raw["user"],
                        "funds": [],
                        "action": {"update_leverage": {
                            "trade_index": raw["user_trade_index"],
                            "new_leverage": target,
                        }},
                    }}),
                    height,
                )
                .await)
    {
        return estimated(rejected("LiquidationReached", PROSPECTIVE_LIQUIDATION));
    }
    let mut estimates = position_change_estimates(json!({
        "status": "estimate",
        "price": fixed(&threshold),
        "entry": {"price": entry, "source": "Sai Perp stored entry price", "observedAt": state.observed_at},
        "excludesBorrowingFees": true,
        "assumption": "The target leverage takes effect if Perp's borrowing-inclusive liquidation check passes.",
    }));
    if queued {
        estimates["execution"] =
            unavailable("Queued leverage changes settle on a fresh oracle update.");
    }
    if collateral > before {
        let funds = read(json!({"get_exchange_balance": {
            "user": raw["user"], "collateral_index": raw["collateral_index"],
        }}))
        .await
        .ok()?;
        if funds["collateral_index"] != raw["collateral_index"] {
            return Some(Err(SnapshotError::CollateralMismatch));
        }
        let available: BigUint = number_text(funds.get("available_to_trade")?)
            .ok()?
            .parse()
            .ok()?;
        let required = &collateral - &before;
        if available < required {
            let funding = format!(
                "Perp requires {} USDC of additional margin. The observed available balance is {} USDC.",
                usdc(&required),
                usdc(&available),
            );
            if !queued {
                return estimated(rejected("InsufficientDepositBalance", &funding));
            }
            estimates["execution"]["warning"] =
                json!(format!("{funding} Perp checks funding at fill."));
        }
    }
    Some(Ok((
        estimates,
        Some(LeverageCollateral {
            before: before.to_string(),
            after: Some(collateral.to_string()),
        }),
    )))
}

fn position_collateral(raw: &Value) -> Option<BigUint> {
    number_text(raw.get("collateral_amount")?)
        .ok()?
        .parse()
        .ok()
}

fn past_liquidation<T: Ord>(price: &T, threshold: &T, long: bool) -> bool {
    if long {
        price <= threshold
    } else {
        price >= threshold
    }
}

fn position_change_estimates(liquidation: Value) -> Value {
    json!({
        "fees": {"status": "exact", "amountUsdc": "0", "excludes": ["Network fees"]},
        "worstCaseFill": {"status": "not_applicable", "reason": "This action has no price fill."},
        "liquidationPrice": liquidation,
    })
}

fn rejected(code: &str, reason: &str) -> Value {
    let mut estimates = all_unavailable(reason);
    estimates["execution"] = json!({"status": "would_be_rejected", "code": code, "reason": reason});
    estimates
}

fn usdc(units: &BigUint) -> String {
    let whole = units / 1_000_000u32;
    let fraction = units % 1_000_000u32;
    format!("{whole}.{fraction:0>6}")
}

impl Trading<'_> {
    pub(in crate::trade) async fn estimate_position_update(
        &self,
        preview: &mut TradePreview,
        addr: &str,
        id: &Value,
        verified: Option<(PositionRoute, Option<Value>)>,
        indexed: Value,
        bind_collateral: bool,
    ) -> Result<(), SaiError> {
        let target = preview.intent.body["requestedLeverage"].as_str();
        let bind_collateral = bind_collateral && target.is_some();
        let mut before = if bind_collateral {
            verified
                .as_ref()
                .and_then(|(_, raw)| raw.as_ref())
                .and_then(position_collateral)
        } else {
            None
        };
        let unavailable = if target.is_some() {
            unknown_execution_inputs
        } else {
            unknown
        };
        let (result, collateral) =
            if let (Some(endpoint), Some((route, verified_raw))) = (self.lcd_endpoint, verified) {
                let lcd = Lcd(endpoint);
                let quote = async {
                    let snapshot = self
                        .position_snapshot(
                            &lcd,
                            addr,
                            id,
                            route,
                            (indexed, verified_raw),
                            target.is_none(),
                        )
                        .await?;
                    if bind_collateral {
                        before = position_collateral(&snapshot.raw);
                    }
                    let Some(height) = snapshot.verified_height(&lcd, id).await? else {
                        return Ok(None);
                    };
                    if let Some(target) = target {
                        return leverage_execution_estimates(
                            &lcd, &snapshot, id, &height, target, None,
                        )
                        .await
                        .map(Some);
                    }
                    Ok(trigger_estimates(&lcd, &snapshot, id, &height)
                        .await
                        .map(|value| (value, None)))
                };
                match quote.await {
                    Ok(result) => result.unwrap_or_else(|| (unavailable(), None)),
                    Err(SnapshotError::Owner(message)) => return Err(invalid(message)),
                    Err(SnapshotError::IndexMismatch | SnapshotError::PositionChanged) => {
                        before = None;
                        (unavailable(), None)
                    }
                    Err(SnapshotError::CollateralMismatch | SnapshotError::Unavailable) => {
                        (unavailable(), None)
                    }
                }
            } else {
                (unavailable(), None)
            };
        preview.estimates = Some(result);
        preview.leverage_collateral = if bind_collateral {
            collateral.or_else(|| {
                before.map(|amount| LeverageCollateral {
                    before: amount.to_string(),
                    after: None,
                })
            })
        } else {
            None
        };
        Ok(())
    }
}

async fn trigger_estimates(
    lcd: &Lcd<'_>,
    snapshot: &Snapshot,
    id: &Value,
    height: &str,
) -> Option<Value> {
    let effective = snapshot.effective_leverage(lcd, id, height).await.ok()?;
    let stored = contract_decimal(snapshot.raw.get("leverage")?).ok()?;
    if effective != stored {
        return Some(all_unavailable(
            "The after-hours guard can change or close this position.",
        ));
    }
    Some(json!({
        "fees": {"status": "exact", "amountUsdc": "0", "excludes": ["Network fees"]},
        "liquidationPrice": {
            "status": "unchanged",
            "reason": "This action changes only TP/SL triggers.",
        },
        "worstCaseFill": {"status": "not_applicable", "reason": "This action has no price fill."},
    }))
}

fn liquidation_price(price: u128, collateral: &BigUint, leverage: u128, long: bool) -> BigUint {
    let price = BigUint::from(price);
    // Apply the same floors as Perp for the fee components and price distance.
    let fee = (collateral * 5u32 / 100u32) * 2u32;
    let price_per_unit = (&price / collateral) * SCALE / leverage;
    let distance = price_per_unit * (collateral * 90u32 - fee * 100u32) / 100u32;
    if !long {
        price + distance
    } else if price > distance {
        price - distance
    } else {
        BigUint::ZERO
    }
}

fn fixed(units: &BigUint) -> String {
    if *units == BigUint::ZERO {
        return "0".into();
    }
    let digits = units.to_string();
    plain_decimal(digits.trim_end_matches('0'), digits.len() as i64 - 18)
}
