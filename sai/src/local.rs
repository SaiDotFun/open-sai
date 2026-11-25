mod python_numbers;
mod receipt_output;

use std::{
    fmt::Write as _,
    io::{self, Write},
    time::{Duration, Instant},
};

use clap::Subcommand;
use sai_core::{
    errors::{Code, ExitCode, SaiError},
    keeper,
    log::{EventContext, Field, Level},
    reconcile,
    settings::{Environment, Flags},
    storage::{IntentStatus, Storage, StorageEvent},
    wait,
};
use serde::Serialize;
use serde_json::ser::{Formatter, PrettyFormatter};
use serde_json::{Value, json};
use tokio::signal::unix::{self, Signal, SignalKind};

use crate::{load_settings, usage};

#[derive(Subcommand)]
pub enum IntentCommand {
    /// Read Keeper's latest intent records and record local pending fills.
    #[command(args_override_self = true)]
    Status {
        addr: String,
        /// Specific intent key.
        #[arg(long)]
        key: Option<String>,
    },
    /// List locally recorded intents for an account.
    Local { addr: String },
    /// Wait for an intent's final outcome and print its receipt.
    #[command(arg_required_else_help = true)]
    Wait {
        #[arg(value_name = "KEY")]
        identity: String,
        /// EVM or Nibiru address that submitted the intent.
        #[arg(long, conflicts_with = "key")]
        addr: Option<String>,
        /// Specific intent key when the positional argument is the account.
        #[arg(long)]
        key: Option<String>,
        /// Maximum wait in seconds.
        #[arg(long, default_value_t = wait::DEFAULT_TIMEOUT.to_string(), allow_hyphen_values = true)]
        timeout: String,
    },
}

pub fn intent_help(command: &str) -> Option<String> {
    let help = match command {
        "intent" => {
            "  Inspect and reconcile trading request outcomes.\n\nOptions:\n  --help  Show this message and exit.\n\nCommands:\n  local   List locally recorded intent identities for ADDR.\n  status  Read Keeper's latest intent records and record local pending...\n  wait    Wait for an intent's final outcome and print its receipt."
        }
        "intent status" => {
            "  Read Keeper's latest intent records and record local pending fills.\n\n  Use --key to check one intent for a final outcome.\n\nOptions:\n  --key TEXT  Specific intent key.\n  --help      Show this message and exit."
        }
        "intent local" => {
            "  List locally recorded intent identities for ADDR.\n\nOptions:\n  --help  Show this message and exit."
        }
        "intent wait" => {
            "  Wait for an intent's final outcome and print its receipt.\n\nOptions:\n  --addr TEXT      EVM or Nibiru address that submitted the intent.\n  --timeout FLOAT  Maximum wait in seconds.  [default: 30.0]\n  --help           Show this message and exit."
        }
        _ => return None,
    };
    Some(format!("{}\n\n{help}", usage(command)))
}

pub fn intent(
    command: IntentCommand,
    as_addr: Option<&str>,
    flags: &Flags,
    env: &Environment,
    json: bool,
    events: EventContext<'_>,
) -> Result<ExitCode, SaiError> {
    let started = Instant::now();
    let timeout = if let IntentCommand::Wait { timeout, .. } = &command {
        let seconds = timeout.trim().parse::<f64>().map_err(|_| {
            SaiError::usage(format!(
                "Invalid value for '--timeout': {} is not a valid float.",
                sai_core::settings::repr(timeout)
            ))
        })?;
        Some(wait::validate_timeout(seconds)?)
    } else {
        None
    };
    let settings = load_settings(flags, env)?;
    let log_storage = |event: StorageEvent<'_>| {
        storage_event_with_endpoint(events, event, Some(settings.endpoint()))
    };
    let storage = Storage::new(&env.home).with_event_sink(&log_storage);
    match command {
        IntentCommand::Status { addr, key } => {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|_| SaiError::internal())?;
            let data = runtime.block_on(crate::repl::interruptible(reconcile::reconcile(
                settings.endpoint(),
                &addr,
                key.as_deref(),
                keeper::TIMEOUT,
                &storage,
                events,
            )))?;
            emit(&keeper::public_intent_status(&env.home, &data), json)?;
        }
        IntentCommand::Local { addr } => {
            emit(&storage.list_pending(settings.endpoint(), &addr)?, json)?;
        }
        IntentCommand::Wait {
            identity,
            addr,
            key,
            ..
        } => {
            let (key, addr) = match key {
                Some(key) => (key, Some(identity)),
                None => (identity, addr),
            };
            let timeout = timeout.expect("wait timeout was validated");
            let endpoint = settings.endpoint();
            let addr = addr
                .filter(|addr| !addr.is_empty())
                .or_else(|| as_addr.map(str::to_owned));
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|_| SaiError::internal())?;
            return runtime.block_on(async {
                let mut signal = wait_signal()?;
                let addr = match addr {
                Some(addr) => Some(addr),
                None => {
                    let local = tokio::select! {
                        biased;
                        _ = signal.recv() => return Err(wait_aborted()),
                        record = wait_account_record(&storage, endpoint, &key, started, timeout) => record,
                    };
                    match local {
                        Ok(Some((addr, _))) => Some(addr),
                        Ok(None) if storage.session_exists() => {
                            let session = storage.load_session(None)?;
                            (session.endpoint() == Some(endpoint))
                                .then(|| session.addr().to_owned())
                        }
                        Ok(None) => None,
                        Err(error) if wait::ledger_busy(&error) => {
                            return emit_wait(
                                wait::unknown_result(&key),
                                endpoint,
                                env,
                                json,
                                false,
                                &mut signal,
                            ).await;
                        }
                        Err(error) => return Err(error.into()),
                    }
                }
            }
            .ok_or_else(|| {
                SaiError::new(
                    Code::InvalidRequest,
                    "Pass --addr with the account that submitted this intent",
                )
            })?;
                let (receipt, recovered) = tokio::select! {
                    biased;
                    _ = signal.recv() => (
                        wait::recovery_result(&storage, endpoint, &addr, &key, true, None), true,
                    ),
                    receipt = wait::for_intent(endpoint, &addr, &key, timeout, started, &storage, events) => {
                        match receipt {
                            Ok(receipt) => (receipt, false),
                            Err(_) => (wait::recovery_result(&storage, endpoint, &addr, &key, false, None), true),
                        }
                    }
                };
                emit_wait(receipt, endpoint, env, json, recovered, &mut signal).await
            });
        }
    }
    Ok(ExitCode::Ok)
}

pub(crate) fn wait_signal() -> Result<Signal, SaiError> {
    unix::signal(SignalKind::interrupt()).map_err(|_| SaiError::internal())
}

pub(crate) fn wait_aborted() -> SaiError {
    SaiError::new(Code::InvalidRequest, "Aborted")
}

async fn wait_account_record(
    storage: &Storage<'_>,
    endpoint: &str,
    key: &str,
    started: Instant,
    timeout: Duration,
) -> Result<Option<(String, String)>, sai_core::storage::StorageError> {
    let storage = storage.clone().with_busy_timeout(Duration::ZERO);
    loop {
        let record = storage.intent_record(endpoint, key);
        match &record {
            Err(error) if wait::ledger_busy(error) => {
                let left = timeout.saturating_sub(started.elapsed());
                if left.is_zero() {
                    return record;
                }
                tokio::time::sleep(left.min(Duration::from_millis(10))).await;
            }
            _ => return record,
        }
    }
}

pub(crate) async fn emit_wait(
    receipt: wait::ResultReceipt,
    endpoint: &str,
    env: &Environment,
    json: bool,
    recovered: bool,
    signal: &mut Signal,
) -> Result<ExitCode, SaiError> {
    let confirmation = match receipt.outcome {
        outcome @ (wait::Outcome::Rejected | wait::Outcome::NotSent) => Some(outcome),
        _ => None,
    };
    let (body, exit) = wait_output(receipt, endpoint, env, json, recovered);
    let line = encode_wait_line(&body, json)?;
    let mut output_started = false;
    let interrupted = tokio::select! {
        biased;
        _ = signal.recv() => true,
        output = async {
            output_started = true;
            receipt_output::write_stdout(line).await
        } => {
            if output.is_ok() {
                return Ok(exit);
            }
            false
        }
    };
    let (Some(addr), Some(key)) = (body["addr"].as_str(), body["intentKey"].as_str()) else {
        return Err(if interrupted {
            wait_aborted()
        } else {
            SaiError::internal()
        });
    };
    let storage = Storage::new(&env.home);
    let interrupted = interrupted || body["interrupted"].as_bool().unwrap_or(false);
    let receipt = wait::recovery_result(&storage, endpoint, addr, key, interrupted, confirmation);
    let (body, exit) = wait_output(receipt, endpoint, env, json, true);
    let line = encode_wait_line(&body, json)?;
    if !output_started
        && tokio::select! {
            biased;
            _ = signal.recv() => false,
        output = receipt_output::write_stdout(line.clone()) => output.is_ok(),
        }
    {
        return Ok(exit);
    }
    let _ = io::stderr().lock().write_all(line.as_bytes());
    Ok(exit)
}

fn wait_output(
    wait::ResultReceipt { outcome, mut body }: wait::ResultReceipt,
    endpoint: &str,
    env: &Environment,
    json: bool,
    recovered: bool,
) -> (Value, ExitCode) {
    let key = body["intentKey"]
        .as_str()
        .expect("receipt intent key")
        .to_owned();
    if !recovered && outcome == wait::Outcome::Unknown {
        let mut args = vec!["intent", "wait", &key];
        if let Some(addr) = body["addr"].as_str().filter(|addr| !addr.is_empty()) {
            args.extend(["--addr", addr]);
        }
        body["hint"] = json!(Storage::new(&env.home).trade_command_hint(endpoint, &args));
    }
    if let Some(message) = body["message"].as_str() {
        body["message"] = json!(sai_core::log::redact(&env.home, message));
    }
    let mut exit = ExitCode::Ok;
    if let Some(code) = outcome.code() {
        let message = if recovered {
            "The receipt did not finish. Check the intent status before retrying."
        } else {
            body["message"]
                .as_str()
                .unwrap_or("The intent outcome is still unknown.")
        };
        exit = code.exit_code();
        if json {
            let mut error = json!({
                "code": code,
                "message": sai_core::log::redact(&env.home, message),
                "retryable": exit == ExitCode::Rejected,
                "intentKey": sai_core::log::redact(&env.home, &key),
            });
            if let Some(hint) = body["hint"].as_str() {
                error["hint"] = json!(sai_core::log::redact(&env.home, hint));
            }
            body["error"] = error;
        }
    }
    (body, exit)
}

pub(crate) fn storage_event(events: EventContext<'_>, event: StorageEvent<'_>) {
    storage_event_with_endpoint(events, event, None);
}

pub(crate) fn storage_event_with_endpoint(
    events: EventContext<'_>,
    event: StorageEvent<'_>,
    trade_endpoint: Option<&str>,
) {
    let endpoint_id = || {
        trade_endpoint.map_or(
            Field::Scalar(serde_json::Value::Null),
            Field::endpoint_identity,
        )
    };
    match event {
        StorageEvent::IntentReserved {
            key,
            kind,
            addr,
            entity,
            endpoint,
        } => {
            events.event(
                "intent.reserved",
                Level::Info,
                [
                    ("intentKey", json!(key).into()),
                    ("kind", json!(kind).into()),
                    ("status", json!("unknown").into()),
                    ("addr", json!(addr).into()),
                    (
                        "tradeId",
                        json!((!entity.is_empty()).then_some(entity)).into(),
                    ),
                    ("endpoint", Field::Endpoint(endpoint.into())),
                    ("endpointId", endpoint_id()),
                ],
            );
        }
        StorageEvent::IntentStatus {
            key,
            kind,
            previous,
            status,
            addr,
            endpoint,
        } => {
            let code = match status {
                IntentStatus::Rejected => Some(Code::AdmissionRejected),
                IntentStatus::NotSent => Some(Code::NotSent),
                _ => None,
            };
            events.event(
                "intent.status",
                Level::Info,
                [
                    ("intentKey", json!(key).into()),
                    ("kind", json!(kind).into()),
                    ("previous", json!(previous).into()),
                    ("status", json!(status.as_str()).into()),
                    ("addr", json!(addr).into()),
                    ("endpoint", Field::Endpoint(endpoint.into())),
                    ("endpointId", endpoint_id()),
                    ("code", json!(code).into()),
                ],
            );
        }
        StorageEvent::SessionExpired { addr, expires_at } => {
            events.event(
                "session.expired",
                Level::Warn,
                [
                    ("code", json!(Code::SessionExpired).into()),
                    ("addr", json!(addr).into()),
                    ("expiresAt", json!(expires_at).into()),
                    ("endpointId", endpoint_id()),
                ],
            );
        }
        StorageEvent::SessionSaved {
            addr,
            expires_at,
            endpoint,
            connected_accounts,
        } => {
            events.event(
                "session.saved",
                Level::Info,
                [
                    ("addr", json!(addr).into()),
                    ("expiresAt", json!(expires_at).into()),
                    (
                        "endpoint",
                        endpoint
                            .map_or_else(|| json!(null).into(), |url| Field::Endpoint(url.into())),
                    ),
                    ("connectedAccounts", json!(connected_accounts).into()),
                ],
            );
        }
        StorageEvent::SessionActive { addr } => {
            events.event(
                "session.active",
                Level::Info,
                [("addr", json!(addr).into())],
            );
        }
        StorageEvent::SessionRemoved {
            addr,
            connected_accounts,
        } => {
            events.event(
                "session.removed",
                Level::Info,
                [
                    ("addr", json!(addr).into()),
                    ("connectedAccounts", json!(connected_accounts).into()),
                ],
            );
        }
        StorageEvent::UnreadableSessionRemoved => {
            events.event(
                "session.removed",
                Level::Warn,
                [
                    ("code", json!(Code::SessionUnavailable).into()),
                    ("reason", json!("unreadable session file").into()),
                ],
            );
        }
    }
}

// Match the spaces Python emits with json.dumps(indent=None).
pub(crate) struct InlineJson;

impl Formatter for InlineJson {
    fn begin_array_value<W: Write + ?Sized>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if !first {
            writer.write_all(b", ")?;
        }
        Ok(())
    }

    fn begin_object_key<W: Write + ?Sized>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> io::Result<()> {
        self.begin_array_value(writer, first)
    }

    fn begin_object_value<W: Write + ?Sized>(&mut self, writer: &mut W) -> io::Result<()> {
        writer.write_all(b": ")
    }
}

pub(crate) fn emit(value: &impl Serialize, json: bool) -> Result<(), SaiError> {
    let text = if json {
        encode(value, InlineJson)?
    } else {
        encode(value, PrettyFormatter::new())?
    };
    println!("{}", ascii_json(&text));
    Ok(())
}

fn encode_wait_line(value: &Value, json: bool) -> Result<String, SaiError> {
    use python_numbers::PythonNumbers;
    let text = if json {
        encode(value, PythonNumbers(InlineJson))
    } else {
        encode(value, PythonNumbers(PrettyFormatter::new()))
    }?;
    let mut line = ascii_json(&text).into_owned();
    line.push('\n');
    Ok(line)
}

fn encode(value: &impl Serialize, formatter: impl Formatter) -> Result<String, SaiError> {
    let mut bytes = Vec::new();
    value
        .serialize(&mut serde_json::Serializer::with_formatter(
            &mut bytes, formatter,
        ))
        .map_err(|_| SaiError::internal())?;
    String::from_utf8(bytes).map_err(|_| SaiError::internal())
}

pub fn ascii_json(text: &str) -> std::borrow::Cow<'_, str> {
    if text.bytes().all(|byte| byte < 0x7f) {
        return text.into();
    }
    let mut escaped = String::new();
    for ch in text.chars() {
        if ch < '\u{7f}' {
            escaped.push(ch);
        } else {
            for unit in ch.encode_utf16(&mut [0; 2]) {
                write!(escaped, "\\u{unit:04x}").expect("writing to a string");
            }
        }
    }
    escaped.into()
}
