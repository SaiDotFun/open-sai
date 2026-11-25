use num_bigint::BigUint;
use serde_json::{Value, json};

use crate::errors::SaiError;

use crate::trade::{
    TradePreview, Trading,
    actions::invalid,
    confirmation::{plain_decimal, positive_decimal},
    estimates::{market_rejection, oracle_publication_time},
    position::{Lcd, PositionRoute, Snapshot, SnapshotError, contract_decimal, field, number_text},
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
) -> Value {
    leverage_execution(lcd, state, id, height, target, quoted_effective)
        .await
        .unwrap_or_else(unknown_execution_inputs)
}

async fn leverage_execution(
    lcd: &Lcd<'_>,
    state: &Snapshot,
    id: &Value,
    height: &str,
    target: &str,
    quoted_effective: Option<u128>,
) -> Option<Value> {
    let raw = &state.raw;
    let read = |query| state.smart(lcd, query, height);
    let pair = read(json!({"get_market": {"index": raw.get("market_index")?}}))
        .await
        .ok()?;
    if let Some(reason) = market_rejection(&pair, true) {
        return Some(rejected("MarketClosed", reason));
    }
    let queued = read(json!({"market_order_config": {}})).await.ok()?["fill_at_next_oracle_price"]
        .as_bool()?;
    let group_query = json!({"get_group": {"index": pair.get("group_index")?}});
    let oracle_price = if queued {
        None
    } else {
        let prices = read(json!({"get_perp_prices": {
            "market_index": raw["market_index"], "collateral_index": raw["collateral_index"],
        }}))
        .await
        .ok()?;
        let oracle = &prices["market"];
        if let Some(published) = oracle_publication_time(oracle)? {
            let group = read(group_query.clone()).await.ok()?;
            let ttl = group["stale_price_expiration_secs"].as_u64()?;
            if ttl > 0
                && i128::from(lcd.block_timestamp(height).await.ok()?) - i128::from(published)
                    > i128::from(ttl)
            {
                return Some(rejected("OraclePriceStale", STALE));
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
                return Some(rejected(
                    "LiquidationReached",
                    "The after-hours guard reaches liquidation at the stored leverage.",
                ));
            }
        }
        return Some(all_unavailable(GUARDED));
    }
    if quoted_effective.is_some_and(|quoted| quoted != effective) {
        return Some(all_unavailable(
            "Effective leverage changed while quoting margin.",
        ));
    }

    let group = read(group_query).await.ok()?;
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
        return Some(position_change_estimates(unavailable(OUTSIDE)));
    }
    let schedule = read(json!({"get_fees": {"index": pair.get("fee_index")?}}))
        .await
        .ok()?;
    let open_rate = contract_decimal(schedule.get("open_fee_p")?).ok()?;
    let before: BigUint = number_text(raw.get("collateral_amount")?)
        .ok()?
        .parse()
        .ok()?;
    let notional = &before * effective / SCALE;
    let collateral = &before * effective / leverage;
    if collateral <= notional * open_rate / SCALE {
        return Some(position_change_estimates(unavailable(OUTSIDE)));
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
        return Some(rejected("LiquidationReached", PROSPECTIVE_LIQUIDATION));
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
        let available: BigUint = number_text(funds.get("available_to_trade")?)
            .ok()?
            .parse()
            .ok()?;
        let required = collateral - before;
        if available < required {
            let funding = format!(
                "Perp requires {} USDC of additional margin. The observed available balance is {} USDC.",
                usdc(&required),
                usdc(&available),
            );
            if !queued {
                return Some(rejected("InsufficientDepositBalance", &funding));
            }
            estimates["execution"]["warning"] =
                json!(format!("{funding} Perp checks funding at fill."));
        }
    }
    Some(estimates)
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
        route: Option<PositionRoute>,
        indexed: Value,
        verified_raw: Option<Value>,
    ) -> Result<(), SaiError> {
        let result = if let (Some(endpoint), Some(route)) = (self.lcd_endpoint, route) {
            let lcd = Lcd(endpoint);
            let target = preview.intent.body["requestedLeverage"].as_str();
            let quote = async {
                let snapshot = self
                    .position_snapshot(&lcd, addr, id, route, indexed, verified_raw)
                    .await?;
                let Some(height) = snapshot.verified_height(&lcd, id).await? else {
                    return Ok(None);
                };
                Ok(position_estimates(&lcd, &snapshot, id, &height, target).await)
            };
            match quote.await {
                Ok(result) => result.unwrap_or_else(unknown),
                Err(SnapshotError::Owner(message)) => return Err(invalid(message)),
                Err(SnapshotError::IndexMismatch | SnapshotError::Unavailable) => unknown(),
            }
        } else {
            unknown()
        };
        preview.estimates = Some(result);
        Ok(())
    }
}

async fn position_estimates(
    lcd: &Lcd<'_>,
    snapshot: &Snapshot,
    id: &Value,
    height: &str,
    target: Option<&str>,
) -> Option<Value> {
    let effective = snapshot.effective_leverage(lcd, id, height).await.ok()?;
    let stored = contract_decimal(snapshot.raw.get("leverage")?).ok()?;
    if effective != stored {
        return Some(all_unavailable(
            "The after-hours guard can change or close this position.",
        ));
    }
    let mut result = json!({
        "fees": {"status": "exact", "amountUsdc": "0", "excludes": ["Network fees"]},
        "liquidationPrice": unavailable("Position entry price or projected collateral is unavailable."),
        "worstCaseFill": {"status": "not_applicable", "reason": "This action has no price fill."},
    });
    if let Some(target) = target {
        // An unavailable risk input leaves the fees and fill results intact.
        quote_leverage(lcd, snapshot, height, effective, target, &mut result).await;
    } else {
        result["liquidationPrice"] = json!({
            "status": "unchanged",
            "reason": "This action changes only TP/SL triggers.",
        });
    }
    Some(result)
}

async fn quote_leverage(
    lcd: &Lcd<'_>,
    state: &Snapshot,
    height: &str,
    current: u128,
    target: &str,
    result: &mut Value,
) -> Option<()> {
    let raw = &state.raw;
    let long = raw.get("long")?.as_bool()?;
    let leverage = positive_decimal(target, "Leverage").ok()?;
    let read = |query| state.smart(lcd, query, height);
    let pair = read(json!({"get_market": {"index": raw.get("market_index")?}}))
        .await
        .ok()?;
    let group_query = json!({"get_group": {"index": field(&pair, "group_index").ok()?}});
    let group = read(group_query.clone()).await.ok()?;
    let minimum = contract_decimal(group.get("min_leverage")?).ok()?;
    let maximum = contract_decimal(group.get("max_leverage")?).ok()?;
    let pair_cap = read(json!({"get_pair_custom_max_leverage": {
        "index": state.market.get("marketId")?,
    }}))
    .await
    .ok()?;
    let pair_cap = contract_decimal(&pair_cap).ok()?;
    let maximum = if pair_cap == 0 {
        maximum
    } else {
        maximum.min(pair_cap)
    };
    if leverage == current || leverage > maximum || (leverage < minimum && leverage <= current) {
        result["liquidationPrice"] = unavailable(OUTSIDE);
        return Some(());
    }

    let schedule = read(json!({"get_fees": {"index": pair.get("fee_index")?}}))
        .await
        .ok()?;
    let open_rate = contract_decimal(schedule.get("open_fee_p")?).ok()?;
    let entry = raw.get("open_price")?.as_str()?;
    let price = positive_decimal(entry, "Entry price").ok()?;
    let before: BigUint = number_text(raw.get("collateral_amount")?)
        .ok()?
        .parse()
        .ok()?;
    let notional = &before * current / SCALE;
    let collateral = &before * current / leverage;
    if collateral <= notional * open_rate / SCALE {
        result["liquidationPrice"] = unavailable(OUTSIDE);
        return Some(());
    }

    let threshold = liquidation_price(price, &collateral, leverage, long);
    result["liquidationPrice"] = json!({
        "status": "estimate",
        "price": fixed(&threshold),
        "entry": {"price": entry, "source": "Sai Perp stored entry price", "observedAt": state.observed_at},
        "excludesBorrowingFees": true,
        "assumption": "The target leverage takes effect if Perp's borrowing-inclusive liquidation check passes.",
    });

    let oracle = read(json!({"get_perp_prices": {
        "market_index": raw["market_index"], "collateral_index": raw["collateral_index"],
    }}))
    .await
    .ok()?;
    let oracle = oracle.get("market")?;
    let price = contract_decimal(oracle.get("base_per_quote")?).ok()?;
    if price == 0 {
        return Some(());
    }
    let price = BigUint::from(price);
    if (long && price > threshold) || (!long && price < threshold) {
        return Some(());
    }
    let config = read(json!({"market_order_config": {}})).await.ok()?;
    if config["fill_at_next_oracle_price"].as_bool()? {
        result["execution"] =
            unavailable("Queued leverage changes settle on a fresh oracle update.");
        return Some(());
    }

    let mut published_at = None;
    for key in ["base_last_update_time", "quote_last_update_time"] {
        if let Some(value) = oracle.get(key).filter(|value| !value.is_null()) {
            let value = value.as_u64()?;
            published_at = Some(published_at.map_or(value, |time: u64| time.min(value)));
        }
    }
    let stale = if let Some(published_at) = published_at {
        let group = read(group_query).await.ok()?;
        let ttl = group["stale_price_expiration_secs"].as_u64()?;
        ttl > 0
            && i128::from(lcd.block_timestamp(height).await.ok()?) - i128::from(published_at)
                > i128::from(ttl)
    } else {
        false
    };
    let (code, reason) = if stale {
        (
            "OraclePriceStale",
            "Perp rejects the leverage change at the observed stale oracle price.",
        )
    } else {
        (
            "LiquidationReached",
            "The observed oracle price is past the prospective liquidation threshold.",
        )
    };
    result["execution"] = json!({"status": "would_be_rejected", "code": code, "reason": reason});
    result["liquidationPrice"] = unavailable(reason);
    Some(())
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
