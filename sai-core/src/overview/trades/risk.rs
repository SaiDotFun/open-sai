use bigdecimal::BigDecimal;
use num_bigint::Sign;
use serde_json::{Value, json};

use super::{DECIMAL, USD_TOKEN, converted, decimal, float, level, scale_collateral};
use crate::overview::{
    Object, Stamp, add_age,
    chain::{Chain, ContractReads},
    iso, merge, now, unix_instant,
};

pub(super) async fn read(
    trade: &Object,
    reads: Option<&mut ContractReads<'_>>,
    addr: &str,
) -> Value {
    let Some(reads) = reads else {
        return json!({"riskStatus": "unavailable", "riskReason": "No matching LCD block"});
    };
    let mut risk = json!({"riskStatus": "unavailable"});
    match load(trade, reads, addr, &mut risk).await {
        Some(()) => risk,
        None => reason(
            risk,
            "Could not read matching contract risk data at one block",
        ),
    }
}

async fn load(
    trade: &Object,
    reads: &mut ContractReads<'_>,
    addr: &str,
    risk: &mut Value,
) -> Option<()> {
    let chain = reads.chain;
    let position = reads
        .query(
            None,
            json!({"get_trade": {"trader": addr, "index": trade["id"]}}),
        )
        .await?;
    let fetched = now();
    let raw = position.get("trade")?;
    if !agrees(trade, raw)? {
        inconsistent(risk, "Keeper and contract trade values differ");
        return Some(());
    }

    let market = &trade["perpBorrowing"];
    let liquidation = decimal(position.get("liquidation_price")?)?;
    let entry = decimal(raw.get("open_price")?)?;
    let base_id = market["baseToken"]["id"].as_u64()?;
    let quote_id = market["quoteToken"]["id"].as_u64()?;
    let adjustment = crate::keeper::truthy(&position["needs_after_hours_trigger"]);
    if liquidation.sign() == Sign::Minus || entry.sign() != Sign::Plus {
        return None;
    }
    let mut source = json!({"fetchedAt": iso(fetched), "sourceBlock": chain.height});
    add_age(&mut source, Some(chain.time), fetched);
    *risk = json!({
        "riskStatus": "partial",
        "afterHoursLeverageAdjustmentPending": adjustment,
        "riskSource": source,
        "liquidationPriceQuote": float(&liquidation),
        "liquidationPriceUsd": if quote_id == USD_TOKEN { float(&liquidation) } else { None },
    });

    let contract_market = reads.market(market["marketId"].as_u64()?).await?;
    if contract_market.get("base")? != &format!("TokenIndex({base_id})")
        || contract_market.get("quote")? != &format!("TokenIndex({quote_id})")
    {
        inconsistent(risk, "Keeper and contract disagree on the market pair");
        return Some(());
    }
    // Delay evaluating the group until after the oracle read so a partial result keeps the mark.
    let group = contract_market
        .get("group_index")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let (oracle, fetched) = reads.rate(base_id, quote_id).await?;
    let rate = oracle.get("rate")?;
    let mark = decimal(rate.get("base_per_quote")?)?;
    let quote_usd = decimal(rate.get("quote_price_usd")?)?;
    if mark.sign() != Sign::Plus
        || quote_usd.sign() != Sign::Plus
        || (quote_id == USD_TOKEN && quote_usd != 1)
    {
        return None;
    }
    let base_at = oracle_time(&rate["base_last_update_time"], chain);
    let quote_at =
        oracle_time(&rate["quote_last_update_time"], chain).filter(|_| quote_id != USD_TOKEN);
    let base_source = oracle_source(oracle.get("base")?, base_at, chain, *fetched)?;
    let quote_source = if quote_id == USD_TOKEN {
        Value::Null
    } else {
        oracle_source(oracle.get("quote")?, quote_at, chain, *fetched)?
    };
    let mut mark_source = if quote_source.is_null() {
        base_source.clone()
    } else {
        match (
            base_source["sourceAt"].as_str(),
            quote_source["sourceAt"].as_str(),
        ) {
            (Some(base), Some(quote)) if quote < base => quote_source.clone(),
            (Some(_), Some(_)) => base_source.clone(),
            _ => {
                json!({"sourceBlock": null, "sourceAt": null, "sourceAgeSeconds": null})
            }
        }
    };
    merge(
        &mut mark_source,
        json!({
            "queryBlock": chain.height, "fetchedAt": iso(*fetched),
            "base": base_source, "quote": quote_source,
        }),
    );
    merge(
        risk,
        json!({
            "markSource": mark_source,
            "markPriceUsd": converted(Some(&mark), Some(&quote_usd)),
            "liquidationPriceUsd": converted(Some(&liquidation), Some(&quote_usd)),
        }),
    );

    let Some(expiration) = reads.group_expiration(group.as_deref()?).await else {
        risk["riskReason"] = json!("Contract oracle staleness limit is unknown");
        return Some(());
    };
    let oldest = match (base_at, quote_at) {
        (Some(base), Some(quote)) => base.min(quote),
        (Some(base), None) if quote_id == USD_TOKEN => base,
        _ => {
            risk["riskReason"] = json!("Contract oracle publish time is unknown");
            return Some(());
        }
    };
    let long = raw["long"].as_bool()?;
    if expiration != 0 && (chain.time.timestamp() - oldest.timestamp()) as u64 > expiration {
        if (long && mark <= liquidation) || (!long && mark >= liquidation) {
            risk["liquidationWarning"] = json!("at or past liquidation (stale mark)");
        }
        risk["riskReason"] = json!("Contract oracle mark is stale");
        return Some(());
    }
    let tp = level(raw.get("tp")?)?;
    let sl = level(raw.get("sl")?)?;
    merge(
        risk,
        json!({
            "entryPriceUsd": converted(Some(&entry), Some(&quote_usd)),
            "takeProfitPriceUsd": converted(tp.as_ref(), Some(&quote_usd)),
            "stopLossPriceUsd": converted(sl.as_ref(), Some(&quote_usd)),
            "liquidationDistancePercent": distance(&mark, Some(&liquidation), !long),
            "takeProfitDistancePercent": distance(&mark, tp.as_ref(), long),
            "stopLossDistancePercent": distance(&mark, sl.as_ref(), !long),
        }),
    );
    if adjustment {
        risk["takeProfitDistancePercent"] = Value::Null;
        risk["stopLossDistancePercent"] = Value::Null;
        risk["riskReason"] = json!(
            "Gross PnL and TP/SL distance are unavailable while the after-hours leverage adjustment is pending"
        );
        return Some(());
    }

    let collateral = &market["collateralToken"];
    let conversion = if collateral["symbol"] == "USDC" {
        BigDecimal::new(1.into(), 6)
    } else {
        let pinned = if collateral["symbol"] == "stNIBI" {
            reads.rate(collateral["id"].as_u64()?, USD_TOKEN).await
        } else {
            None
        };
        let Some((oracle, fetched)) = pinned else {
            risk["riskReason"] = json!("Pinned collateral USD price is unavailable");
            return Some(());
        };
        let rate = oracle.get("rate")?;
        let price = decimal(rate.get("base_per_quote")?)?;
        if price.sign() != Sign::Plus {
            risk["riskReason"] = json!("Pinned collateral USD price is unavailable");
            return Some(());
        }
        let at = oracle_time(&rate["base_last_update_time"], chain);
        let mut source = oracle_source(oracle.get("base")?, at, chain, *fetched)?;
        merge(
            &mut source,
            json!({"fetchedAt": iso(*fetched), "queryBlock": chain.height}),
        );
        risk["collateralPriceSource"] = source;
        scale_collateral(&price)
    };
    let leverage = decimal(&raw["leverage"])?;
    let amount = decimal(&raw["collateral_amount"])?;
    let change = DECIMAL.round_decimal(if long { &mark - &entry } else { &entry - &mark });
    let roi = DECIMAL.round_decimal(change / &entry);
    // The contract caps gross return to the range -100% through 900%.
    let pnl = DECIMAL
        .multiply(&roi, &leverage)
        .clamp(BigDecimal::from(-1), BigDecimal::from(9));
    merge(
        risk,
        json!({
            "riskStatus": "verified",
            "collateralUsd": converted(Some(&amount), Some(&conversion)),
            "sizeUsd": converted(Some(&DECIMAL.multiply(&amount, &leverage)), Some(&conversion)),
            "unrealizedPnlUsd": converted(Some(&DECIMAL.multiply(&pnl, &amount)), Some(&conversion)),
            "unrealizedPnlPercent": float(&DECIMAL.multiply(&pnl, &BigDecimal::from(100))),
        }),
    );
    Some(())
}

fn distance(mark: &BigDecimal, level: Option<&BigDecimal>, above: bool) -> Option<f64> {
    let delta = DECIMAL.round_decimal(level? - mark);
    let ratio = DECIMAL.round_decimal(delta / mark);
    float(&DECIMAL.multiply(&ratio, &BigDecimal::from(if above { 100 } else { -100 })))
}

fn agrees(indexed: &Object, raw: &Value) -> Option<bool> {
    let market = &indexed["perpBorrowing"];
    let field = |key| raw.get(key);
    let matches = field("user")? == indexed.get("trader")?
        && field("user_trade_index")? == &format!("UserTradeIndex({})", indexed["id"])
        && field("market_index")? == &format!("MarketIndex({})", market["marketId"])
        && field("collateral_index")?
            == &format!("TokenIndex({})", market["collateralToken"]["id"])
        && field("is_open")?.as_bool() == Some(true)
        && field("trade_type")? == "trade"
        && field("long")?.as_bool() == indexed["isLong"].as_bool()
        && indexed["isLong"].is_boolean()
        && indexed["collateralAmount"].is_number()
        && decimal(field("collateral_amount")?)? == decimal(&indexed["collateralAmount"])?
        && float(&decimal(field("leverage")?)?) == indexed["leverage"].as_f64()
        && float(&decimal(field("open_price")?)?) == indexed["openPrice"].as_f64()
        && trigger(field("tp")?)? == trigger(indexed.get("tp")?)?
        && trigger(field("sl")?)? == trigger(indexed.get("sl")?)?;
    Some(matches)
}

fn trigger(value: &Value) -> Option<Option<f64>> {
    Some(level(value)?.as_ref().and_then(float))
}

fn inconsistent(risk: &mut Value, message: &str) {
    *risk = json!({"riskStatus": "inconsistent", "riskReason": message});
}

fn reason(mut risk: Value, message: &str) -> Value {
    risk["riskReason"] = json!(message);
    risk
}

fn oracle_time(value: &Value, chain: &Chain<'_>) -> Option<Stamp> {
    unix_instant(value).filter(|stamp| stamp.timestamp_micros() > 0 && *stamp <= chain.time)
}

fn oracle_source(
    stamp: &Value,
    published: Option<Stamp>,
    chain: &Chain<'_>,
    fetched: Stamp,
) -> Option<Value> {
    stamp.as_object()?;
    let written = stamp["price_block_time_ms"]
        .as_i64()
        .and_then(|millis| oracle_time(&json!(millis as f64 / 1_000.0), chain));
    let height = stamp["price_block"]
        .as_u64()
        .filter(|height| (1..=chain.height).contains(height));
    let mut source = json!({"sourceBlock": height});
    add_age(&mut source, written.or(published), fetched);
    Some(source)
}
