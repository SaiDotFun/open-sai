use serde_json::{Value, json};

use crate::{
    errors::{Code, SaiError},
    keeper::TIMEOUT,
    storage::Session,
    trade::{
        TradePreview, Trading,
        actions::invalid,
        position::{ContractPositionError, Lcd, Snapshot, SnapshotError},
    },
};

use super::{
    COLLATERAL_ORACLE, FEE_UNAVAILABLE, Price, SCALE, base_fees, decimal, integer, lcd::QuoteLcd,
    liquidation_fee, not_applicable, now, unavailable,
};

const UNKNOWN: &str = "Cannot verify the position and Perp liquidation inputs at one block.";
const GUARDED: &str = "The after-hours guard can change or close this position.";

impl Trading<'_> {
    pub(super) async fn close_estimates(
        &self,
        preview: &mut TradePreview,
        session: Option<&Session>,
    ) -> Result<Value, SaiError> {
        let mut result = json!({
            "execution": unavailable(UNKNOWN),
            "fees": unavailable(UNKNOWN),
            "liquidationPrice": not_applicable("A full close leaves no position."),
            "worstCaseFill": {
                "status": "unbounded",
                "reason": "This Keeper intent carries no slippage limit.",
            },
        });
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
        let (route, raw) = match Self::verify_contract_owner(&lcd, account, &id, route).await {
            Ok(verified) => verified,
            Err(error @ ContractPositionError::Owner) => return Err(error.into()),
            Err(ContractPositionError::Read(_)) => return Ok(result),
        };
        let quote = async {
            let state = self
                .position_snapshot(&lcd, account, &id, route.clone(), indexed, Some(raw))
                .await?;
            if let Some(height) = state.verified_height(&lcd, &id).await? {
                let quote_lcd = QuoteLcd::new(endpoint, state.route.contract_address.clone());
                Self::quote_close(&quote_lcd, &lcd, &state, &id, &height, &mut result).await;
            }
            Ok(())
        };
        match quote.await {
            Err(SnapshotError::Owner(message)) => return Err(invalid(message)),
            Err(SnapshotError::IndexMismatch) => return Ok(result),
            Ok(()) | Err(SnapshotError::Unavailable) => {
                // Missing estimate inputs do not invalidate the verified owner and index.
                preview.position_owner_verified = Some(true);
                preview.position_route = Some(route);
            }
        }
        Ok(result)
    }

    async fn quote_close(
        lcd: &QuoteLcd<'_>,
        position_lcd: &Lcd<'_>,
        state: &Snapshot,
        id: &Value,
        height: &str,
        result: &mut Value,
    ) -> Option<()> {
        let raw = &state.raw;
        let long = raw["long"].as_bool()?;
        let trade = raw.get("user")?;
        let height = Some(height);

        let prices = lcd
            .smart(
                json!({"get_perp_prices": {
                    "market_index": raw.get("market_index")?,
                    "collateral_index": raw.get("collateral_index")?,
                }}),
                height,
            )
            .await
            .ok()?;
        let oracle = prices["market"].as_object()?;
        let mut published_at = None;
        for key in ["base_last_update_time", "quote_last_update_time"] {
            if let Some(value) = oracle.get(key).filter(|value| !value.is_null()) {
                let time = value.as_u64()?;
                published_at = Some(published_at.map_or(time, |prior: u64| prior.min(time)));
            }
        }
        let mut pair = None;
        if let Some(time) = published_at {
            let market = lcd.market(&raw["market_index"], height).await?;
            let group = lcd.group(&market, height).await?;
            let ttl = group["stale_price_expiration_secs"].as_u64()?;
            if ttl > 0
                && i128::from(lcd.block_timestamp(height?).await?) - i128::from(time)
                    > i128::from(ttl)
            {
                let config = lcd
                    .smart(json!({"market_order_config": {}}), height)
                    .await
                    .ok()?;
                if config["fill_at_next_oracle_price"].as_bool()? {
                    let queued = unavailable("Queued closes settle on a fresh oracle update.");
                    for value in result.as_object_mut()?.values_mut() {
                        *value = queued.clone();
                    }
                } else {
                    let reason = "Perp rejects the close at the observed stale oracle price.";
                    for value in result.as_object_mut()?.values_mut() {
                        *value = unavailable(reason);
                    }
                    result["execution"] = json!({
                        "status": "would_be_rejected", "code": "OraclePriceStale", "reason": reason,
                    });
                }
                return Some(());
            }
            pair = Some(market);
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

        decimal(&raw["leverage"])?;
        let effective = match state.effective_leverage(position_lcd, id, height?).await {
            Ok(leverage) => leverage,
            Err(error) if error.code == Code::PreviewMismatch => return None,
            Err(_) => {
                result["execution"] = unavailable(GUARDED);
                result["fees"] = unavailable(GUARDED);
                result["liquidationPrice"] = unavailable(GUARDED);
                return Some(());
            }
        };
        result["execution"] = json!({"status": "voluntary_close"});
        result["fees"] = unavailable(FEE_UNAVAILABLE);
        let price = Price::new(
            prices["collateral"].as_str()?,
            COLLATERAL_ORACLE,
            Some(observed_at),
        )?;
        let pair = match pair {
            Some(pair) => pair,
            None => lcd.market(&raw["market_index"], height).await?,
        };
        let schedule = lcd.fees(&pair, height).await?;
        let notional = &collateral * effective / SCALE;
        let (_, fees) = base_fees(&schedule, &price, notional, false, false, Some(&collateral))?;
        result["fees"] = fees;
        Some(())
    }
}
