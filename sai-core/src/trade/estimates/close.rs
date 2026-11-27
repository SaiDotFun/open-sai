use num_bigint::BigUint;
use serde_json::{Value, json};

use crate::{
    errors::{Code, SaiError},
    keeper::TIMEOUT,
    storage::Session,
    trade::{
        TradePreview, Trading,
        actions::invalid,
        position::{ContractPositionError, Lcd, PositionRoute, Read, Snapshot, SnapshotError},
    },
};

use super::{
    COLLATERAL_ORACLE, FEE_UNAVAILABLE, Price, SCALE, base_fees, decimal, integer, lcd::QuoteLcd,
    liquidation_fee, not_applicable, now, oracle_publication_time, unavailable,
};

const UNKNOWN: &str = "Cannot verify the position and Perp liquidation inputs at one block.";
const GUARDED: &str = "The after-hours guard can change or close this position.";
const UNKNOWN_ORACLE_TIME: &str = "The oracle price publication time is unknown.";

pub(in crate::trade) struct PartialCloseQuote<'a> {
    pub(in crate::trade) amount: &'a BigUint,
    pub(in crate::trade) rechecked: Option<(u128, &'a Value)>,
}

fn unavailable_close(partial: bool) -> Value {
    json!({
        "execution": unavailable(UNKNOWN),
        "fees": unavailable(UNKNOWN),
        "liquidationPrice": if partial { unavailable(UNKNOWN) } else { not_applicable("A full close leaves no position.") },
        "worstCaseFill": {
            "status": "unbounded",
            "reason": "This Keeper intent carries no slippage limit.",
        },
    })
}

impl Trading<'_> {
    pub(super) async fn close_estimates(
        &self,
        preview: &mut TradePreview,
        session: Option<&Session>,
    ) -> Result<Value, SaiError> {
        let result = unavailable_close(false);
        preview.position_owner_verified = session.map(|_| false);
        preview.position_route = None;
        let (Some(endpoint), Some(session), Some(target)) =
            (self.lcd_endpoint, session, &preview.intent.target)
        else {
            return Ok(result);
        };
        let id = serde_json::from_str(&target.trade_id).map_err(|_| SaiError::internal())?;
        let account = session.addr();
        let indexed = self.indexed_position(account, &id, TIMEOUT).await?;
        let lcd = Lcd(endpoint);
        let Ok(route) = self.position_route(&lcd, false).await else {
            return Ok(result);
        };
        let verified = match Self::verify_contract_owner(&lcd, account, &id, route).await {
            Ok(verified) => verified,
            Err(error @ ContractPositionError::Owner) => return Err(error.into()),
            Err(ContractPositionError::Read(_)) => return Ok(result),
        };
        let route = verified.0.clone();
        let (known_owner, result) = self
            .estimate_verified_close(&lcd, account, &id, indexed, verified)
            .await?;
        if known_owner {
            preview.position_owner_verified = Some(true);
            preview.position_route = Some(route);
        }
        Ok(result)
    }

    pub(in crate::trade) async fn estimate_verified_close(
        &self,
        lcd: &Lcd<'_>,
        account: &str,
        id: &Value,
        indexed: Value,
        (route, raw): (PositionRoute, Value),
    ) -> Result<(bool, Value), SaiError> {
        let mut result = unavailable_close(false);
        let quote = async {
            let state = self
                .position_snapshot(lcd, account, id, route, (indexed, Some(raw)), true)
                .await?;
            if let Some(height) = state.verified_height(lcd, id).await? {
                let quote_lcd = QuoteLcd::new(lcd.0, state.route.contract_address.clone());
                Self::quote_close(&quote_lcd, lcd, &state, id, &height, None, &mut result).await;
            } else {
                unknown_oracle_time(&mut result);
            }
            Ok(())
        };
        match quote.await {
            Err(SnapshotError::Owner(message)) => Err(invalid(message)),
            Err(SnapshotError::IndexMismatch) => Ok((false, result)),
            Ok(())
            | Err(
                SnapshotError::CollateralMismatch
                | SnapshotError::PositionChanged
                | SnapshotError::Unavailable,
            ) => {
                // Missing estimate inputs do not invalidate the verified owner and index.
                Ok((true, result))
            }
        }
    }

    pub(in crate::trade) async fn partial_close_estimates(
        lcd: &Lcd<'_>,
        state: &Snapshot,
        id: &Value,
        height: Result<Option<&str>, &SnapshotError>,
        partial: PartialCloseQuote<'_>,
    ) -> Value {
        let mut result = unavailable_close(true);
        if !state.raw["long"].is_boolean() {
            return result;
        }
        if let Ok(Some(height)) = height {
            let quote_lcd = QuoteLcd::new(lcd.0, state.route.contract_address.clone());
            Self::quote_close(
                &quote_lcd,
                lcd,
                state,
                id,
                height,
                Some(partial),
                &mut result,
            )
            .await;
        } else if !matches!(height, Err(SnapshotError::PositionChanged)) {
            unknown_oracle_time(&mut result);
        }
        result
    }

    async fn quote_close(
        lcd: &QuoteLcd<'_>,
        position_lcd: &Lcd<'_>,
        state: &Snapshot,
        id: &Value,
        height: &str,
        partial: Option<PartialCloseQuote<'_>>,
        result: &mut Value,
    ) -> Option<()> {
        let raw = &state.raw;
        let long = raw["long"].as_bool()?;
        let trade = raw.get("user")?;
        let height = Some(height);

        let prices;
        let row = if let Some((_, prices)) = partial.as_ref().and_then(|quote| quote.rechecked) {
            prices
        } else {
                        let read = if raw.get("collateral_index")?.as_str()? == "TokenIndex(1)" {
                async {
                    let oracle_addr = position_lcd.smart(&state.route.contract_address, json!({"get_oracle_address": {}}), crate::trade::position::Read::At(height?)).await.ok()?.0;
                    let oracle_str = oracle_addr.as_str()?;
                    let pair = position_lcd.smart(&state.route.contract_address, json!({"get_market": {"index": raw.get("market_index")?}}), crate::trade::position::Read::At(height?)).await.ok()?.0;
                    let base_str = pair.get("base")?.as_str()?;
                    let quote_str = pair.get("quote")?.as_str()?;
                    let parse_token = |s: &str| -> Option<u64> {
                        s.strip_prefix("TokenIndex(")?.strip_suffix(")")?.parse().ok()
                    };
                    let base = parse_token(base_str)?;
                    let quote = parse_token(quote_str)?;
                    let rate_resp = position_lcd.smart(
                        oracle_str,
                        json!({"get_exchange_rate_stamped": {"base": base, "quote": quote}}),
                        crate::trade::position::Read::At(height?)
                    ).await.ok()?;
                    Some(json!({
                        "market": rate_resp.0,
                        "collateral": {"rate": "1000000000000000000"}
                    }))
                }.await.ok_or_else(|| crate::errors::SaiError::internal())
            } else {
                lcd.smart(
                    json!({"get_perp_prices": {
                        "market_index": raw.get("market_index")?,
                        "collateral_index": raw.get("collateral_index")?,
                    }}),
                    height,
                )
                .await
            };
            prices = match read {
                Ok(prices) => prices,
                Err(_) => return unknown_oracle_time(result),
            };
            &prices
        };
        let oracle = &row["market"];
        let Some(Some(published_at)) = oracle_publication_time(oracle) else {
            return unknown_oracle_time(result);
        };
        let config = lcd
            .smart(json!({"market_order_config": {}}), height)
            .await
            .ok()?;
        if config["fill_at_next_oracle_price"].as_bool()? {
            let queued = unavailable("Queued closes settle on a fresh oracle update.");
            for value in result.as_object_mut()?.values_mut() {
                *value = queued.clone();
            }
            return Some(());
        }
        let pair = lcd.market(&raw["market_index"], Read::At(height?)).await?;
        let group = lcd.group(&pair, height).await?;
        let ttl = group["stale_price_expiration_secs"].as_u64()?;
        if ttl > 0
            && i128::from(lcd.block_timestamp(height?).await?) - i128::from(published_at)
                > i128::from(ttl)
        {
            let reason = "Perp rejects the close at the observed stale oracle price.";
            for value in result.as_object_mut()?.values_mut() {
                *value = unavailable(reason);
            }
            result["execution"] = json!({
                "status": "would_be_rejected", "code": "OraclePriceStale", "reason": reason,
            });
            return Some(());
        }

        let threshold = lcd
            .smart(
                json!({"get_liquidation_price": {
                    "trade_id": raw.get("user_trade_index")?,
                    "trader": trade,
                    "include_borrowing_fees": true,
                }}),
                height,
            )
            .await
            .ok()?;
        let observed_at = now();
        let market = Price::new(
            oracle.get("base_per_quote")?.as_str()?,
            "Sai Perp market oracle",
            Some(observed_at.clone()),
        )?;
        let trigger = decimal(&threshold["liquidation_price"])?;
        let collateral = integer(&raw["collateral_amount"])?;
        if (long && market.units <= trigger) || (!long && market.units >= trigger) {
            result["execution"] = json!({
                "status": "would_liquidate",
                "marketPrice": market.output,
                "liquidationPrice": threshold["liquidation_price"],
            });
            result["fees"] = json!({
                "status": "estimate",
                "amountUsdc": liquidation_fee(&collateral),
                "basis": "Gross Perp liquidation fee components.",
                "excludes": ["Borrowing fees", "network fees"],
            });
            result["liquidationPrice"] = not_applicable("Liquidation closes the entire position.");
            return Some(());
        }

        let stored = decimal(&raw["leverage"])?;
        let effective = match partial.as_ref().and_then(|quote| quote.rechecked) {
            Some((effective, _)) => Ok(effective),
            None => state.effective_leverage(position_lcd, id, height?).await,
        };
        let effective = match effective {
            Ok(effective) if partial.is_some() && effective != stored => {
                result["execution"] = unavailable(GUARDED);
                result["fees"] = unavailable(GUARDED);
                result["liquidationPrice"] = unavailable(GUARDED);
                return Some(());
            }
            Ok(leverage) => leverage,
            Err(error) if error.code == Code::PreviewMismatch => return None,
            Err(_) => {
                result["execution"] = unavailable(GUARDED);
                result["fees"] = unavailable(GUARDED);
                result["liquidationPrice"] = unavailable(GUARDED);
                return Some(());
            }
        };
        if partial.is_some() {
            let settlement = unavailable(
                "Partial-close settlement can draw on the remaining collateral. Execution may be rejected if the resulting leverage exceeds the market cap.",
            );
            result["execution"] = settlement.clone();
            result["liquidationPrice"] = settlement;
        } else {
            result["execution"] = json!({"status": "voluntary_close"});
        }
        result["fees"] = unavailable(FEE_UNAVAILABLE);
        let collat_str = if pair["collateralToken"]["symbol"] == "USDC" {
            Some("1000000000000000000")
        } else {
            row["collateral"].as_str()
        };
        let price = Price::new(
            collat_str?,
            COLLATERAL_ORACLE,
            Some(observed_at),
        )?;
        let schedule = lcd.fees(&pair, height).await?;
        let reduced = partial.as_ref().map_or(&collateral, |quote| quote.amount);
        let notional = reduced * effective / SCALE;
        let (_, fees) = base_fees(
            &schedule,
            &price,
            notional,
            false,
            partial.is_some(),
            Some(&collateral),
        )?;
        result["fees"] = fees;
        Some(())
    }
}

fn unknown_oracle_time(result: &mut Value) -> Option<()> {
    for field in result.as_object_mut()?.values_mut() {
        *field = unavailable(UNKNOWN_ORACLE_TIME);
    }
    result["execution"]["code"] = json!("OraclePublicationTimeUnknown");
    Some(())
}
