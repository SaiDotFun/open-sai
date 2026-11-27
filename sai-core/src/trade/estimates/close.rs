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
    Price, SCALE, base_fees, decimal, integer, lcd::QuoteLcd, liquidation_fee, not_applicable, now,
    oracle_publication_time, unavailable,
};

const UNKNOWN: &str = "Cannot verify the position and Perp liquidation inputs at one block.";
const GUARDED: &str = "The after-hours guard can change or close this position.";
const UNKNOWN_ORACLE_TIME: &str = "The oracle price publication time is unknown.";

pub(in crate::trade) struct PartialCloseQuote<'a> {
    pub(in crate::trade) amount: &'a BigUint,
    pub(in crate::trade) effective: Option<u128>,
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
                .position_snapshot(lcd, account, id, route, (indexed, Some(raw)), false)
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
        let Some(pair) = lcd.market(raw.get("market_index")?, Read::At(height)).await else {
            return unknown_oracle_time(result);
        };
        let Ok(oracle) = position_lcd
            .market_oracle_price(&state.route.contract_address, &pair, height)
            .await
        else {
            return unknown_oracle_time(result);
        };
        let Some(Some(published_at)) = oracle_publication_time(&oracle) else {
            return unknown_oracle_time(result);
        };
        let Some(market) = oracle
            .get("base_per_quote")
            .and_then(Value::as_str)
            .and_then(|price| Price::new(price, "Sai Perp market oracle", Some(now())))
        else {
            return unverified_market_oracle(
                result,
                "OraclePriceUnavailable",
                super::UNVERIFIED_MARKET_ORACLE,
            );
        };
        let queued = lcd
            .smart(json!({"market_order_config": {}}), Some(height))
            .await
            .ok()
            .and_then(|config| config["fill_at_next_oracle_price"].as_bool());
        let Some(ttl) = lcd
            .group(&pair, Some(height))
            .await
            .and_then(|group| group["stale_price_expiration_secs"].as_u64())
        else {
            return unknown_oracle_time(result);
        };
        if ttl > 0 {
            let Some(block_time) = lcd.block_timestamp(height).await else {
                return unknown_oracle_time(result);
            };
            if i128::from(block_time) - i128::from(published_at) > i128::from(ttl) {
                let reason = "Perp rejects the close at the observed stale oracle price.";
                for value in result.as_object_mut()?.values_mut() {
                    *value = unavailable(reason);
                }
                result["execution"] = json!({
                    "status": "would_be_rejected", "code": "OraclePriceStale", "reason": reason,
                });
                return Some(());
            }
        }
        if queued? {
            let queued = unavailable("Queued closes settle on a fresh oracle update.");
            for value in result.as_object_mut()?.values_mut() {
                *value = queued.clone();
            }
            return Some(());
        }

        let long = raw["long"].as_bool()?;
        let trade = raw.get("user")?;
        let threshold = lcd
            .smart(
                json!({"get_liquidation_price": {
                    "trade_id": raw.get("user_trade_index")?,
                    "trader": trade,
                    "include_borrowing_fees": true,
                }}),
                Some(height),
            )
            .await
            .ok()?;
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
        let effective = match partial.as_ref().and_then(|quote| quote.effective) {
            Some(effective) => Ok(effective),
            None => state.effective_leverage(position_lcd, id, height).await,
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
        result["fees"] = unavailable("Perp fee schedule is unavailable.");
        let schedule = lcd.fees(&pair, Some(height)).await?;
        let reduced = partial.as_ref().map_or(&collateral, |quote| quote.amount);
        let notional = reduced * effective / SCALE;
        let (_, fees) = base_fees(
            &schedule,
            &Price::usdc(),
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
    unverified_market_oracle(result, "OraclePublicationTimeUnknown", UNKNOWN_ORACLE_TIME)
}

fn unverified_market_oracle(result: &mut Value, code: &str, reason: &str) -> Option<()> {
    for field in result.as_object_mut()?.values_mut() {
        *field = unavailable(reason);
    }
    result["execution"]["code"] = json!(code);
    Some(())
}
