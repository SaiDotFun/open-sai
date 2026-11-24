use std::{
    fmt::Write as _,
    io::{self, Write},
};

use clap::Subcommand;
use sai_core::{
    errors::{Code, SaiError},
    keeper,
    log::{EventContext, Field, Level},
    reconcile,
    settings::{Environment, Flags},
    storage::{IntentStatus, Storage, StorageEvent},
};
use serde::Serialize;
use serde_json::json;
use serde_json::ser::{Formatter, PrettyFormatter};

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
        _ => return None,
    };
    Some(format!("{}\n\n{help}", usage(command)))
}

pub fn intent(
    command: IntentCommand,
    flags: &Flags,
    env: &Environment,
    json: bool,
    events: EventContext<'_>,
) -> Result<(), SaiError> {
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
            emit(&keeper::public_intent_status(&env.home, &data), json)
        }
        IntentCommand::Local { addr } => {
            emit(&storage.list_pending(settings.endpoint(), &addr)?, json)
        }
    }
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
