//! Reconcile local intent rows against Keeper's authoritative outcomes.

use std::{
    collections::HashSet,
    time::{Duration, Instant},
};

use serde_json::{Map, Value, json};

use crate::{
    errors::{Code, SaiError},
    keeper::{self, Keeper, canonical_account, stringify, truthy},
    log::{EventContext, Field, Level},
    storage::{IntentStatus, StatusUpdateOptions, Storage, StorageError},
};

const OUTCOME_NOT_INDEXED: &str = "market_order_outcome_not_indexed";
const HISTORY_TIMEOUT: Duration = Duration::from_secs(2);
const HISTORY_RETRY_DELAY: Duration = Duration::from_millis(250);
const HISTORY_PAGE_SIZE: usize = 25;
const HISTORY_MAX_PAGES: usize = 4;
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

#[derive(Clone, Copy)]
pub(crate) struct WaitBudget {
    pub started: Instant,
    pub timeout: Duration,
}

impl WaitBudget {
    pub fn remaining(self) -> Duration {
        self.timeout.saturating_sub(self.started.elapsed())
    }
}

pub(crate) enum PollError {
    Keeper(SaiError),
    Data(SaiError),
    Storage(StorageError),
    Deadline,
}

impl From<StorageError> for PollError {
    fn from(error: StorageError) -> Self {
        Self::Storage(error)
    }
}

#[derive(Clone, Copy)]
struct Poll {
    timeout: Duration,
    budget: Option<WaitBudget>,
}

impl Poll {
    fn before_write(self) -> Result<(), PollError> {
        if self
            .budget
            .is_some_and(|budget| budget.remaining().is_zero())
        {
            return Err(PollError::Deadline);
        }
        Ok(())
    }
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
    poll(
        &keeper,
        addr,
        key,
        Poll {
            timeout,
            budget: None,
        },
        storage,
        events,
    )
    .await
    .map_err(|error| match error {
        PollError::Keeper(error) | PollError::Data(error) => error,
        PollError::Storage(error) => error.into(),
        PollError::Deadline => SaiError::internal(),
    })
}

pub(crate) async fn poll_wait(
    keeper: &Keeper<'_>,
    addr: &str,
    key: &str,
    budget: WaitBudget,
    storage: &Storage<'_>,
    events: EventContext<'_>,
) -> Result<Map<String, Value>, PollError> {
    poll(
        keeper,
        addr,
        Some(key),
        Poll {
            timeout: HISTORY_TIMEOUT,
            budget: Some(budget),
        },
        storage,
        events,
    )
    .await
}

async fn poll(
    keeper: &Keeper<'_>,
    addr: &str,
    key: Option<&str>,
    poll: Poll,
    storage: &Storage<'_>,
    events: EventContext<'_>,
) -> Result<Map<String, Value>, PollError> {
    let endpoint = keeper.endpoint();
    let mut variables = Map::from_iter([("user".into(), json!(addr)), ("key".into(), json!(key))]);
    let mut data = if let Some(budget) = poll.budget {
        variables.insert("includeAction".into(), json!(true));
        let timeout = poll.timeout.min(budget.remaining());
        bounded_request(keeper, keeper::WAIT_INTENTS, variables, timeout).await?
    } else {
        keeper
            .request(keeper::INTENTS, variables, None, poll.timeout)
            .await
            .map_err(PollError::Keeper)?
    };
    let Some(rows) = data.get_mut("userIntents").filter(|rows| !rows.is_null()) else {
        return Ok(data);
    };
    if poll.budget.is_some() && !rows.is_array() {
        *rows = Value::Null;
        return Ok(data);
    }
    let absent = rows.as_array().is_some_and(Vec::is_empty);
    let account = canonical_account(addr);
    let original = rows.take();
    let mut matching = Vec::new();
    match (original, &account) {
        (Value::Array(items), Some(account)) => {
            for row in items {
                if !row.is_object() && poll.budget.is_none() {
                    return Err(PollError::Data(SaiError::internal()));
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
        (Value::String(_) | Value::Object(_), Some(_)) => {
            return Err(PollError::Data(SaiError::internal()));
        }
        _ => return Err(PollError::Data(incomplete_data("TypeError"))),
    }
    let Some(account) = account else {
        *rows = Value::Array(matching);
        return Ok(data);
    };
    let Some(key) = key else {
        reconcile_account(storage, endpoint, &account, &mut matching).map_err(PollError::Data)?;
        *rows = Value::Array(matching);
        return Ok(data);
    };
    *rows = Value::Array(matching);

    if absent {
        log_keeper(
            events,
            endpoint,
            if poll.budget.is_some() {
                &account
            } else {
                addr
            },
            key,
            None,
            poll.budget.is_some(),
        );
        poll.before_write()?;
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
        log_keeper(
            events,
            endpoint,
            if poll.budget.is_some() {
                &account
            } else {
                addr
            },
            key,
            Some((&row, outcome)),
            poll.budget.is_some(),
        );
        if outcome != Outcome::Unknown {
            let context = KeyedIntent {
                storage,
                keeper,
                addr,
                account: &account,
                key,
                poll,
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
    poll: Poll,
}

async fn reconcile_key(
    context: &KeyedIntent<'_>,
    row: &Value,
    mut outcome: Outcome,
    data: &mut Map<String, Value>,
) -> Result<(), PollError> {
    let store = |outcome| {
        context.poll.before_write()?;
        Ok::<_, PollError>(store_outcome(
            context.storage,
            context.keeper.endpoint(),
            context.account,
            context.key,
            row,
            outcome,
            false,
        )?)
    };
    let mut local = store(outcome)?;
    if (outcome == Outcome::TerminalFilled && (row["kind"].is_null() || perp_history(&row["kind"])))
        || (outcome == Outcome::Pending && row["lifecycleStep"] != "pending_exec")
    {
        let matches = if let Some(budget) = context.poll.budget {
            read_wait_history(context, row, budget).await
        } else {
            read_history(context, row).await.map_err(PollError::Data)?
        };
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
) -> Result<Option<String>, StorageError> {
    let status = match outcome {
        Outcome::Accepted => IntentStatus::Accepted,
        Outcome::Pending => IntentStatus::Pending,
        Outcome::TerminalFailed | Outcome::TerminalFilled => IntentStatus::Terminal,
        Outcome::Unknown => return Ok(None),
    };
    storage.set_status_with(
        key,
        endpoint,
        account,
        status,
        StatusUpdateOptions {
            repair_legacy_terminal: outcome == Outcome::Pending && explicit_market_pending(row),
            only_if_absent,
        },
    )
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
                context.poll.timeout.min(HISTORY_TIMEOUT),
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
            Value::Object(_) | Value::String(_) => {
                return Err(SaiError::internal());
            }
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

async fn bounded_request(
    keeper: &Keeper<'_>,
    query: &str,
    variables: Map<String, Value>,
    timeout: Duration,
) -> Result<Map<String, Value>, PollError> {
    if timeout.is_zero() {
        return Err(PollError::Deadline);
    }
    keeper
        .request(query, variables, None, timeout)
        .await
        .map_err(PollError::Keeper)
}

async fn read_wait_history(
    context: &KeyedIntent<'_>,
    row: &Value,
    budget: WaitBudget,
) -> Vec<Value> {
    let Ok(hash) = normalize_hash(&row["txHash"]) else {
        return Vec::new();
    };
    if hash.is_empty() {
        return Vec::new();
    }
    let started = Instant::now();
    let remaining = || {
        budget
            .remaining()
            .min(HISTORY_TIMEOUT.saturating_sub(started.elapsed()))
    };
    let mut pages_left = HISTORY_MAX_PAGES + 1;
    let mut found = Vec::new();
    for attempt in 0..2 {
        if attempt > 0 {
            if remaining() <= HISTORY_RETRY_DELAY {
                break;
            }
            tokio::time::sleep(HISTORY_RETRY_DELAY).await;
        }
        let mut seen = HashSet::new();
        let mut execution_block = (row["lifecycleStep"] == "exec_pass")
            .then(|| row["execBlock"].as_i64())
            .flatten();
        for page in 0..pages_left.min(HISTORY_MAX_PAGES) {
            pages_left -= 1;
            let Ok(data) = bounded_request(
                context.keeper,
                keeper::INTENT_HISTORY,
                Map::from_iter([
                    ("trader".into(), json!(context.addr)),
                    ("limit".into(), json!(HISTORY_PAGE_SIZE)),
                    ("offset".into(), json!(page * HISTORY_PAGE_SIZE)),
                ]),
                remaining(),
            )
            .await
            else {
                return found;
            };
            let Some(history) = data["perp"]["tradeHistory"].as_array() else {
                return found;
            };
            for item in history {
                if item["intentKey"] == context.key
                    && account_matches(&item["trade"]["trader"], context.account)
                    && ["txHash", "evmTxHash"]
                        .iter()
                        .any(|field| normalize_hash(&item[*field]).is_ok_and(|value| value == hash))
                    && item["id"].as_i64().is_none_or(|id| seen.insert(id))
                {
                    found.push(item.clone());
                }
            }
            if history.len() < HISTORY_PAGE_SIZE {
                break;
            }
            if let Some(first) = found.first() {
                execution_block = execution_block.or_else(|| first["block"]["block"].as_i64());
                if let Some((oldest, execution)) = history
                    .last()
                    .and_then(|item| item["block"]["block"].as_i64())
                    .zip(execution_block)
                    && oldest < execution
                {
                    break;
                }
            }
        }
        if !found.is_empty() {
            break;
        }
    }
    found
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
    wait: bool,
) {
    let mut fields = vec![
        ("intentKey", json!(key).into()),
        ("addr", json!(addr).into()),
        ("endpoint", Field::Endpoint(endpoint.into())),
    ];
    if let Some((row, outcome)) = observed {
        if wait {
            let mut public = row.as_object().map_or(Value::Null, keeper::intent_record);
            public["errorCode"] = keeper::wait_error_code(row, &public);
            for key in [
                "kind",
                "lifecycleStep",
                "execActor",
                "errorCode",
                "txHash",
                "txHashInvalid",
            ] {
                fields.push((key, public[key].clone().into()));
            }
        } else {
            for key in ["kind", "lifecycleStep", "execActor", "errorCode", "txHash"] {
                fields.push((key, row[key].clone().into()));
            }
            if !row["errorMsg"].is_null() {
                fields.push(("errorMsg", Field::Untrusted(stringify(&row["errorMsg"]))));
            }
        }
        if outcome == Outcome::TerminalFailed {
            fields.push(("code", json!(Code::KeeperError).into()));
        }
    } else {
        fields.push(("found", json!(false).into()));
    }
    events.event("intent.keeper", Level::Info, fields);
}
