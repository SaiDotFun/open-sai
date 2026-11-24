//! Project the Keeper evidence for a waited intent.

use std::{ptr, sync::LazyLock};

use regex::Regex;
use serde_json::{Map, Value, json};

use crate::{keeper, wait::Outcome};

const TRADE_FILLS: &[&str] = &[
    "position_opened",
    "position_closed_user",
    "trade_size_increase",
    "trade_size_decrease",
];

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

    pub fn build(
        &self,
        outcome: Outcome,
        request_hash: &Value,
        request_block: &Value,
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
        let fill = (outcome == Outcome::Filled).then(|| {
            json!({
                "price": null,
                "sizeCollateralBaseUnits": null,
                "oraclePrice": number(&order["execPrice"]),
                "escrowedCollateralBaseUnits": number(&order["collateralAmount"]),
            })
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
        project(json!({
            "requestTxHash": keeper::request_hash(request_hash).0
                .or_else(|| keeper::request_hash(&order["orderReqTxHash"]).0)
                .or_else(|| public["txHash"].as_str().map(str::to_owned)),
            "requestBlock": first_truthy([&number(request_block), &number(&order["orderReqBlock"])]),
            "action": {"kind": intent["kind"], "orderType": self.order_type},
            "execution": execution,
            "fill": fill,
            "positionNow": position(&self.indexed["trade"], &public["trade"]),
            "positionFeesUsd": fees,
        }))
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
    for field in [
        "collateralAmount",
        "openCollateralAmount",
        "leverage",
        "openPrice",
        "closePrice",
        "tp",
        "sl",
    ] {
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
