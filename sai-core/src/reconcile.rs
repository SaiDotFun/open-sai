//! Reconcile local intent rows against Keeper's authoritative outcomes.

use std::time::Duration;

use serde_json::{Map, Value, json};

use crate::{
    errors::{Code, SaiError},
    keeper::{self, Keeper, canonical_account, truthy},
    log::{EventContext, Field, Level},
    storage::{IntentStatus, StatusUpdateOptions, Storage},
};

const OUTCOME_NOT_INDEXED: &str = "market_order_outcome_not_indexed";
const HISTORY_TIMEOUT: Duration = Duration::from_secs(2);
const HISTORY_RETRY_DELAY: Duration = Duration::from_millis(250);
pub(crate) const PENDING_HINT: &str = "Fill pending at the next oracle price. Do not resubmit.";
pub(crate) const UNINDEXED_HINT: &str =
    "The order may have filled. Check positions and run intent status again. Do not resubmit.";
pub(crate) const HISTORY_HINT: &str = "Waiting for matching trade history. Do not resubmit.";
pub(crate) const LOCAL_PENDING_HINT: &str =
    "This intent is still pending locally. Do not resubmit.";

#[derive(Clone, Copy, Eq, PartialEq)]
pub enum Outcome {
    Accepted,
    Pending,
    TerminalFilled,
    TerminalFailed,
    Unknown,
}

fn perp_history(kind: &Value) -> bool {
    matches!(
        kind.as_str(),
        Some(
            "OPEN_TRADE"
                | "CLOSE_POSITION"
                | "CANCEL_PENDING_TRADE"
                | "UPDATE_TRADE"
                | "UPDATE_OPEN_ORDER"
                | "INCREASE_POSITION_SIZE"
                | "DECREASE_POSITION_SIZE"
        )
    )
}

pub fn classify_outcome(row: &Value) -> Outcome {
    match (row["execActor"].as_str(), row["lifecycleStep"].as_str()) {
        (Some("server" | "user"), Some("exec_finality_observed")) if perp_history(&row["kind"]) => {
            Outcome::Pending
        }
        (Some("server"), Some("pending_exec")) => Outcome::Pending,
        (Some("server"), Some("exec_fail" | "exec_req_fail")) => {
            if row["errorCode"] == OUTCOME_NOT_INDEXED {
                Outcome::Pending
            } else {
                Outcome::TerminalFailed
            }
        }
        (Some("server"), Some("exec_pass")) => Outcome::TerminalFilled,
        (Some("server"), Some("jwt_pass" | "exec_req_pass")) => Outcome::Accepted,
        _ => Outcome::Unknown,
    }
}

fn explicit_market_pending(row: &Value) -> bool {
    row["lifecycleStep"] == "pending_exec"
        || (row["errorCode"] == OUTCOME_NOT_INDEXED
            && matches!(
                row["lifecycleStep"].as_str(),
                Some("exec_fail" | "exec_req_fail")
            ))
}

fn pending_hint(row: &Value) -> &'static str {
    if row["lifecycleStep"] == "pending_exec" {
        PENDING_HINT
    } else if row["errorCode"] == OUTCOME_NOT_INDEXED {
        UNINDEXED_HINT
    } else {
        HISTORY_HINT
    }
}

fn account_matches(value: &Value, account: &str) -> bool {
    value.as_str().and_then(canonical_account).as_deref() == Some(account)
}

/// Query Keeper with the requested account verbatim. Scope ledger writes to its
/// canonical address and use the C3 transaction rules for every transition.
pub async fn reconcile(
    endpoint: &str,
    addr: &str,
    key: Option<&str>,
    timeout: Duration,
    storage: &Storage<'_>,
    events: EventContext<'_>,
) -> Result<Map<String, Value>, SaiError> {
    let keeper = Keeper::new(endpoint, events);
    let mut data = keeper
        .request(
            keeper::INTENTS,
            Map::from_iter([("user".into(), json!(addr)), ("key".into(), json!(key))]),
            None,
            timeout,
        )
        .await?;
    let Some(rows) = data.get_mut("userIntents").filter(|rows| !rows.is_null()) else {
        return Ok(data);
    };
    let absent = rows.as_array().is_some_and(Vec::is_empty);
    let account = canonical_account(addr);
    let original = rows.take();
    let mut matching = Vec::new();
    match (original, &account) {
        (Value::Array(items), Some(account)) => {
            for row in items {
                if !row.is_object() {
                    return Err(SaiError::internal());
                }
                if account_matches(&row["evmAddress"], account)
                    && (row["nibiAddress"].is_null()
                        || account_matches(&row["nibiAddress"], account))
                    && key.is_none_or(|key| row["intentKey"] == key)
                {
                    matching.push(row);
                }
            }
        }
        (Value::Array(_), None) => {}
        (other, _) if !truthy(&other) => {}
        (Value::String(_) | Value::Object(_), None) => {}
        (Value::String(_) | Value::Object(_), Some(_)) => return Err(SaiError::internal()),
        _ => return Err(incomplete_data("TypeError")),
    }
    let Some(account) = account else {
        *rows = Value::Array(matching);
        return Ok(data);
    };
    let Some(key) = key else {
        reconcile_account(storage, endpoint, &account, &mut matching)?;
        *rows = Value::Array(matching);
        return Ok(data);
    };
    *rows = Value::Array(matching);

    if absent {
        log_keeper(events, endpoint, addr, key, None);
        let local =
            storage.mark_absent(key, endpoint, &account, keeper::ABSENT_AFTER_SECONDS, None)?;
        if let Some(status) = &local {
            data.insert("localStatus".into(), json!(status));
        }
        if local.as_deref() == Some("pending") {
            set_pending(&mut data, LOCAL_PENDING_HINT);
        }
    } else if let Some(row) = data["userIntents"].get(0).cloned() {
        let outcome = classify_outcome(&row);
        log_keeper(events, endpoint, addr, key, Some((&row, outcome)));
        if outcome != Outcome::Unknown {
            let context = KeyedIntent {
                storage,
                keeper: &keeper,
                addr,
                account: &account,
                key,
                timeout,
            };
            reconcile_key(&context, &row, outcome, &mut data).await?;
        }
    }
    Ok(data)
}

fn reconcile_account(
    storage: &Storage<'_>,
    endpoint: &str,
    account: &str,
    matching: &mut [Value],
) -> Result<(), SaiError> {
    for row in matching {
        let outcome = classify_outcome(row);
        if !matches!(outcome, Outcome::Pending | Outcome::Accepted) || !truthy(&row["intentKey"]) {
            continue;
        }
        let key = row["intentKey"].as_str().ok_or_else(SaiError::internal)?;
        let local = store_outcome(
            storage,
            endpoint,
            account,
            key,
            row,
            outcome,
            outcome == Outcome::Accepted,
        )?;
        if local.as_deref() == Some("pending")
            || (local.is_none() && outcome == Outcome::Pending && explicit_market_pending(row))
        {
            let hint = if outcome == Outcome::Pending {
                pending_hint(row)
            } else {
                LOCAL_PENDING_HINT
            };
            set_pending(row.as_object_mut().expect("matched intent object"), hint);
        }
    }
    Ok(())
}

struct KeyedIntent<'a> {
    storage: &'a Storage<'a>,
    keeper: &'a Keeper<'a>,
    addr: &'a str,
    account: &'a str,
    key: &'a str,
    timeout: Duration,
}

async fn reconcile_key(
    context: &KeyedIntent<'_>,
    row: &Value,
    mut outcome: Outcome,
    data: &mut Map<String, Value>,
) -> Result<(), SaiError> {
    let store = |outcome| {
        store_outcome(
            context.storage,
            context.keeper.endpoint(),
            context.account,
            context.key,
            row,
            outcome,
            false,
        )
    };
    let mut local = store(outcome)?;
    if (outcome == Outcome::TerminalFilled && (row["kind"].is_null() || perp_history(&row["kind"])))
        || (outcome == Outcome::Pending && row["lifecycleStep"] != "pending_exec")
    {
        let matches = read_history(context, row).await?;
        let established_fill = !matches.is_empty() && outcome == Outcome::Pending;
        data.insert("matchedTradeHistory".into(), json!(matches));
        if established_fill {
            outcome = Outcome::TerminalFilled;
            local = store(outcome)?;
            data.insert("outcome".into(), json!("terminal-filled"));
            if let Some(status) = &local {
                data.insert("localStatus".into(), json!(status));
            }
        }
    }
    if outcome == Outcome::Pending || local.as_deref() == Some("pending") {
        if let Some(status) = &local {
            data.insert("localStatus".into(), json!(status));
        }
        if local.is_none() || local.as_deref() == Some("pending") {
            let hint = if outcome == Outcome::Pending {
                pending_hint(row)
            } else {
                LOCAL_PENDING_HINT
            };
            set_pending(data, hint);
        }
    }
    Ok(())
}

fn set_pending(data: &mut Map<String, Value>, hint: &str) {
    data.insert("outcome".into(), json!("pending"));
    data.insert("hint".into(), json!(hint));
}

fn store_outcome(
    storage: &Storage<'_>,
    endpoint: &str,
    account: &str,
    key: &str,
    row: &Value,
    outcome: Outcome,
    only_if_absent: bool,
) -> Result<Option<String>, SaiError> {
    let status = match outcome {
        Outcome::Accepted => IntentStatus::Accepted,
        Outcome::Pending => IntentStatus::Pending,
        Outcome::TerminalFailed | Outcome::TerminalFilled => IntentStatus::Terminal,
        Outcome::Unknown => return Ok(None),
    };
    Ok(storage.set_status_with(
        key,
        endpoint,
        account,
        status,
        StatusUpdateOptions {
            repair_legacy_terminal: outcome == Outcome::Pending && explicit_market_pending(row),
            only_if_absent,
        },
    )?)
}

fn normalize_hash(value: &Value) -> Result<String, SaiError> {
    if !truthy(value) {
        return Ok(String::new());
    }
    let lower = value
        .as_str()
        .ok_or_else(SaiError::internal)?
        .to_lowercase();
    Ok(lower.strip_prefix("0x").unwrap_or(&lower).into())
}

async fn read_history(context: &KeyedIntent<'_>, row: &Value) -> Result<Vec<Value>, SaiError> {
    let hash = normalize_hash(&row["txHash"])?;
    if hash.is_empty() {
        return Ok(Vec::new());
    }
    for attempt in 0..2 {
        if attempt > 0 {
            tokio::time::sleep(HISTORY_RETRY_DELAY).await;
        }
        let Ok(data) = context
            .keeper
            .request(
                keeper::HISTORY,
                Map::from_iter([("trader".into(), json!(context.addr))]),
                None,
                context.timeout.min(HISTORY_TIMEOUT),
            )
            .await
        else {
            break;
        };
        let perp = data
            .get("perp")
            .ok_or_else(|| incomplete_data("KeyError"))?;
        let history = perp
            .as_object()
            .ok_or_else(|| incomplete_data("TypeError"))?
            .get("tradeHistory")
            .ok_or_else(|| incomplete_data("KeyError"))?;
        let history = match history {
            Value::Array(items) => items,
            // Python iterates these containers. An empty one yields no matches.
            Value::Object(_) | Value::String(_) if !truthy(history) => continue,
            Value::Object(_) | Value::String(_) => return Err(SaiError::internal()),
            _ => return Err(incomplete_data("TypeError")),
        };
        let mut matches = Vec::new();
        for item in history {
            if !item.is_object() {
                return Err(SaiError::internal());
            }
            if item["intentKey"] == context.key
                && account_matches(&item["trade"]["trader"], context.account)
            {
                let tx_hash = normalize_hash(&item["txHash"])?;
                let evm_tx_hash = normalize_hash(&item["evmTxHash"])?;
                if hash == tx_hash || hash == evm_tx_hash {
                    matches.push(item.clone());
                }
            }
        }
        if !matches.is_empty() {
            return Ok(matches);
        }
    }
    Ok(Vec::new())
}

fn incomplete_data(kind: &str) -> SaiError {
    SaiError::new(
        Code::IncompleteData,
        format!("Keeper or contract data was missing or malformed ({kind})"),
    )
}

fn log_keeper(
    events: EventContext<'_>,
    endpoint: &str,
    addr: &str,
    key: &str,
    observed: Option<(&Value, Outcome)>,
) {
    let mut fields = vec![
        ("intentKey", json!(key).into()),
        ("addr", json!(addr).into()),
        ("endpoint", Field::Endpoint(endpoint.into())),
    ];
    if let Some((row, outcome)) = observed {
        for key in ["kind", "lifecycleStep", "execActor"] {
            if keeper::known_intent_field(key, &row[key]) {
                fields.push((key, row[key].clone().into()));
            }
        }
        if let Some(code) = row.as_object().and_then(keeper::log_intent_error_code) {
            fields.push(("errorCode", json!(code).into()));
        }
        if let Some(hash) = keeper::request_hash(&row["txHash"]) {
            fields.push(("txHash", json!(hash).into()));
        } else if !row["txHash"].is_null() {
            fields.push(("txHashInvalid", json!(true).into()));
        }
        if outcome == Outcome::TerminalFailed {
            fields.push(("code", json!(Code::KeeperError).into()));
        }
    } else {
        fields.push(("found", json!(false).into()));
    }
    events.event("intent.keeper", Level::Info, fields);
}
