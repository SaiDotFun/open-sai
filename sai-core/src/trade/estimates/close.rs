use std::time::Duration;

use serde_json::{Map, Value, json};
use tokio::time::{Instant, sleep, timeout_at};

use crate::{
    keeper::{MARKET, POSITION, TIMEOUT, truthy},
    storage::Session,
    trade::{TradePreview, Trading, confirmation::canonical_price},
};

use super::{
    COLLATERAL_ORACLE, FEE_UNAVAILABLE, Price, SCALE, USDC_PLACES, base_fees, decimal, fixed,
    integer, json_index,
    lcd::{QuoteLcd, ReadError},
    liquidation_fee, not_applicable, now, unavailable,
};

const UNKNOWN: &str = "Cannot verify the position and Perp liquidation inputs at one block.";
const GUARDED: &str = "The after-hours guard can change or close this position.";

impl Trading<'_> {
    pub(super) async fn close_estimates(
        &self,
        preview: &TradePreview,
        session: Option<&Session>,
    ) -> Value {
        let mut result = json!({
            "execution": unavailable(UNKNOWN),
            "fees": unavailable(UNKNOWN),
            "liquidationPrice": not_applicable("A full close leaves no position."),
            "worstCaseFill": {
                "status": "unbounded",
                "reason": "This Keeper intent carries no slippage limit.",
            },
        });
        if let (Some(endpoint), Some(session), Some(target)) =
            (self.lcd_endpoint, session, &preview.intent.target)
        {
            self.quote_close(endpoint, session.addr(), &target.trade_id, &mut result)
                .await;
        }
        result
    }

    async fn quote_close(
        &self,
        endpoint: &str,
        account: &str,
        id: &str,
        result: &mut Value,
    ) -> Option<()> {
        let id = serde_json::from_str(id).ok()?;
        let lcd = QuoteLcd::new(endpoint, self.trading_contract(endpoint).await.ok()?);
        let raw = self.position_snapshot(&lcd, account, &id).await?;
        let long = raw["long"].as_bool()?;
        let trade = raw.get("user")?;
        let read = json!({"get_trade_raw": {"trader": trade, "index": id}});
        let (latest, height) = lcd.anchor(read).await?;
        if !same_position(&raw, &latest) {
            return None;
        }
        let height = Some(height.as_str());

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
                "amountUsdc": canonical_price(&fixed(&liquidation_fee(&collateral), USDC_PLACES)),
                "basis": "Gross Perp liquidation fee components.",
                "excludes": ["Borrowing fees", "network fees"],
            });
            result["liquidationPrice"] = not_applicable("Liquidation closes the entire position.");
            return Some(());
        }

        let stored = decimal(&raw["leverage"])?;
        let effective = match effective_leverage(&lcd, &raw, &id, height, stored).await {
            Ok(Some(leverage)) => leverage,
            Ok(None) | Err(ReadError::Failed | ReadError::Http(_)) => {
                result["execution"] = unavailable(GUARDED);
                result["fees"] = unavailable(GUARDED);
                result["liquidationPrice"] = unavailable(GUARDED);
                return Some(());
            }
            Err(ReadError::UnverifiedHeight) => return None,
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

    async fn position_snapshot(
        &self,
        lcd: &QuoteLcd<'_>,
        account: &str,
        id: &Value,
    ) -> Option<Value> {
        let mut indexed = self.indexed_position(account, id, TIMEOUT).await?;
        let borrowing = indexed.get("perpBorrowing")?.clone();
        let collateral = borrowing.get("collateralToken")?;
        if collateral["symbol"] != "USDC" {
            return None;
        }
        let trader = indexed.get("trader")?.as_str()?.to_owned();
        let market_id = borrowing.get("marketId")?;
        let market = format!("MarketIndex({})", json_index(market_id)?);
        let collateral_id = collateral.get("id")?;
        let token = format!("TokenIndex({})", json_index(collateral_id)?);
        let variables = Map::from_iter([
            ("market".into(), market_id.clone()),
            ("collateral".into(), collateral_id.clone()),
        ]);
        let data = self
            .keeper
            .request(MARKET, variables, None, TIMEOUT)
            .await
            .ok()?;
        let row = &data["perp"]["borrowing"];
        if row["marketId"] != *market_id
            || row["collateralToken"]["id"] != *collateral_id
            || !truthy(&row["isOpen"])
        {
            return None;
        }
        let read_raw = || async {
            let raw = lcd
                .smart(
                    json!({"get_trade_raw": {"trader": trader, "index": id}}),
                    None,
                )
                .await
                .ok()?;
            (raw["user"] == trader
                && raw["user_trade_index"] == format!("UserTradeIndex({id})")
                && raw["market_index"] == market
                && raw["collateral_index"] == token
                && raw["is_open"].as_bool() == Some(true)
                && raw["trade_type"] == "trade")
                .then_some(raw)
        };
        let mut raw = read_raw().await?;
        if !same_margin(&raw, &indexed)? {
            let deadline = Instant::now() + Duration::from_secs(2);
            for delay in [250, 500].map(Duration::from_millis) {
                if deadline.saturating_duration_since(Instant::now()) <= delay {
                    break;
                }
                sleep(delay).await;
                indexed = self
                    .indexed_position(
                        account,
                        id,
                        deadline.saturating_duration_since(Instant::now()),
                    )
                    .await?;
                if indexed["trader"] != trader || indexed["perpBorrowing"] != borrowing {
                    return None;
                }
                raw = timeout_at(deadline, read_raw()).await.ok()??;
                if same_margin(&raw, &indexed)? {
                    break;
                }
            }
            if Instant::now() >= deadline || !same_margin(&raw, &indexed)? {
                return None;
            }
        }
        let funds = lcd
            .smart(
                json!({"get_exchange_balance": {"user": trader, "collateral_index": token}}),
                None,
            )
            .await
            .ok()?;
        (funds["collateral_index"] == token).then_some(raw)
    }

    async fn indexed_position(
        &self,
        account: &str,
        id: &Value,
        timeout: Duration,
    ) -> Option<Value> {
        let variables =
            Map::from_iter([("trader".into(), json!(account)), ("id".into(), id.clone())]);
        let mut data = self
            .keeper
            .request(POSITION, variables, None, timeout)
            .await
            .ok()?;
        let position = &mut data["perp"]["trade"];
        (truthy(&position["isOpen"]) && position["tradeType"] == "trade").then(|| position.take())
    }
}

fn same_margin(raw: &Value, indexed: &Value) -> Option<bool> {
    let text = |value: &Value| match value {
        Value::String(value) => Some(value.clone()),
        Value::Number(value) => Some(value.to_string()),
        _ => None,
    };
    Some(
        text(&raw["collateral_amount"])? == text(&indexed["collateralAmount"])?
            && text(&raw["leverage"])?.parse::<f64>().ok()?
                == text(&indexed["leverage"])?.parse::<f64>().ok()?,
    )
}

fn same_position(prior: &Value, latest: &Value) -> bool {
    [
        "user",
        "user_trade_index",
        "market_index",
        "collateral_index",
        "collateral_amount",
        "leverage",
        "is_open",
        "trade_type",
        "long",
        "open_price",
    ]
    .iter()
    .all(|key| prior[*key] == latest[*key])
}

async fn effective_leverage(
    lcd: &QuoteLcd<'_>,
    raw: &Value,
    id: &Value,
    height: Option<&str>,
    stored: u128,
) -> Result<Option<u128>, ReadError> {
    let index = &raw["market_index"];
    let enabled = lcd
        .smart(
            json!({"is_after_hours_market": {"market_index": index}}),
            height,
        )
        .await?;
    match enabled.as_bool() {
        Some(false) => return Ok(Some(stored)),
        None => return Ok(None),
        Some(true) => {}
    }
    let pair = lcd
        .smart(json!({"get_market": {"index": index}}), height)
        .await?;
    let block = |key: &str| match pair.get(key) {
        Some(Value::Null) => Ok(None),
        Some(value) => value.as_i64().map(Some).ok_or(ReadError::Failed),
        None => Err(ReadError::Failed),
    };
    let (Some(opened), Some(closed)) = (block("block_last_opened")?, block("block_last_closed")?)
    else {
        return Ok(Some(stored));
    };
    if opened > closed {
        let records = lcd
            .smart(
                json!({"get_trade_infos": {
                    "trader": raw["user"], "start_idx": id, "scan_limit": 1,
                }}),
                height,
            )
            .await?;
        let Some([info]) = records.as_array().map(Vec::as_slice) else {
            return Ok(None);
        };
        let Some(created) = info["created_block"].as_i64() else {
            return Ok(None);
        };
        if created >= opened {
            return Ok(Some(stored));
        }
    }
    let cap = lcd
        .smart(json!({"get_after_hours_leverage": {}}), height)
        .await?;
    Ok(decimal(&cap).map(|cap| stored.min(cap)))
}
