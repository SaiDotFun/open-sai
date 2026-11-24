use std::{
    error::Error,
    fmt::Write as _,
    io::{self, Write},
};

use chrono::Utc;
use clap::Subcommand;
use sai_core::{
    config::config_path,
    session::expired,
    storage::{RemovedSession, SessionSummary, Storage},
};
use serde::Serialize;
use serde_json::ser::{Formatter, PrettyFormatter};
mod settings;

pub use settings::SettingsFlags;
use settings::keeper_endpoint;

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
    /// List locally recorded intents for an account.
    Local { addr: String },
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
    flags: SettingsFlags<'_>,
    json: bool,
) -> Result<(), Box<dyn Error>> {
    let path = config_path()?;
    let storage = Storage::new(path.parent().expect("config filename under Sai home"));
    match command {
        AuthCommand::Status => {
            let endpoint = keeper_endpoint(&path, flags)?;
            let session = storage.load_active_session(&endpoint, as_addr)?;
            emit(&session.summary()?, json)
        }
        AuthCommand::List => {
            let endpoint = keeper_endpoint(&path, flags)?;
            let accounts = storage.load_accounts()?;
            let now = Utc::now();
            let accounts = accounts
                .iter()
                .map(|(session, active)| {
                    Ok(AccountRow {
                        summary: session.summary()?,
                        active,
                        expired: Some(expired(session, now)?),
                        endpoint_matches: Some(session.endpoint() == Some(&endpoint)),
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
    flags: SettingsFlags<'_>,
    json: bool,
) -> Result<(), Box<dyn Error>> {
    let path = config_path()?;
    let storage = Storage::new(path.parent().expect("config filename under Sai home"));
    let endpoint = keeper_endpoint(&path, flags)?;
    match command {
        IntentCommand::Local { addr } => emit(&storage.list_pending(&endpoint, &addr)?, json),
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

fn emit(value: &impl Serialize, json: bool) -> Result<(), Box<dyn Error>> {
    let mut bytes = Vec::new();
    if json {
        value.serialize(&mut serde_json::Serializer::with_formatter(
            &mut bytes, InlineJson,
        ))?;
    } else {
        value.serialize(&mut serde_json::Serializer::with_formatter(
            &mut bytes,
            PrettyFormatter::new(),
        ))?;
    }
    let text = String::from_utf8(bytes)?;
    if text.bytes().all(|byte| byte < 0x7f) {
        println!("{text}");
    } else {
        let mut escaped = String::new();
        for ch in text.chars() {
            if ch < '\u{7f}' {
                escaped.push(ch);
            } else {
                for unit in ch.encode_utf16(&mut [0; 2]) {
                    write!(escaped, "\\u{unit:04x}")?;
                }
            }
        }
        println!("{escaped}");
    }
    Ok(())
}
