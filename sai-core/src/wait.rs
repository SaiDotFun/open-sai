//! Wait for an intent within one deadline, including Keeper reads and ledger updates.

use std::{
    path::Path,
    time::{Duration, Instant},
};

use rusqlite::ErrorCode;
use serde_json::{Value, json};

use crate::{
    errors::{Code, SaiError},
    keeper::{self, Keeper},
    log::{self, EventContext, Field, Level},
    receipt::Evidence,
    reconcile::{self, PollError, WaitBudget, classify_outcome},
    session::shell_quote,
    storage::{Storage, StorageError, endpoint_origin},
};

pub const DEFAULT_TIMEOUT: f64 = 30.0;
const INITIAL_DELAY: Duration = Duration::from_millis(250);
const MAX_DELAY: Duration = Duration::from_secs(2);

#[derive(Clone, Copy, Eq, PartialEq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Filled,
    Placed,
    Succeeded,
    Failed,
    Liquidated,
    Unexpected,
    Rejected,
    NotSent,
    Absent,
    Terminal,
    Unknown,
}

impl Outcome {
    pub fn code(self) -> Option<Code> {
        match self {
            Self::Filled | Self::Placed | Self::Succeeded => None,
            Self::Unknown => Some(Code::OutcomeUnknown),
            Self::Rejected => Some(Code::AdmissionRejected),
            Self::NotSent => Some(Code::NotSent),
            _ => Some(Code::KeeperError),
        }
    }

    pub(crate) fn has_execution(self) -> bool {
        matches!(
            self,
            Self::Filled
                | Self::Placed
                | Self::Succeeded
                | Self::Failed
                | Self::Liquidated
                | Self::Unexpected
        )
    }

    fn recorded(status: &str) -> Self {
        match status {
            "terminal" => Self::Terminal,
            "absent" => Self::Absent,
            "rejected" => Self::Rejected,
            "not_sent" => Self::NotSent,
            _ => Self::Unknown,
        }
    }
}

pub struct ResultReceipt {
    pub outcome: Outcome,
    pub body: Value,
}

pub fn validate_timeout(seconds: f64) -> Result<Duration, SaiError> {
    if !seconds.is_finite() || seconds <= 0.0 {
        return Err(SaiError::new(
            Code::InvalidRequest,
            "Timeout must be a positive finite number of seconds",
        ));
    }
    Ok(Duration::try_from_secs_f64(seconds).unwrap_or(Duration::MAX))
}

pub fn ledger_busy(error: &StorageError) -> bool {
    matches!(error, StorageError::Sqlite(rusqlite::Error::SqliteFailure(error, _))
        if matches!(error.code, ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked))
}

fn local_status(
    storage: &Storage<'_>,
    endpoint: &str,
    addr: &str,
    key: &str,
) -> Result<Option<String>, StorageError> {
    match storage.intent_record(endpoint, key) {
        Ok(Some((owner, status))) if keeper::canonical_account(addr).as_deref() == Some(&owner) => {
            Ok(Some(status))
        }
        Ok(_) => Ok(None),
        Err(error) if ledger_busy(&error) => Ok(None),
        Err(error) => Err(error),
    }
}

struct WaitLog<'a> {
    events: EventContext<'a>,
    endpoint: &'a str,
    addr: &'a str,
    key: &'a str,
    started: Instant,
    outcome: Outcome,
}

impl Drop for WaitLog<'_> {
    fn drop(&mut self) {
        self.events.event(
            "intent.wait.end",
            Level::Info,
            [
                ("intentKey", json!(self.key).into()),
                ("addr", json!(self.addr).into()),
                ("endpoint", Field::Endpoint(self.endpoint.into())),
                ("status", json!(self.outcome).into()),
                (
                    "code",
                    json!(
                        self.outcome.code().filter(|_| !matches!(
                            self.outcome,
                            Outcome::Absent | Outcome::Terminal
                        ))
                    )
                    .into(),
                ),
                ("durationMs", json!(millis(self.started.elapsed())).into()),
            ],
        );
    }
}

fn millis(duration: Duration) -> u128 {
    (duration.as_secs_f64() * 1000.0).round_ties_even() as u128
}

/// Poll one account and key. All writes use the same reconciliation as intent
/// status, with zero SQLite busy timeout so a competing writer cannot hold up polling.
pub async fn for_intent(
    endpoint: &str,
    addr: &str,
    key: &str,
    timeout: Duration,
    started: Instant,
    storage: &Storage<'_>,
    events: EventContext<'_>,
) -> Result<ResultReceipt, SaiError> {
    if keeper::canonical_account(addr).is_none() {
        return Err(SaiError::new(
            Code::InvalidRequest,
            "Pass a valid EVM or Nibiru account address",
        ));
    }
    let budget = WaitBudget { started, timeout };
    let storage = storage.clone().with_busy_timeout(Duration::ZERO);
    let recorded = || -> Result<Outcome, StorageError> {
        Ok(local_status(&storage, endpoint, addr, key)?
            .as_deref()
            .map_or(Outcome::Unknown, Outcome::recorded))
    };
    events.event(
        "intent.wait.start",
        Level::Info,
        [
            ("intentKey", json!(key).into()),
            ("addr", json!(addr).into()),
            ("endpoint", Field::Endpoint(endpoint.into())),
            ("timeoutMs", json!(millis(timeout)).into()),
        ],
    );
    let mut wait_log = WaitLog {
        events,
        endpoint,
        addr,
        key,
        started,
        outcome: Outcome::Unknown,
    };
    if !budget.remaining().is_zero() {
        let initial = recorded()?;
        if matches!(initial, Outcome::Rejected | Outcome::NotSent) {
            wait_log.outcome = initial;
            return Ok(result(
                key,
                Some(addr),
                initial,
                &Value::Null,
                &[],
                &Value::Null,
                &Value::Null,
            ));
        }
    }

    let keeper = Keeper::new(endpoint, events);
    let mut delay = INITIAL_DELAY;
    let (mut row, mut request_hash, mut request_block) = (Value::Null, Value::Null, Value::Null);
    let mut history = Vec::new();
    while !budget.remaining().is_zero() {
        let data = match reconcile::poll_wait(&keeper, addr, key, budget, &storage, events).await {
            Ok(data) => data,
            Err(PollError::Keeper(_)) => Default::default(),
            Err(PollError::Storage(error)) if ledger_busy(&error) => Default::default(),
            Err(PollError::Deadline) => break,
            Err(PollError::Storage(error)) => return Err(error.into()),
            Err(PollError::Data(error)) => return Err(error),
        };
        row = data
            .get("userIntents")
            .and_then(|rows| rows.get(0))
            .cloned()
            .unwrap_or_default();
        if row["execActor"] == "server" {
            if keeper::truthy(&row["txHash"]) {
                request_hash = row["txHash"].clone();
            }
            if row["lifecycleStep"] == "pending_exec" && keeper::truthy(&row["execBlock"]) {
                request_block = row["execBlock"].clone();
            }
        }
        let classified = classify_outcome(&row);
        match classified {
            reconcile::Outcome::TerminalFilled => {}
            reconcile::Outcome::TerminalFailed => {
                wait_log.outcome = Outcome::Failed;
                break;
            }
            reconcile::Outcome::Unknown
                if data
                    .get("localStatus")
                    .is_some_and(|status| status == "absent") =>
            {
                wait_log.outcome = Outcome::Absent;
                break;
            }
            _ if data
                .get("outcome")
                .is_some_and(|outcome| outcome == "terminal-filled") => {}
            _ => {
                let remaining = budget.remaining();
                if remaining.is_zero() {
                    break;
                }
                tokio::time::sleep(delay.min(remaining)).await;
                delay = (delay * 2).min(MAX_DELAY);
                continue;
            }
        }
        history = data
            .get("matchedTradeHistory")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        wait_log.outcome = Evidence::new(&row, &history).success();
        break;
    }
    if wait_log.outcome == Outcome::Unknown {
        wait_log.outcome = recorded()?;
    }
    Ok(result(
        key,
        Some(addr),
        wait_log.outcome,
        &row,
        &history,
        &request_hash,
        &request_block,
    ))
}

pub fn unknown_result(key: &str) -> ResultReceipt {
    result(
        key,
        None,
        Outcome::Unknown,
        &Value::Null,
        &[],
        &Value::Null,
        &Value::Null,
    )
}

fn result(
    key: &str,
    addr: Option<&str>,
    outcome: Outcome,
    row: &Value,
    history: &[Value],
    request_hash: &Value,
    request_block: &Value,
) -> ResultReceipt {
    let receipt = Evidence::new(row, history).build(outcome, request_hash, request_block);
    let mut body = json!({"intentKey": key, "addr": addr, "outcome": outcome, "receipt": receipt});
    let message = match outcome {
        Outcome::Failed => {
            let public = row.as_object().map_or(Value::Null, keeper::intent_record);
            body["keeperErrorCode"] = keeper::wait_error_code(row, &public);
            Some("Keeper reported an intent error.".into())
        }
        Outcome::Liquidated => Some("Keeper recorded liquidation while processing the intent.".into()),
        Outcome::Unexpected => Some(match receipt["execution"]["tradeChangeType"].as_str() {
            Some(change) => format!("Keeper recorded {change} for this intent. Review the receipt."),
            None => "Keeper recorded an unexpected outcome. Review the receipt.".into(),
        }),
        Outcome::Rejected => Some("Keeper rejected admission for this intent.".into()),
        Outcome::NotSent => Some("The local ledger recorded that the intent was not sent.".into()),
        Outcome::Terminal => Some("The local ledger recorded a final outcome. Its execution details are unavailable.".into()),
        Outcome::Absent => Some("Keeper had no record after the admission grace period. The local block was released. A delayed admission can still appear.".into()),
        _ => None,
    };
    if let Some(message) = message {
        body["message"] = json!(message);
    }
    if let Some(code) = outcome
        .code()
        .filter(|_| !matches!(outcome, Outcome::Absent | Outcome::Terminal))
    {
        body["code"] = json!(code);
    }
    if outcome == Outcome::Unknown {
        body["hint"] = json!("Check this same intent again. Do not resubmit.");
    }
    ResultReceipt { outcome, body }
}

pub fn recovery_result(
    home: &Path,
    endpoint: &str,
    addr: &str,
    key: &str,
    interrupted: bool,
) -> ResultReceipt {
    let storage = Storage::new(home).with_busy_timeout(Duration::ZERO);
    let status = local_status(&storage, endpoint, addr, key).ok().flatten();
    let outcome = status
        .as_deref()
        .map_or(Outcome::Unknown, Outcome::recorded);
    let body = json!({
        "intentKey": key,
        "addr": addr,
        "outcome": outcome,
        "localStatus": status,
        "code": outcome.code(),
        "receipt": null,
        "interrupted": interrupted,
        "hint": command_hint(home, endpoint, &["intent", "status", addr, "--key", key]),
    });
    ResultReceipt { outcome, body }
}

pub fn command_hint(home: &Path, endpoint: &str, args: &[&str]) -> String {
    let origin = endpoint_origin(endpoint);
    let endpoint = if origin != "[redacted url]"
        && origin == endpoint
        && log::redact(home, endpoint) == endpoint
    {
        shell_quote(endpoint)
    } else {
        r#""${SAI_RECONCILE_ENDPOINT:?Set SAI_RECONCILE_ENDPOINT to the original Keeper URL}""#
            .into()
    };
    let args = args
        .iter()
        .map(|arg| shell_quote(arg))
        .collect::<Vec<_>>()
        .join(" ");
    format!("sai --endpoint {endpoint} {args}")
}
