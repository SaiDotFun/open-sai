use std::{
    env,
    error::Error,
    fmt::Write as _,
    fs,
    io::{self, Write},
    path::Path,
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
use toml_edit::Document;

const MAINNET_HTTP: &str = "https://sai-keeper.nibiru.fi/query";

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
    flag: Option<&str>,
    json: bool,
) -> Result<(), Box<dyn Error>> {
    let path = config_path()?;
    let storage = Storage::new(path.parent().expect("config filename under Sai home"));
    match command {
        AuthCommand::Status => {
            let endpoint = keeper_endpoint(&path, flag)?;
            let session = storage.load_active_session(&endpoint, as_addr)?;
            emit(&session.summary()?, json)
        }
        AuthCommand::List => {
            let endpoint = keeper_endpoint(&path, flag)?;
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
    flag: Option<&str>,
    json: bool,
) -> Result<(), Box<dyn Error>> {
    let path = config_path()?;
    let storage = Storage::new(path.parent().expect("config filename under Sai home"));
    let endpoint = keeper_endpoint(&path, flag)?;
    match command {
        IntentCommand::Local { addr } => emit(&storage.list_pending(&endpoint, &addr)?, json),
    }
}

// Replace this adapter with the shared C2 settings resolver once it lands.
// Local commands keep the original endpoint string for storage comparisons.
fn keeper_endpoint(path: &Path, flag: Option<&str>) -> Result<String, Box<dyn Error>> {
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == io::ErrorKind::NotFound => String::new(),
        Err(error) => {
            return Err(format!("Cannot read {}: {error}", path.display()).into());
        }
    };
    let file = Document::parse(text.as_str())
        .map_err(|_| format!("Cannot parse {} as TOML", path.display()))?;
    let configured = file
        .get("keeper")
        .map(|item| {
            item.as_table_like().ok_or_else(|| {
                format!(
                    "{}: 'keeper' must be a table, such as [keeper]",
                    path.display()
                )
            })
        })
        .transpose()?
        .and_then(|table| table.get("endpoint"))
        .map(|item| {
            let url = item.as_str().ok_or_else(|| {
                format!(
                    "{}: Invalid value for keeper.endpoint: expected a URL string",
                    path.display()
                )
            })?;
            checked_endpoint(url, &path.display().to_string())
        })
        .transpose()?;
    if let Some(endpoint) = flag {
        return Ok(checked_endpoint(endpoint.trim(), "--endpoint")?.to_owned());
    }
    if let Ok(endpoint) = env::var("SAI_ENDPOINT")
        && !endpoint.trim().is_empty()
    {
        return Ok(checked_endpoint(endpoint.trim(), "SAI_ENDPOINT")?.to_owned());
    }
    Ok(configured.unwrap_or(MAINNET_HTTP).to_owned())
}

fn checked_endpoint<'a>(url: &'a str, source: &str) -> Result<&'a str, String> {
    if let Some((scheme, rest)) = url.trim_start().split_once("://")
        && (scheme.eq_ignore_ascii_case("https") || scheme.eq_ignore_ascii_case("http"))
        && !rest
            .split(['/', '?', '#'])
            .next()
            .unwrap_or_default()
            .is_empty()
    {
        return Ok(url);
    }
    Err(format!(
        "{source}: Invalid value for keeper.endpoint: expected a URL starting with https:// or http://"
    ))
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
