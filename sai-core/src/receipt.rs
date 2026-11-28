//! Project the Keeper evidence for a waited intent.

use std::{ptr, sync::LazyLock};

use bigdecimal::{BigDecimal, ToPrimitive};
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::{keeper, usdc, wait::Outcome};

const TRADE_FILLS: &[&str] = &[
    "position_opened",
    "position_closed_user",
    "trade_size_increase",
    "trade_size_decrease",
];

/// Values that Keeper ties to the fill only after the receipt is confirmed.
#[derive(Default)]
pub(crate) struct FillValues {
    pub price: Value,
    pub size: Option<i64>,
    pub realized_pnl: Value,
}

pub(crate) struct Evidence<'a> {
    row: &'a Value,
    indexed: &'a Value,
    matching: Vec<&'a Value>,
    order_type: Option<&'static str>,
}

impl<'a> Evidence<'a> {
    pub fn new(row: &'a Value, history: &'a [Value]) -> Self {
        let block = &row["execBlock"];
        let matching: Vec<_> = history
            .iter()
            .filter(|item| {
                row["lifecycleStep"] != "exec_pass"
                    || block.is_null()
                    || item["block"]["block"] == *block
            })
            .collect();
        let indexed = matching
            .iter()
            .copied()
            .find(|item| item["tradeChangeType"] == "position_liquidated")
            .or_else(|| {
                matching.iter().copied().find(|item| {
                    let change = &item["tradeChangeType"];
                    !change.is_null()
                        && change != "after_hours_risk_trigger"
                        && !matches_action(row, order_type(row, item), change)
                })
            })
            .or_else(|| {
                matching.iter().copied().find(|item| {
                    let change = &item["tradeChangeType"];
                    !change.is_null() && change != "after_hours_risk_trigger"
                })
            })
            .or_else(|| matching.first().copied())
            .unwrap_or(&Value::Null);
        Self {
            row,
            indexed,
            matching,
            order_type: order_type(row, indexed),
        }
    }

    pub fn success(&self) -> Outcome {
        let change = &self.indexed["tradeChangeType"];
        match change.as_str() {
            Some("position_liquidated") => Outcome::Liquidated,
            _ if keeper::truthy(change) => {
                if !matches_action(self.row, self.order_type, change) {
                    Outcome::Unexpected
                } else if matches!(
                    change.as_str(),
                    Some("limit_order_created" | "stop_order_created")
                ) {
                    Outcome::Placed
                } else if change
                    .as_str()
                    .is_some_and(|change| TRADE_FILLS.contains(&change))
                {
                    Outcome::Filled
                } else {
                    Outcome::Succeeded
                }
            }
            _ if matches!(self.order_type, Some("limit" | "stop")) => Outcome::Placed,
            _ => Outcome::Succeeded,
        }
    }

    /// Identify a position opened by a correlated end-block market order.
    pub fn opening_fill_position(&self) -> Option<(i64, i64)> {
        if self.indexed["tradeChangeType"] != "position_opened" {
            return None;
        }
        let block = self.indexed["block"]["block"].as_i64()?;
        let trade = &self.indexed["trade"];
        let order = &self.indexed["marketOrder"];
        let trade_id = trade["id"].as_i64()?;
        if block <= 0
            || trade_id < 0
            || order["orderExecBlock"].as_i64() != Some(block)
            || trade["openBlock"]["block"].as_i64() != Some(block)
        {
            return None;
        }
        // Opening collateral cannot exceed the original escrow. This
        // already proves some history snapshots are stale.
        if trade["openCollateralAmount"]
            .as_i64()
            .zip(order["collateralAmount"].as_i64())
            .is_some_and(|(collateral, escrow)| collateral > escrow)
        {
            return None;
        }
        Some((trade_id, block))
    }

    pub fn opening_fill(
        &self,
        data: &Map<String, Value>,
        trade_id: i64,
        block: i64,
        account: &str,
    ) -> FillValues {
        let Some(trade) = data.get("perp").and_then(|perp| perp.get("trade")) else {
            return FillValues::default();
        };
        // Ordinary transactions finish before the end-block market fill. A
        // new position still at its opening update describes that fill.
        if trade["id"].as_i64() != Some(trade_id)
            || trade["trader"]
                .as_str()
                .and_then(keeper::canonical_account)
                .as_deref()
                != Some(account)
            || trade["tradeType"] != "trade"
            || trade["isOpen"] != true
            || trade["openBlock"]["block"].as_i64() != Some(block)
            || trade["lastUpdatedBlock"]["block"].as_i64() != Some(block)
        {
            return FillValues::default();
        }
        let price = number(&trade["openPrice"]);
        let price = if decimal_float(&price).is_some_and(|value| value > 0.0) {
            price
        } else {
            Value::Null
        };
        let size = opening_size(trade, &self.row["body"]["leverage"]);
        FillValues {
            price,
            size,
            realized_pnl: Value::Null,
        }
    }

    /// Identify a full close that Keeper ties to its end-block market fill.
    /// History rows carry the position as it is now, and a closed position
    /// does not change again, so its close price is the fill price.
    pub fn closing_fill(&self) -> Option<(i64, Value)> {
        if self.indexed["tradeChangeType"] != "position_closed_user" {
            return None;
        }
        let block = self.indexed["block"]["block"].as_i64()?;
        let history_id = self.indexed["id"].as_i64()?;
        let trade = &self.indexed["trade"];
        if block <= 0
            || history_id < 0
            || trade["isOpen"] != false
            || trade["closeBlock"]["block"].as_i64() != Some(block)
            || self.indexed["marketOrder"]["orderExecBlock"].as_i64() != Some(block)
        {
            return None;
        }
        let price = number(&trade["closePrice"]);
        decimal_float(&price)
            .is_some_and(|value| value > 0.0)
            .then_some((history_id, price))
    }

    /// Read the gross realized PnL Keeper recorded on the closing history row.
    pub fn closing_pnl(&self, data: &Map<String, Value>, history_id: i64) -> Value {
        let rows = data
            .get("perp")
            .and_then(|perp| perp.get("tradeHistory"))
            .and_then(Value::as_array);
        let Some(row) = rows.into_iter().flatten().find(|row| {
            row["id"].as_i64() == Some(history_id)
                && row["tradeChangeType"] == "position_closed_user"
        }) else {
            return Value::Null;
        };
        let (units, fraction) = (
            number(&row["grossPricePnlCollateral"]),
            number(&row["grossPricePnlPct"]),
        );
        let (Some(amount), Some(fraction)) = (decimal(&units), decimal(&fraction)) else {
            return Value::Null;
        };
        let usd = usdc_collateral(&self.indexed["trade"])
            .then(|| plain(&(amount / BigDecimal::from(1_000_000)).round(6)));
        json!({
            "basis": "grossBeforeFees",
            "collateralBaseUnits": units,
            "usd": usd,
            "percent": plain(&(fraction * BigDecimal::from(100))),
        })
    }

    pub fn build(
        &self,
        outcome: Outcome,
        request_hash: &Value,
        request_block: &Value,
        fill_values: FillValues,
        project: impl Fn(Value) -> Value,
    ) -> Value {
        let public = self
            .indexed
            .as_object()
            .map_or(Value::Null, |row| project(keeper::intent_history(row)));
        let intent = self
            .row
            .as_object()
            .map_or(Value::Null, |row| project(keeper::intent_record(row)));
        let order = &self.indexed["marketOrder"];
        let change = match self.indexed["tradeChangeType"].as_str() {
            Some(change @ ("order_updated" | "position_liquidated")) => json!(change),
            _ => public["tradeChangeType"].clone(),
        };
        let indexed = keeper::truthy(self.indexed);
        let execution = outcome.has_execution().then(|| {
            let extra_effects: Vec<_> = self.matching.iter().copied()
                .filter(|item| !ptr::eq(*item, self.indexed) && item["tradeChangeType"] == "after_hours_risk_trigger")
                .filter_map(Value::as_object)
                .map(|row| project(keeper::intent_history(row)))
                .map(|extra| json!({
                    "tradeChangeType": extra["tradeChangeType"],
                    "historyId": extra["id"],
                    "block": extra["block"]["block"],
                }))
                .collect();
            json!({
                "source": if indexed { "trade_history" } else { "keeper_intent" },
                "block": first_truthy([&public["block"]["block"], &number(&order["orderExecBlock"]), &intent["execBlock"]]),
                "blockTs": first_truthy([&public["block"]["block_ts"], if indexed { &Value::Null } else { &intent["execBlockTs"] }]),
                "historyId": public["id"],
                "tradeChangeType": change,
                "marketOrderSequence": number(&order["sequence"]),
                "extraEffects": extra_effects,
            })
        });
        let usdc = usdc_collateral(&self.indexed["trade"]);
        let fill = (outcome == Outcome::Filled).then(|| {
            let size = fill_values.size.map_or(Value::Null, |size| json!(size));
            let escrow = number(&order["collateralAmount"]);
            let mut fill = json!({
                "price": fill_values.price,
                "sizeCollateralBaseUnits": size,
                "sizeCollateralUsd": usdc_usd(&size, usdc),
                "oraclePrice": number(&order["execPrice"]),
                "escrowedCollateralBaseUnits": escrow,
                "escrowedCollateralUsd": usdc_usd(&escrow, usdc),
            });
            // Only closing fills realize PnL. Keeper reports none for a
            // partial close, so its value stays null.
            if matches!(
                self.indexed["tradeChangeType"].as_str(),
                Some("position_closed_user" | "trade_size_decrease")
            ) {
                fill["realizedPnl"] = fill_values.realized_pnl;
            }
            fill
        });
        let fees = [
            ("opening", number(&self.indexed["openingFeeUsd"])),
            ("closing", number(&self.indexed["closingFeeUsd"])),
            ("borrowing", number(&self.indexed["borrowingFeeUsd"])),
        ];
        let fees = fees
            .iter()
            .any(|(_, value)| !value.is_null())
            .then(|| Map::from_iter(fees.map(|(key, value)| (key.into(), value))));
        let mut receipt = Map::from_iter([
            (
                "requestTxHash".into(),
                json!(
                    keeper::request_hash(request_hash)
                        .0
                        .or_else(|| keeper::request_hash(&order["orderReqTxHash"]).0)
                        .or_else(|| public["txHash"].as_str().map(str::to_owned))
                ),
            ),
            (
                "requestBlock".into(),
                first_truthy([&number(request_block), &number(&order["orderReqBlock"])]),
            ),
            (
                "action".into(),
                json!({"kind": intent["kind"], "orderType": self.order_type}),
            ),
        ]);
        // `sai trade cancel ID` takes the ID of a placed limit or stop order.
        if outcome == Outcome::Placed {
            receipt.insert("orderId".into(), public["trade"]["id"].clone());
        }
        receipt.extend(
            json!({
                "execution": execution,
                "fill": fill,
                "positionNow": position(&self.indexed["trade"], &public["trade"]),
                "positionFeesUsd": fees,
            })
            .as_object()
            .cloned()
            .unwrap_or_default(),
        );
        project(Value::Object(receipt))
    }
}

fn opening_size(trade: &Value, requested_leverage: &Value) -> Option<i64> {
    let collateral = trade["collateralAmount"]
        .as_i64()
        .filter(|value| *value > 0)?;
    let leverage = number(&trade["leverage"]);
    let indexed = decimal_float(&leverage).filter(|value| *value > 0.0)?;
    let requested = number(requested_leverage);
    let decimal = requested.as_str()?;
    if decimal_float(&requested) != Some(indexed) {
        return None;
    }
    // The intent preserves the exact decimal. GraphQL Float rounding could
    // otherwise change the size when flooring to whole collateral base units.
    let leverage: BigDecimal = decimal.parse().ok()?;
    (leverage * BigDecimal::from(collateral))
        .to_i64()
        .filter(|size| *size > 0)
}

/// Read a validated receipt number exactly, as GraphQL Float text.
fn decimal(value: &Value) -> Option<BigDecimal> {
    match value {
        Value::String(text) => text.parse().ok(),
        Value::Number(number) => number.to_string().parse().ok(),
        _ => None,
    }
}

/// Write a decimal without an exponent or trailing fractional zeros.
fn plain(value: &BigDecimal) -> String {
    let text = value.normalized().to_plain_string();
    if text == "-0" { "0".into() } else { text }
}

fn decimal_float(value: &Value) -> Option<f64> {
    match value {
        Value::String(text) => text.parse().ok(),
        _ => value.as_f64(),
    }
}

fn first_truthy<const N: usize>(values: [&Value; N]) -> Value {
    values
        .iter()
        .copied()
        .find(|value| keeper::truthy(value))
        .unwrap_or(values[N - 1])
        .clone()
}

fn order_type(row: &Value, indexed: &Value) -> Option<&'static str> {
    if row["kind"] == "OPEN_TRADE" {
        match row["body"]["orderType"].as_str() {
            Some("market") => return Some("market"),
            Some("limit") => return Some("limit"),
            Some("stop") => return Some("stop"),
            _ => {}
        }
    }
    if row["kind"] == "OPEN_TRADE" || row["kind"].is_null() {
        return match indexed["tradeChangeType"].as_str() {
            Some("limit_order_created") => Some("limit"),
            Some("stop_order_created") => Some("stop"),
            _ => None,
        };
    }
    None
}

fn matches_action(row: &Value, order_type: Option<&str>, change: &Value) -> bool {
    let Some(change) = change.as_str() else {
        return false;
    };
    let body = &row["body"];
    let requests_update =
        |field: &str| matches!(body[field]["operation"].as_str(), Some("set" | "clear"));
    match row["kind"].as_str() {
        Some("OPEN_TRADE") => {
            change
                == match order_type {
                    Some("limit") => "limit_order_created",
                    Some("stop") => "stop_order_created",
                    _ => "position_opened",
                }
        }
        None if row["kind"].is_null() => {
            TRADE_FILLS.contains(&change)
                || matches!(change, "limit_order_created" | "stop_order_created")
        }
        Some("CLOSE_POSITION") => change == "position_closed_user",
        Some("INCREASE_POSITION_SIZE") => change == "trade_size_increase",
        Some("DECREASE_POSITION_SIZE") => change == "trade_size_decrease",
        Some("CANCEL_PENDING_TRADE") => change == "order_closed_user",
        Some("UPDATE_OPEN_ORDER") => change == "order_updated",
        Some("UPDATE_TRADE") if body.is_object() => match change {
            "leverage_updated" => !body["requestedLeverage"].is_null(),
            "tp_updated" => requests_update("takeProfit"),
            "sl_updated" => requests_update("stopLoss"),
            "tpsl_updated" => requests_update("takeProfit") || requests_update("stopLoss"),
            _ => false,
        },
        Some("UPDATE_TRADE") => matches!(
            change,
            "tp_updated" | "sl_updated" | "tpsl_updated" | "leverage_updated"
        ),
        _ => false,
    }
}

fn number(value: &Value) -> Value {
    static DECIMAL: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\A-?[0-9]{1,24}(?:\.[0-9]{1,24})?\z").expect("receipt decimal pattern")
    });
    let valid = match value {
        Value::Number(value) => {
            value.as_i64().is_some()
                || (value.as_str().contains(['.', 'e', 'E'])
                    && value.as_f64().is_some_and(f64::is_finite))
        }
        Value::String(text) => DECIMAL.is_match(text),
        _ => false,
    };
    if valid { value.clone() } else { Value::Null }
}

fn position(trade: &Value, public: &Value) -> Option<Map<String, Value>> {
    let usdc = usdc_collateral(trade);
    let trade = trade.as_object()?;
    let mut result = public.as_object().cloned().unwrap_or_default();
    for field in ["isOpen", "isLong"] {
        if let Some(value) = trade.get(field).and_then(Value::as_bool) {
            result.insert(field.into(), json!(value));
        }
    }
    if let Some(kind @ ("trade" | "limit" | "stop")) =
        trade.get("tradeType").and_then(Value::as_str)
    {
        result.insert("tradeType".into(), json!(kind));
    }
    for (field, usd) in [
        ("collateralAmount", "collateralUsd"),
        ("openCollateralAmount", "openCollateralUsd"),
    ] {
        if let Some(value) = trade.get(field) {
            let units = number(value);
            let dollars = usdc_usd(&units, usdc);
            result.insert(field.into(), units);
            result.insert(usd.into(), dollars);
        }
    }
    for field in ["leverage", "openPrice", "closePrice", "tp", "sl"] {
        if let Some(value) = trade.get(field) {
            result.insert(field.into(), number(value));
        }
    }
    for field in ["openBlock", "closeBlock"] {
        if let Some(value) = trade.get(field) {
            result.insert(
                field.into(),
                if value.is_object() {
                    json!({"block": number(&value["block"])})
                } else {
                    Value::Null
                },
            );
        }
    }
    if let Some(borrowing) = trade.get("perpBorrowing").filter(|value| value.is_object()) {
        let mut market = json!({"marketId": number(&borrowing["marketId"])});
        if let Some(symbol) = borrowing["collateralToken"]["symbol"].as_str()
            && (1..=12).contains(&symbol.len())
            && symbol
                .bytes()
                .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit())
        {
            market["collateralToken"] = json!({"symbol": symbol});
        }
        result.insert("perpBorrowing".into(), market);
    }
    Some(result)
}

/// Report whether a position's collateral is USDC, or unknown and so the
/// USDC every CLI order escrows.
fn usdc_collateral(trade: &Value) -> bool {
    match &trade["perpBorrowing"]["collateralToken"]["symbol"] {
        Value::String(symbol) => symbol == "USDC",
        _ => true,
    }
}

/// Read USDC base units as dollars, or null for other collateral.
fn usdc_usd(units: &Value, usdc: bool) -> Value {
    usdc.then(|| usdc::base_units_usd(units))
        .flatten()
        .map_or(Value::Null, Value::String)
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::{usdc_collateral, usdc_usd};

    #[test]
    fn receipts_read_usdc_base_units_as_dollars() {
        assert_eq!(usdc_usd(&json!(3_000_000), true), json!("3"));
        assert_eq!(usdc_usd(&json!("2997450"), true), json!("2.99745"));
        assert_eq!(usdc_usd(&Value::Null, true), Value::Null);
        assert_eq!(usdc_usd(&json!(3_000_000), false), Value::Null);
    }

    #[test]
    fn only_known_non_usdc_collateral_drops_the_dollar_value() {
        let trade =
            |symbol: Value| json!({"perpBorrowing": {"collateralToken": {"symbol": symbol}}});
        assert!(usdc_collateral(&trade(json!("USDC"))));
        assert!(usdc_collateral(&json!({})));
        assert!(!usdc_collateral(&trade(json!("stNIBI"))));
    }
}
