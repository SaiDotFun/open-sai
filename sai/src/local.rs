use std::{
    fmt::Write as _,
    io::{self, Write},
    time::Instant,
};

use chrono::Utc;
use clap::Subcommand;
use sai_core::{
    errors::{Code, ExitCode, SaiError},
    keeper,
    log::{EventContext, Field, Level},
    reconcile,
    session::expired,
    settings::{Environment, Flags},
    storage::{IntentStatus, RemovedSession, SessionSummary, Storage, StorageEvent},
    wait,
};
use serde::Serialize;
use serde_json::json;
use serde_json::ser::{Formatter, PrettyFormatter};

use crate::{load_settings, usage};

#[derive(Subcommand)]
pub enum AuthCommand {
    /// Show the current account and expiry.
    Status,
    /// List connected accounts and which one is active.
    List,
    /// Make a connected account active.
    Use { addr: String },
    /// Remove the specified account or the active account.
    Logout { addr: Option<String> },
}

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
            "  Inspect and reconcile trading request outcomes.\n\nOptions:\n  --help  Show this message and exit.\n\nCommands:\n  local   List locally recorded intent identities for ADDR.\n  status  Read Keeper's latest intent records and record local pending..."
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

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct AccountRow<'a> {
    #[serde(flatten)]
    summary: SessionSummary<'a>,
    active: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    expired: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    endpoint_matches: Option<bool>,
}

#[derive(Serialize)]
struct AccountList<'a> {
    accounts: Vec<AccountRow<'a>>,
}

#[derive(Serialize)]
struct Logout {
    removed: bool,
    addr: Option<String>,
    note: &'static str,
}

pub fn auth(
    command: AuthCommand,
    as_addr: Option<&str>,
    flags: &Flags,
    env: &Environment,
    json: bool,
    events: EventContext<'_>,
) -> Result<(), SaiError> {
    let log_storage = |event: StorageEvent<'_>| storage_event(events, event);
    let storage = Storage::new(&env.home).with_event_sink(&log_storage);
    match command {
        AuthCommand::Status => {
            let settings = load_settings(flags, env)?;
            let session = storage.load_active_session(settings.endpoint(), as_addr)?;
            emit(&session.summary()?, json)
        }
        AuthCommand::List => {
            let settings = load_settings(flags, env)?;
            let accounts = storage.load_accounts()?;
            let now = Utc::now();
            let accounts = accounts
                .iter()
                .map(|(session, active)| {
                    Ok(AccountRow {
                        summary: session.summary()?,
                        active,
                        expired: Some(expired(session, now)?),
                        endpoint_matches: Some(session.endpoint() == Some(settings.endpoint())),
                    })
                })
                .collect::<Result<_, sai_core::storage::StorageError>>()?;
            emit(&AccountList { accounts }, json)
        }
        AuthCommand::Use { addr } => {
            let session = storage.use_session(&addr)?;
            emit(
                &AccountRow {
                    summary: session.summary()?,
                    active: true,
                    expired: None,
                    endpoint_matches: None,
                },
                json,
            )
        }
        AuthCommand::Logout { addr } => {
            let removed =
                storage.delete_session(addr.as_deref().filter(|s| !s.is_empty()).or(as_addr))?;
            emit(
                &Logout {
                    removed: removed.is_some(),
                    addr: match removed {
                        Some(RemovedSession::Account(addr)) => Some(addr),
                        _ => None,
                    },
                    note: "Keeper cannot revoke session tokens yet; the token stays valid until it expires.",
                },
                json,
            )
        }
    }
}

pub fn intent(
    command: IntentCommand,
    as_addr: Option<&str>,
    flags: &Flags,
    env: &Environment,
    json: bool,
    events: EventContext<'_>,
) -> Result<ExitCode, SaiError> {
    let log_storage = |event: StorageEvent<'_>| storage_event(events, event);
    let storage = Storage::new(&env.home).with_event_sink(&log_storage);
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
    match command {
        IntentCommand::Status { addr, key } => {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|_| SaiError::internal())?;
            let data = runtime.block_on(reconcile::reconcile(
                settings.endpoint(),
                &addr,
                key.as_deref(),
                keeper::TIMEOUT,
                &storage,
                events,
            ))?;
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
            let addr = match addr {
                Some(addr) => Some(addr),
                None => {
                    let busy = timeout
                        .saturating_sub(started.elapsed())
                        .min(std::time::Duration::from_millis(i32::MAX as u64));
                    match storage
                        .clone()
                        .with_busy_timeout(busy)
                        .intent_record(endpoint, &key)
                    {
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
                            );
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
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|_| SaiError::internal())?;
            let (receipt, recovered) = runtime.block_on(async {
                tokio::select! {
                    biased;
                    signal = tokio::signal::ctrl_c() => (
                        wait::recovery_result(&storage, endpoint, &addr, &key, signal.is_ok(), None), true,
                    ),
                    receipt = wait::for_intent(endpoint, &addr, &key, timeout, started, &storage, events) => {
                        match receipt {
                            Ok(receipt) => (receipt, false),
                            Err(_) => (wait::recovery_result(&storage, endpoint, &addr, &key, false, None), true),
                        }
                    }
                }
            });
            return emit_wait(receipt, endpoint, env, json, recovered);
        }
    }
    Ok(ExitCode::Ok)
}

pub(crate) fn emit_wait(
    wait::ResultReceipt { outcome, mut body }: wait::ResultReceipt,
    endpoint: &str,
    env: &Environment,
    json: bool,
    recovered: bool,
) -> Result<ExitCode, SaiError> {
    let key = body["intentKey"]
        .as_str()
        .expect("receipt intent key")
        .to_owned();
    if !recovered && outcome == wait::Outcome::Unknown {
        let mut args = vec!["intent", "wait", &key];
        if let Some(addr) = body["addr"].as_str().filter(|addr| !addr.is_empty()) {
            args.extend(["--addr", addr]);
        }
        body["hint"] = json!(Storage::new(&env.home).keeper_command_hint(endpoint, &args));
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
    emit(&body, json)?;
    Ok(exit)
}

pub(crate) fn storage_event(events: EventContext<'_>, event: StorageEvent<'_>) {
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
                ],
            );
        }
        _ => {}
    }
}

// Match the spaces Python emits with json.dumps(indent=None).
struct InlineJson;

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
    let mut bytes = Vec::new();
    if json {
        value.serialize(&mut serde_json::Serializer::with_formatter(
            &mut bytes, InlineJson,
        ))
    } else {
        value.serialize(&mut serde_json::Serializer::with_formatter(
            &mut bytes,
            PrettyFormatter::new(),
        ))
    }
    .map_err(|_| SaiError::internal())?;
    let text = String::from_utf8(bytes).map_err(|_| SaiError::internal())?;
    println!("{}", ascii_json(&text));
    Ok(())
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
