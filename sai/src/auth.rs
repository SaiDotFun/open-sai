use std::{
    env,
    io::{self, IsTerminal},
};

use chrono::Utc;
use clap::Subcommand;
use sai_core::{
    auth::{self, CLIPBOARD_NAMES, Clipboard, LoginError},
    errors::{Code, ExitCode, SaiError},
    keeper::Keeper,
    log::{self, EventContext},
    session::{expired, renewal_command},
    settings::{Environment, Flags},
    storage::{RemovedSession, SessionSummary, Storage, StorageEvent},
};
use serde::Serialize;
use zeroize::Zeroizing;

use crate::{
    load_settings,
    local::{emit, storage_event},
};

#[derive(Subcommand)]
pub enum AuthCommand {
    /// Connect the account whose key is NAME by signing Keeper's challenge.
    #[command(args_override_self = true)]
    Login {
        name: String,
        /// Expected EVM address of the named wallet.
        #[arg(long)]
        addr: Option<String>,
    },
    /// Connect with a session token copied from Sai Web.
    Import,
    /// Renew the selected account's session with its saved key name.
    Renew { addr: Option<String> },
    /// Show the current account and expiry.
    Status,
    /// List connected accounts and which one is active.
    List,
    /// Make a connected account active.
    Use { addr: String },
    /// Remove the specified account or the active account.
    Logout { addr: Option<String> },
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

pub fn dispatch(
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
        AuthCommand::Login { name, addr } => {
            let settings = load_settings(flags, env)?;
            let endpoint = settings.endpoint();
            let keeper = Keeper::new(endpoint, events);
            let session = runtime()?
                .block_on(login(&keeper, &name, addr.as_deref()))
                .map_err(|error| {
                    if !matches!(error, LoginError::NeedsTerminal(_)) {
                        return login_failure(error);
                    }
                    let mut args = Vec::new();
                    if json {
                        args.push("--json");
                    }
                    args.extend(["auth", "login", &name]);
                    if let Some(addr) = addr.as_deref() {
                        args.extend(["--addr", addr]);
                    }
                    let command = storage.keeper_command_hint(endpoint, &args);
                    unsupported(format!("{error} Run `{command}` in a terminal."), command)
                })?;
            let summary = session.summary()?;
            let summary = serde_json::to_value(summary).map_err(|_| SaiError::internal())?;
            storage.save_session(session)?;
            emit(&summary, json)
        }
        AuthCommand::Import => runtime()?.block_on(import(&storage, flags, env, json)),
        AuthCommand::Renew { addr } => {
            let stored =
                storage.load_session(addr.as_deref().filter(|s| !s.is_empty()).or(as_addr))?;
            let settings = load_settings(flags, env)?;
            let endpoint = settings.endpoint();
            if stored.endpoint() != Some(endpoint) {
                return Err(SaiError::new(
                    Code::SessionUnavailable,
                    "Stored session belongs to another Keeper endpoint",
                ));
            }
            let Some(name) = stored.key_name() else {
                let command = renewal_command(&stored);
                let message = if stored.is_imported() {
                    format!("This session was imported. Run {command} with a fresh token.")
                } else {
                    format!("This session has no saved key name. Run {command}.")
                };
                return Err(SaiError::new(Code::InvalidRequest, message));
            };
            let keeper = Keeper::new(endpoint, events);
            let session = runtime()?
                .block_on(login(&keeper, name, Some(stored.addr())))
                .map_err(|error| {
                    if matches!(error, LoginError::NeedsTerminal(_)) {
                        SaiError::new(
                            Code::SessionUnavailable,
                            format!("{error} Run {} in a terminal.", renewal_command(&stored)),
                        )
                    } else {
                        login_failure(error)
                    }
                })?;
            let summary = serde_json::to_value(session.origin_summary()?)
                .map_err(|_| SaiError::internal())?;
            storage.replace_session(&stored, session)?;
            emit(&summary, json)
        }
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

fn runtime() -> Result<tokio::runtime::Runtime, SaiError> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| SaiError::internal())
}

async fn login(
    keeper: &Keeper<'_>,
    name: &str,
    addr: Option<&str>,
) -> Result<sai_core::storage::Session, LoginError> {
    let wallet_home = env::home_dir().ok_or_else(SaiError::internal)?;
    auth::login(
        keeper,
        &wallet_home,
        name,
        addr,
        io::stdin().is_terminal(),
        |message| eprintln!("{message}"),
    )
    .await
}

fn login_failure(error: LoginError) -> SaiError {
    match error {
        LoginError::Failure(error) => error,
        error => SaiError::new(Code::KeeperError, error.to_string()),
    }
}

async fn import(
    storage: &Storage<'_>,
    flags: &Flags,
    env: &Environment,
    json: bool,
) -> Result<(), SaiError> {
    let token = read_token().await?;
    let settings = load_settings(flags, env)?;
    let session = auth::import_session(token.clone(), settings.endpoint())?;
    let summary = serde_json::to_value(session.summary()?).map_err(|_| SaiError::internal())?;
    log::remember([token.to_string()]);
    storage.save_session(session)?;
    if let Some((copied, tool)) = Clipboard::read().await
        && copied == token
        && !tool.clear().await
    {
        eprintln!("Warning: could not clear the clipboard");
    }
    emit(&summary, json)
}

async fn read_token() -> Result<Zeroizing<String>, SaiError> {
    let terminal = io::stdin().is_terminal();
    if terminal {
        // rpassword turns Ctrl-C into SIGINT after restoring the terminal.
        // Catch it so a canceled prompt uses the CLI's normal error format.
        let _interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())
            .map_err(|_| SaiError::internal())?;
        let config = rpassword::ConfigBuilder::new()
            .input_file_path("/dev/stdin")
            .output_writer(io::stderr())
            .build();
        let typed = rpassword::prompt_password_with_config(
            "Paste the Sai session token (Enter reads the clipboard): ",
            config,
        )
        .map(Zeroizing::new)
        .map_err(|error| {
            if error.kind() == io::ErrorKind::UnexpectedEof {
                eprintln!();
            }
            SaiError::new(Code::InvalidRequest, "Aborted")
        })?;
        if !typed.trim().is_empty() {
            return Ok(Zeroizing::new(typed.trim().to_owned()));
        }
    }
    let Some((copied, _)) = Clipboard::read().await else {
        return Err(if terminal {
            SaiError::new(
                Code::InvalidRequest,
                format!("No working clipboard command ({CLIPBOARD_NAMES})"),
            )
        } else {
            unsupported(
                format!(
                    "No terminal and no working clipboard command ({CLIPBOARD_NAMES}). Ask the person to run `sai auth import` in their own terminal."
                ),
                "sai auth import".into(),
            )
        });
    };
    if copied.is_empty() {
        return Err(SaiError::new(
            Code::InvalidRequest,
            "Clipboard is empty; copy the session token from Sai Web and retry",
        ));
    }
    Ok(copied)
}

fn unsupported(message: String, hint: String) -> SaiError {
    SaiError {
        hint: Some(hint),
        exit_code: ExitCode::Unsupported,
        ..SaiError::new(Code::InvalidRequest, message)
    }
}

pub fn help(command: &str) -> Option<String> {
    let help = match command {
        "auth" => {
            r#"  Connect, list, switch, and remove Sai accounts on this machine.

Options:
  --help  Show this message and exit.

Commands:
  import  Connect with a session token copied from Sai Web.
  list    List connected accounts, their expiry, and which one is active.
  login   Connect the account whose key is NAME by signing Keeper's...
  logout  Remove ADDR, or the active account, from this machine.
  renew   Renew ADDR's session with its saved key name, or renew the...
  status  Show the active account, or the --as account, and its expiry...
  use     Make the connected ADDR the active account."#
        }
        "auth import" => {
            r#"  Connect with a session token copied from Sai Web.

  In a terminal, paste the token at the hidden prompt or press Enter to read
  the clipboard. Without a terminal, as when an agent runs this, read the
  clipboard. After storing the token, clear the clipboard if it still holds
  the token. Prints the addr, expiry, and endpoint, never the token.

Options:
  --help  Show this message and exit."#
        }
        "auth list" => {
            r#"  List connected accounts, their expiry, and which one is active.

Options:
  --help  Show this message and exit."#
        }
        "auth login" => {
            r#"  Connect the account whose key is NAME by signing Keeper's challenge.

  NAME is a Foundry keystore in ~/.foundry/keystores (see `cast wallet list`).
  sai fetches Keeper's sign-in challenge, checks that it is a Sai
  `UserTradingSession` for that account, has the key sign it, and stores the
  session Keeper returns. The session lasts as long as Keeper issues it, 30
  days in production, and lets sai trade for that account.

  If the `ud` tool is on PATH and has a wallet named NAME, sai signs through
  `ud evm wallet run`, which supplies the keystore password. Otherwise `cast
  wallet sign` asks for it.

  To connect without a local key, copy a token from Sai Web and run `sai auth
  import`.

  Examples:
    sai auth login trading-bot
        Sign with ~/.foundry/keystores/trading-bot; cast asks for its
        password.
    sai auth login trading-bot --addr 0xYOUR_ADDR
        Same, but stop if the keystore records another addr.
    sai auth import
        Use a session token copied from Sai Web instead of a key.

Options:
  --addr ADDR  EVM addr you expect NAME to hold. Sign-in stops before signing
               if ud or the keystore file reports another addr.
  --help       Show this message and exit."#
        }
        "auth logout" => {
            r#"  Remove ADDR, or the active account, from this machine.

  Other connected accounts stay connected; when the active one is removed,
  none is active until `sai auth use`. Makes no network call. Keeper cannot
  revoke session tokens yet, so a token copied elsewhere stays valid until it
  expires.

Options:
  --help  Show this message and exit."#
        }
        "auth renew" => {
            r#"  Renew ADDR's session with its saved key name, or renew the active account.

Options:
  --help  Show this message and exit."#
        }
        "auth status" => {
            r#"  Show the active account, or the --as account, and its expiry without showing
  the token.

Options:
  --help  Show this message and exit."#
        }
        "auth use" => {
            r#"  Make the connected ADDR the active account.

  Commands without `--as` act on the active account. Sign in again to connect
  a new one: `sai auth login NAME` and `sai auth import` add accounts.

Options:
  --help  Show this message and exit."#
        }
        _ => return None,
    };
    Some(format!("{}\n\n{help}", crate::usage(command)))
}
