mod risk;

use std::{
    collections::{BTreeSet, HashMap},
    num::NonZeroU64,
    sync::LazyLock,
    time::Duration,
};

use bigdecimal::{BigDecimal, Context, RoundingMode};
use serde_json::{Value, json};

use crate::{
    keeper,
    reconcile::{self, Outcome},
};

use super::{
    Object, Overview, PAGINATION_ERROR, Stamp,
    chain::{ContractReads, membership},
    error_value, failed, incomplete, instant, invalid, iso, merge, now, number, success, variables,
};

const USD_TOKEN: u64 = 0;
const PNL_BASIS: &str = "grossBeforeFees";
static DECIMAL: LazyLock<Context> = LazyLock::new(|| {
    Context::new(
        NonZeroU64::new(28).expect("decimal precision"),
        RoundingMode::HalfEven,
    )
});

pub(super) struct TradeSections {
    pub positions: Value,
    pub orders: Value,
    pub prices: Value,
}

struct Price {
    usd: f64,
    block: Value,
    token: Value,
}

type Prices = HashMap<u64, Price>;

pub(super) async fn read(view: &Overview<'_>) -> TradeSections {
    let snapshot = view
        .pages(keeper::OVERVIEW_TRADES, "trades", "id")
        .await
        .and_then(|snapshot| {
            if snapshot.rows.iter().all(|row| {
                matches!(
                    row.get("tradeType").and_then(Value::as_str),
                    Some("trade" | "limit" | "stop")
                )
            }) {
                Ok(snapshot)
            } else {
                Err(invalid())
            }
        });
    let snapshot = match snapshot {
        Ok(snapshot) => snapshot,
        Err(error) => {
            let section = failed(error);
            return TradeSections {
                positions: section.clone(),
                orders: section,
                prices: json!({"fetchedAt": null, "data": []}),
            };
        }
    };
    let snapshot_fetched = now();
    let unindexed_fill = fill_awaits_indexing(view).await;
    let mut prices = if snapshot.rows.is_empty() {
        Ok((Prices::new(), None))
    } else {
        view.keeper
            .request(keeper::PRICES, Object::new(), None, keeper::TIMEOUT)
            .await
            .and_then(|data| Ok((parse_prices(&data).ok_or_else(invalid)?, Some(now()))))
    };
    let chain = view.chain().await;
    let indexed = snapshot
        .rows
        .iter()
        .filter(|row| row["tradeType"] == "trade")
        .map(|row| row["id"].as_u64().expect("scan validated IDs"))
        .collect();
    let membership = membership(chain, view.addr, indexed).await;
    let membership_complete = membership["status"] == "verified" && !unindexed_fill;
    let mut reads = chain.map(ContractReads::new);
    let quotes = prices.as_ref().map(|(prices, _)| prices).ok();

    let mut positions = read_rows(
        view,
        &snapshot.rows,
        quotes,
        reads.as_mut(),
        snapshot_fetched,
        true,
    )
    .await;
    let mut orders = read_rows(view, &snapshot.rows, quotes, None, snapshot_fetched, false).await;
    positions["membership"] = membership;
    for section in [&mut positions, &mut orders] {
        if section.get("data").is_some() && !snapshot.complete {
            section["error"] = error_value(incomplete(PAGINATION_ERROR));
        }
    }
    if let Some(items) = positions.get_mut("data").map(Value::take) {
        let rows = items.as_array().expect("positions list");
        if snapshot.complete && !membership_complete {
            let reason = if unindexed_fill {
                "Keeper reports a fill awaiting indexing. Positions may be incomplete."
            } else if positions["membership"]["status"] == "stale" {
                "The live position check used a stale LCD block. Positions may be incomplete."
            } else {
                "The indexed positions could not be confirmed against the live contract list."
            };
            positions["error"] = error_value(incomplete(reason));
        } else if positions.get("error").is_none()
            && reads.as_ref().is_some_and(ContractReads::deadline_expired)
            && rows.iter().any(|row| row["riskStatus"] != "verified")
        {
            positions["error"] = error_value(incomplete(
                "An LCD deadline expired. Position risk is partial.",
            ));
        }
        let total = if snapshot.complete && membership_complete {
            rows.iter().try_fold(0.0, |sum, row| {
                Some(sum + row["unrealizedPnlUsd"].as_f64()?)
            })
        } else {
            None
        };
        positions["data"] =
            json!({"items": items, "unrealizedPnlUsd": total, "unrealizedPnlBasis": PNL_BASIS});
    }

    let prices = match prices.as_mut() {
        Ok((quotes, fetched)) => price_section(view, quotes, *fetched, &positions, &orders).await,
        Err(_) => failed(prices.err().expect("price read failed")),
    };
    TradeSections {
        positions,
        orders,
        prices,
    }
}

async fn fill_awaits_indexing(view: &Overview<'_>) -> bool {
    view.keeper
        .request(
            keeper::INTENTS,
            variables(json!({"user": view.addr})),
            None,
            Duration::from_secs(3),
        )
        .await
        .ok()
        .and_then(|data| {
            Some(data.get("userIntents")?.as_array()?.iter().any(|row| {
                row["errorCode"] == reconcile::OUTCOME_NOT_INDEXED
                    && reconcile::classify_outcome(row) == Outcome::Pending
            }))
        })
        .unwrap_or(false)
}

fn parse_prices(data: &Object) -> Option<Prices> {
    let mut prices = Prices::new();
    for row in data.get("oracle")?["tokenPricesUsd"].as_array()? {
        let usd = number(row.get("priceUsd")?)?;
        if usd <= 0.0 {
            continue;
        }
        let token = row.get("token")?;
        prices.insert(
            token["id"].as_u64()?,
            Price {
                usd,
                block: row.get("lastUpdatedBlock")?.clone(),
                token: token_name(token)?.clone(),
            },
        );
    }
    Some(prices)
}

fn token_name(token: &Value) -> Option<&Value> {
    let symbol = token.get("symbol")?;
    if keeper::truthy(symbol) {
        Some(symbol)
    } else {
        token.get("name")
    }
}

async fn price_section(
    view: &Overview<'_>,
    quotes: &Prices,
    fetched: Option<Stamp>,
    positions: &Value,
    orders: &Value,
) -> Value {
    let mut used = BTreeSet::new();
    for row in [&positions["data"]["items"], &orders["data"]]
        .into_iter()
        .filter_map(Value::as_array)
        .flatten()
    {
        if row["collateralToken"] != "USDC" && row.get("collateralPriceSource").is_none() {
            used.extend(row["collateralTokenId"].as_u64());
        }
        if row["quoteTokenId"] != USD_TOKEN && row["riskStatus"] != "verified" {
            used.extend(row["quoteTokenId"].as_u64());
        }
    }
    let mut prices = Vec::new();
    let mut missing = false;
    for id in used {
        if let Some(price) = quotes.get(&id) {
            let mut row = json!({"tokenId": id, "token": price.token, "usd": price.usd});
            let source = view
                .block_source(
                    &price.block["block"],
                    &price.block["block_ts"],
                    fetched.expect("prices have a read time"),
                    true,
                )
                .await;
            merge(&mut row, source);
            prices.push(row);
        } else {
            missing = true;
        }
    }
    let mut section = json!({"fetchedAt": fetched.map(iso), "data": prices});
    if missing {
        section["error"] = error_value(incomplete(
            "Keeper has no positive USD price for one or more tokens",
        ));
    }
    section
}

async fn read_rows(
    view: &Overview<'_>,
    rows: &[Object],
    prices: Option<&Prices>,
    mut reads: Option<&mut ContractReads<'_>>,
    fetched: Stamp,
    positions: bool,
) -> Value {
    let mut items = Vec::new();
    for row in rows
        .iter()
        .filter(|row| (row["tradeType"] == "trade") == positions)
    {
        let Some(mut item) = trade_row(row, prices) else {
            return failed(invalid());
        };
        if positions {
            let risk = risk::read(row, reads.as_deref_mut(), view.addr).await;
            let can_use_estimate = risk.get("riskSource").is_none();
            merge(&mut item, risk);
            if can_use_estimate {
                let Some(state) = row.get("state") else {
                    return failed(invalid());
                };
                if !state.is_null() {
                    let Some(estimate) = number(&state["liquidationPrice"]) else {
                        return failed(invalid());
                    };
                    item["keeperLiquidationEstimatePriceQuote"] = json!(estimate);
                    item["keeperLiquidationEstimateSource"] = json!({
                        "fetchedAt": row["fetchedAt"], "sourceAt": null, "sourceAgeSeconds": null,
                    });
                }
            }
        }
        let Some(block) = row
            .get("lastUpdatedBlock")
            .filter(|block| block.is_object())
        else {
            return failed(invalid());
        };
        let stamp = instant(&row["fetchedAt"]).expect("page time");
        item["fetchedAt"] = row["fetchedAt"].clone();
        merge(
            &mut item,
            view.block_source(&block["block"], &block["block_ts"], stamp, true)
                .await,
        );
        items.push(item);
    }
    success(json!(items), fetched)
}

fn trade_row(trade: &Object, prices: Option<&Prices>) -> Option<Value> {
    let market = trade.get("perpBorrowing")?;
    let collateral = market.get("collateralToken")?;
    let quote = market.get("quoteToken")?;
    let collateral_id = collateral["id"].as_u64()?;
    let quote_id = quote["id"].as_u64()?;
    let symbol = collateral.get("symbol")?;
    let price = |id: u64| {
        if id == USD_TOKEN {
            Some(BigDecimal::from(1))
        } else {
            decimal(&json!(prices?.get(&id)?.usd))
        }
    };
    let conversion = if symbol == "USDC" {
        Some(BigDecimal::new(1.into(), 6))
    } else if symbol == "stNIBI" {
        price(collateral_id).map(|price| scale_collateral(&price))
    } else {
        None
    };
    let amount = decimal(&trade["collateralAmount"])?;
    let leverage = decimal(&trade["leverage"])?;
    let quote_usd = price(quote_id);
    let entry = decimal(&trade["openPrice"])?;
    let tp = level(trade.get("tp")?)?;
    let sl = level(trade.get("sl")?)?;
    let mut row = json!({
        "id": trade["id"],
        "marketId": market.get("marketId")?,
        "market": market.get("baseToken")?.get("symbol")?,
        "side": if keeper::truthy(trade.get("isLong")?) { "long" } else { "short" },
        "collateralToken": symbol,
        "collateralTokenId": collateral_id,
        "collateralBaseUnits": trade["collateralAmount"],
        "collateralUsd": converted(Some(&amount), conversion.as_ref()),
        "leverage": float(&leverage),
        "sizeUsd": converted(Some(&DECIMAL.multiply(&amount, &leverage)), conversion.as_ref()),
        "quoteToken": token_name(quote)?,
        "quoteTokenId": quote_id,
        "takeProfitPriceUsd": converted(tp.as_ref(), quote_usd.as_ref()),
        "stopLossPriceUsd": converted(sl.as_ref(), quote_usd.as_ref()),
    });
    if trade["tradeType"] != "trade" {
        row["orderType"] = trade["tradeType"].clone();
        row["triggerPriceUsd"] = json!(converted(Some(&entry), quote_usd.as_ref()));
    } else {
        merge(
            &mut row,
            json!({
                "entryPriceUsd": converted(Some(&entry), quote_usd.as_ref()),
                "markPriceUsd": null,
                "unrealizedPnlUsd": null,
                "unrealizedPnlPercent": null,
                "unrealizedPnlBasis": PNL_BASIS,
                "liquidationPriceUsd": null,
                "liquidationDistancePercent": null,
                "takeProfitDistancePercent": null,
                "stopLossDistancePercent": null,
            }),
        );
    }
    Some(row)
}

fn decimal(value: &Value) -> Option<BigDecimal> {
    let value: BigDecimal = match value {
        Value::String(text) => text.trim().parse().ok()?,
        Value::Number(number) if !number.to_string().contains(['.', 'e', 'E']) => {
            number.to_string().parse().ok()?
        }
        Value::Number(_) => number(value)?.to_string().parse().ok()?,
        _ => return None,
    };
    // Contract decimal quantities and base units are bounded integers. Reject exponents
    // far outside that range before aligning scales in arithmetic.
    (value.fractional_digit_count().unsigned_abs() <= 1_000 && value.digits() <= 1_000)
        .then_some(value)
}

fn float(value: &BigDecimal) -> Option<f64> {
    value
        .to_string()
        .parse::<f64>()
        .ok()
        .filter(|value| value.is_finite())
}

fn level(value: &Value) -> Option<Option<BigDecimal>> {
    if value.is_null() {
        return Some(None);
    }
    let level = decimal(value)?;
    Some((level.sign() == num_bigint::Sign::Plus).then_some(level))
}

fn scale_collateral(price: &BigDecimal) -> BigDecimal {
    DECIMAL.round_decimal(price / BigDecimal::from(1_000_000))
}

fn converted(value: Option<&BigDecimal>, price: Option<&BigDecimal>) -> Option<f64> {
    float(&DECIMAL.multiply(value?, price?))
}
