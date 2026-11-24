//! Public intent records use the same field allowlists as the Python Keeper client.

use std::{path::Path, sync::LazyLock};

use chrono::{DateTime, Datelike, Timelike};
use regex::Regex;
use serde_json::{Map, Value, json};

use super::{canonical_account, is_evm_address, truthy};
use crate::{
    log,
    reconcile::{HISTORY_HINT, LOCAL_PENDING_HINT, PENDING_HINT, UNINDEXED_HINT},
    session::parse_expiry,
};

static INTENT_KEY: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A[0-9]{8}T[0-9]{9}Z_[0-9A-HJKMNP-TV-Z]{16}\z").expect("intent key pattern")
});
static ROW_ID: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A[0-9]{1,24}(?:[-_:][0-9]{1,24}){0,3}\z").expect("row ID pattern")
});
static BLOCK_TIMESTAMP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(concat!(
        r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}",
        r"(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\z"
    ))
    .expect("Keeper block timestamp pattern")
});

static HISTORY_TIMESTAMP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(concat!(
        r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-5][0-9]",
        r"(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\z"
    ))
    .expect("history timestamp pattern")
});

const KINDS: &[&str] = &[
    "OPEN_TRADE",
    "CLOSE_POSITION",
    "CANCEL_PENDING_TRADE",
    "UPDATE_TRADE",
    "BRIDGE_USD",
    "DEPOSIT_USD",
    "WITHDRAW_USD",
    "UPDATE_OPEN_ORDER",
    "DECREASE_POSITION_SIZE",
    "INCREASE_POSITION_SIZE",
];
const STEPS: &[&str] = &[
    "jwt_pass",
    "jwt_failed",
    "intent_start",
    "sign_reject",
    "sign_failed",
    "exec_req_pass",
    "exec_req_fail",
    "pending_exec",
    "exec_pass",
    "exec_fail",
    "exec_finality_observed",
];
const TRADE_CHANGES: &[&str] = &[
    "position_opened",
    "limit_order_created",
    "stop_order_created",
    "order_triggered",
    "position_closed_tp",
    "position_closed_sl",
    "position_liquidated",
    "position_closed_user",
    "order_closed_user",
    "tp_updated",
    "sl_updated",
    "tpsl_updated",
    "trade_size_increase",
    "trade_size_decrease",
    "leverage_updated",
    "after_hours_risk_trigger",
];
const ERROR_CODES: &[&str] = &[
    "SLIPPAGE",
    "simulation_failed",
    "admission_expired",
    "broadcast_rejected",
    "chain_execution_failed",
    "market_order_expired",
    "market_order_fill_failed",
    "market_order_trading_paused",
    "market_order_market_closed",
    "market_order_trade_not_open",
    "market_order_outcome_not_indexed",
    "intent_already_used",
    "below_minimum",
    "insufficient_idle",
];
const LOG_ERROR_CODES: &[&str] = &[
    "withdraw_evm_unconfigured",
    "invalid_withdraw",
    "intent_validation_failed",
    "chain_switch_failed",
    "gas_estimation_failed",
    "wallet_rejected",
    "wallet_sign_failed",
    "chain_mismatch",
    "request_pending",
    "rpc_submission_failed",
    "sign_failed_unknown",
    "intent_auth_rejected",
    "jwt_failed",
];

/// Project the reconciled output once, after raw Keeper values have served their
/// purpose for correlation and ledger writes. Scrub every string that survives.
pub fn public_intent_status(home: &Path, data: &Map<String, Value>) -> Value {
    let mut result = Map::new();
    reconcile_fields(&mut result, data);
    for (name, project) in [
        (
            "userIntents",
            intent_record as fn(&Map<String, Value>) -> Value,
        ),
        ("matchedTradeHistory", intent_history),
    ] {
        if let Some(rows) = data.get(name).and_then(Value::as_array) {
            result.insert(
                name.into(),
                Value::Array(
                    rows.iter()
                        .filter_map(Value::as_object)
                        .map(project)
                        .collect(),
                ),
            );
        }
    }
    log::scrubber(home)(Value::Object(result).into())
}

/// Project Keeper's public account-history rows inside their GraphQL envelope.
pub fn public_account_history(home: &Path, data: &Map<String, Value>) -> Value {
    let perp = match data.get("perp") {
        Some(Value::Null) => return json!({"perp": null}),
        Some(Value::Object(perp)) => perp,
        _ => return json!({}),
    };
    let rows = match perp.get("tradeHistory") {
        Some(Value::Null) => return json!({"perp": {"tradeHistory": null}}),
        Some(Value::Array(rows)) => rows,
        _ => return json!({"perp": {}}),
    };

    let scrub = log::scrubber(home);
    let projected: Vec<_> = rows
        .iter()
        .filter_map(Value::as_object)
        .map(|row| scrub(history_row(row, account_history_timestamp).into()))
        .collect();
    json!({"perp": {"tradeHistory": projected}})
}

fn account_history_timestamp(block: &mut Map<String, Value>, timestamp: &Value) {
    if timestamp.as_str().is_some_and(|text| {
        HISTORY_TIMESTAMP.is_match(text)
            && !text.starts_with("0000-")
            && chrono::DateTime::parse_from_rfc3339(text).is_ok()
    }) {
        block.insert("block_ts".into(), timestamp.clone());
    } else if !timestamp.is_null() {
        block.insert("block_tsInvalid".into(), json!(true));
    }
}

fn keep(out: &mut Map<String, Value>, key: &str, value: &Value, valid: bool) {
    if valid {
        out.insert(key.into(), value.clone());
    }
}

fn known(value: &Value, allowed: &[&str]) -> bool {
    value.as_str().is_some_and(|text| allowed.contains(&text))
}

fn number(value: &Value) -> bool {
    value.as_i64().is_some_and(|value| value >= 0)
}

fn row_id(value: &Value) -> bool {
    number(value) || value.as_str().is_some_and(|text| ROW_ID.is_match(text))
}

fn block_time(value: &Value) -> bool {
    value
        .as_str()
        .is_some_and(|text| text.chars().count() <= 40 && parse_expiry(text).is_some())
}

fn keep_hash(out: &mut Map<String, Value>, key: &str, value: &Value) {
    let (hash, invalid) = request_hash(value);
    if let Some(hash) = hash {
        out.insert(key.into(), json!(hash));
    }
    if invalid {
        out.insert(format!("{key}Invalid"), json!(true));
    }
}

pub(crate) fn request_hash(value: &Value) -> (Option<String>, bool) {
    if value.is_null() {
        return (None, false);
    }
    if let Some(hash) = value.as_str() {
        let digits = hash
            .strip_prefix("0x")
            .or_else(|| hash.strip_prefix("0X"))
            .unwrap_or(hash);
        if digits.len() == 64 && digits.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return (Some(hash.to_ascii_lowercase()), false);
        }
    }
    (None, true)
}

pub(crate) fn known_intent_field(field: &str, value: &Value) -> bool {
    match field {
        "kind" => known(value, KINDS),
        "lifecycleStep" => known(value, STEPS),
        "execActor" => known(value, &["client", "server", "user"]),
        "errorCode" => {
            known(value, ERROR_CODES) || known(value, LOG_ERROR_CODES) || value == "KEEPER_ERROR"
        }
        _ => false,
    }
}

fn reconcile_fields(out: &mut Map<String, Value>, source: &Map<String, Value>) {
    for (key, allowed) in [
        (
            "outcome",
            &[
                "accepted",
                "pending",
                "terminal-filled",
                "terminal-failed",
                "unknown",
            ][..],
        ),
        (
            "localStatus",
            &[
                "unknown", "accepted", "pending", "terminal", "rejected", "not_sent", "absent",
            ],
        ),
        (
            "hint",
            &[
                PENDING_HINT,
                UNINDEXED_HINT,
                HISTORY_HINT,
                LOCAL_PENDING_HINT,
            ],
        ),
    ] {
        if let Some(value) = source.get(key) {
            keep(out, key, value, known(value, allowed));
        }
    }
}

fn keep_intent_key(out: &mut Map<String, Value>, key: &Value) {
    keep(
        out,
        "intentKey",
        key,
        key.as_str().is_some_and(|text| INTENT_KEY.is_match(text)),
    );
}

fn intent_record(row: &Map<String, Value>) -> Value {
    project_intent_record(row, false)
}

pub(crate) fn log_intent_record(row: &Map<String, Value>) -> Value {
    project_intent_record(row, true)
}

fn project_intent_record(row: &Map<String, Value>, logging: bool) -> Value {
    let value = |key| row.get(key).unwrap_or(&Value::Null);
    let mut record = Map::new();
    keep_intent_key(&mut record, value("intentKey"));
    if let Some(address) = value("evmAddress")
        .as_str()
        .filter(|address| is_evm_address(address))
    {
        record.insert("evmAddress".into(), json!(address));
        let nibi = value("nibiAddress");
        keep(
            &mut record,
            "nibiAddress",
            nibi,
            nibi.as_str().is_some_and(|nibi| {
                nibi.starts_with("nibi1")
                    && canonical_account(nibi)
                        .is_some_and(|account| account.eq_ignore_ascii_case(address))
            }),
        );
    }
    for (key, allowed) in [
        ("kind", KINDS),
        ("lifecycleStep", STEPS),
        ("execActor", &["client", "server", "user"][..]),
    ] {
        let field = value(key);
        keep(&mut record, key, field, known(field, allowed));
    }
    keep(
        &mut record,
        "execBlock",
        value("execBlock"),
        number(value("execBlock")),
    );
    let timestamp = value("execBlockTs");
    let valid = if logging {
        timestamp.as_str().is_some_and(|ts| {
            BLOCK_TIMESTAMP.is_match(ts)
                && DateTime::parse_from_rfc3339(ts)
                    .is_ok_and(|ts| ts.year() > 0 && ts.nanosecond() < 1_000_000_000)
        })
    } else {
        block_time(timestamp)
    };
    keep(&mut record, "execBlockTs", timestamp, valid);
    if logging && !timestamp.is_null() && !valid {
        record.insert("execBlockTsInvalid".into(), json!(true));
    }
    keep_hash(&mut record, "txHash", value("txHash"));
    if let Some(code) = error_code(row, logging) {
        record.insert("errorCode".into(), json!(code));
        // This fixed message is shorter than the untrusted-text limit.
        record.insert("errorMsg".into(), json!("Keeper reported an intent error."));
    }
    if !logging {
        reconcile_fields(&mut record, row);
    }
    Value::Object(record)
}

pub(crate) fn log_intent_error_code(row: &Map<String, Value>) -> Option<&str> {
    error_code(row, true)
}

fn error_code(row: &Map<String, Value>, logging: bool) -> Option<&str> {
    let value = |key| row.get(key).unwrap_or(&Value::Null);
    let code = value("errorCode");
    let failed = truthy(code)
        || truthy(value("errorMsg"))
        || known(
            value("lifecycleStep"),
            &[
                "jwt_failed",
                "sign_reject",
                "sign_failed",
                "exec_req_fail",
                "exec_fail",
            ],
        );
    failed.then(|| {
        code.as_str()
            .filter(|code| {
                ERROR_CODES.contains(code) || (logging && LOG_ERROR_CODES.contains(code))
            })
            .unwrap_or("KEEPER_ERROR")
    })
}

fn intent_history(row: &Map<String, Value>) -> Value {
    history_row(row, |block, timestamp| {
        keep(block, "block_ts", timestamp, block_time(timestamp));
    })
}

fn history_row(
    row: &Map<String, Value>,
    project_timestamp: impl FnOnce(&mut Map<String, Value>, &Value),
) -> Value {
    let value = |key| row.get(key).unwrap_or(&Value::Null);
    let mut history = Map::new();
    keep_intent_key(&mut history, value("intentKey"));
    keep(&mut history, "id", value("id"), row_id(value("id")));
    keep(
        &mut history,
        "tradeChangeType",
        value("tradeChangeType"),
        known(value("tradeChangeType"), TRADE_CHANGES),
    );
    for key in ["txHash", "evmTxHash"] {
        keep_hash(&mut history, key, value(key));
    }
    if let Some(trade) = value("trade").as_object() {
        let mut public = Map::new();
        if let Some(id) = trade.get("id") {
            keep(&mut public, "id", id, row_id(id));
        }
        if let Some(trader) = trade.get("trader") {
            keep(
                &mut public,
                "trader",
                trader,
                trader.as_str().and_then(canonical_account).is_some(),
            );
        }
        history.insert("trade".into(), Value::Object(public));
    }
    if let Some(block) = value("block").as_object() {
        let mut public = Map::new();
        if let Some(height) = block.get("block") {
            keep(&mut public, "block", height, row_id(height));
        }
        if let Some(timestamp) = block.get("block_ts") {
            project_timestamp(&mut public, timestamp);
        }
        history.insert("block".into(), Value::Object(public));
    }
    Value::Object(history)
}
