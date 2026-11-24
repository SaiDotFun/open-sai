use std::{
    env,
    io::{self, IsTerminal, Write},
    os::fd::{AsFd, BorrowedFd},
};

use chrono::{TimeDelta, Utc};
use clap::Subcommand;
use rustix::{
    fs::{OFlags, fcntl_getfl, fcntl_setfl},
    termios::{InputModes, LocalModes, OptionalActions, Termios, tcgetattr, tcsetattr},
};
use sai_core::{
    auth::{self, CLIPBOARD_NAMES, Clipboard, LoginError},
    errors::{Code, ExitCode, SaiError},
    keeper::Keeper,
    log::{self, EventContext},
    session::{ExpiryNotice, session_expiry},
    settings::{Environment, Flags},
    storage::{RemovedSession, SessionSummary, Storage, StorageError, StorageEvent},
};
use serde::Serialize;
use tokio::{
    io::{Interest, unix::AsyncFd},
    signal::unix::{SignalKind, signal},
};
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
    Import {
        /// Require the token to match this EVM address.
        #[arg(long)]
        addr: Option<String>,
    },
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
struct AccountStatus<'a> {
    #[serde(flatten)]
    summary: SessionSummary<'a>,
    #[serde(flatten)]
    expiry_notice: Option<ExpiryNotice>,
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
    #[serde(flatten)]
    expiry_notice: Option<ExpiryNotice>,
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
            let summary = session.summary();
            let summary = serde_json::to_value(summary).map_err(|_| SaiError::internal())?;
            storage.save_session(session)?;
            emit(&summary, json)
        }
        AuthCommand::Import { addr } => {
            runtime()?.block_on(import(&storage, flags, env, json, addr.as_deref()))
        }
        AuthCommand::Renew { addr } => {
            let stored = storage
                .load_session(addr.as_deref().filter(|s| !s.is_empty()).or(as_addr))
                .map_err(account_error)?;
            let settings = load_settings(flags, env)?;
            let endpoint = settings.endpoint();
            if stored.endpoint() != Some(endpoint) {
                return Err(SaiError::new(
                    Code::SessionUnavailable,
                    "Stored session belongs to another Keeper endpoint",
                ));
            }
            let (command, guidance) = storage.renewal_fields(&stored);
            let Some(command) = command else {
                return Err(SaiError::new(
                    Code::InvalidRequest,
                    guidance.unwrap_or_default(),
                ));
            };
            let Some(name) = stored.key_name() else {
                let mut message = if stored.is_imported() {
                    format!("This session was imported. Run `{command}` with a fresh token.")
                } else {
                    format!("This session has no saved key name. Run `{command}`.")
                };
                if let Some(guidance) = guidance {
                    message.push_str(&format!(" {guidance}"));
                }
                return Err(SaiError::new(Code::InvalidRequest, message));
            };
            let keeper = Keeper::new(endpoint, events);
            let session = runtime()?
                .block_on(login(&keeper, name, Some(stored.addr())))
                .map_err(|error| {
                    if matches!(error, LoginError::NeedsTerminal(_)) {
                        let mut message = format!("{error} Run `{command}` in a terminal.");
                        if let Some(guidance) = guidance {
                            message.push_str(&format!(" {guidance}"));
                        }
                        SaiError::new(Code::SessionUnavailable, message)
                    } else {
                        login_failure(error)
                    }
                })?;
            let summary =
                serde_json::to_value(session.summary()).map_err(|_| SaiError::internal())?;
            storage
                .replace_session(&stored, session)
                .map_err(account_error)?;
            emit(&summary, json)
        }
        AuthCommand::Status => {
            let settings = load_settings(flags, env)?;
            let session = storage.load_active_session(settings.endpoint(), as_addr)?;
            let left = session_expiry(&session)? - Utc::now();
            let expiry_notice = storage.expiry_notice(&session, left);
            show_expiry_notice(expiry_notice.as_ref(), json);
            emit(
                &AccountStatus {
                    summary: session.summary(),
                    expiry_notice,
                },
                json,
            )
        }
        AuthCommand::List => {
            let settings = load_settings(flags, env)?;
            let accounts = storage.load_accounts().map_err(account_error)?;
            let now = Utc::now();
            let accounts = accounts
                .iter()
                .map(|(session, active)| {
                    let left = session_expiry(session)? - now;
                    Ok(AccountRow {
                        summary: session.summary(),
                        active,
                        expired: Some(left <= TimeDelta::zero()),
                        endpoint_matches: Some(session.endpoint() == Some(settings.endpoint())),
                        expiry_notice: storage.expiry_notice(session, left),
                    })
                })
                .collect::<Result<Vec<_>, StorageError>>()?;
            if let Some(active) = accounts
                .iter()
                .find(|account| account.active && account.endpoint_matches == Some(true))
            {
                show_expiry_notice(active.expiry_notice.as_ref(), json);
            }
            emit(&AccountList { accounts }, json)
        }
        AuthCommand::Use { addr } => {
            let session = storage.use_session(&addr).map_err(account_error)?;
            emit(
                &AccountRow {
                    summary: session.summary(),
                    active: true,
                    expired: None,
                    endpoint_matches: None,
                    expiry_notice: None,
                },
                json,
            )
        }
        AuthCommand::Logout { addr } => {
            let removed = storage
                .delete_session(addr.as_deref().filter(|s| !s.is_empty()).or(as_addr))
                .map_err(account_error)?;
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

fn show_expiry_notice(notice: Option<&ExpiryNotice>, json: bool) {
    if let Some(notice) = notice
        && !json
        && io::stdin().is_terminal()
        && io::stderr().is_terminal()
    {
        eprintln!("{}", notice.text());
    }
}

// Python exposes account-store decoding failures as ValueError outside status.
fn account_error(error: StorageError) -> SaiError {
    match error {
        StorageError::SessionData(error) => SaiError::new(Code::InvalidRequest, error.0.message),
        error => error.into(),
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
    addr: Option<&str>,
) -> Result<(), SaiError> {
    let mut terminate = signal(SignalKind::terminate()).map_err(|_| SaiError::internal())?;
    let result = tokio::select! {
        biased;
        _ = terminate.recv() => None,
        result = import_with_interrupt(storage, flags, env, json, addr) => Some(result),
    };
    // The import future has been dropped, restoring the terminal if it was in use.
    result.unwrap_or_else(|| terminate_import())
}

fn terminate_import() -> ! {
    // SAFETY: Restore the default disposition for SIGTERM before raising it
    // in this process. Tokio's handler would otherwise consume it again.
    unsafe {
        if libc::signal(libc::SIGTERM, libc::SIG_DFL) != libc::SIG_ERR {
            libc::raise(libc::SIGTERM);
        }
    }
    std::process::exit(128 + libc::SIGTERM);
}

async fn import_with_interrupt(
    storage: &Storage<'_>,
    flags: &Flags,
    env: &Environment,
    json: bool,
    addr: Option<&str>,
) -> Result<(), SaiError> {
    let mut interrupt = signal(SignalKind::interrupt()).map_err(|_| SaiError::internal())?;
    let (token, summary) = tokio::select! {
        biased;
        _ = interrupt.recv() => {
            eprintln!();
            return Err(aborted());
        }
        result = async {
            let token = read_token().await?;
            let settings = load_settings(flags, env)?;
            let session = auth::import_session(token.clone(), settings.endpoint())?;
            if addr.is_some_and(|expected| !expected.eq_ignore_ascii_case(session.addr())) {
                return Err(SaiError::new(Code::InvalidRequest, "The Sai session token is for a different account than --addr"));
            }
            let summary = serde_json::to_value(session.summary()).map_err(|_| SaiError::internal())?;
            log::remember([token.to_string()]);
            storage.save_session_async(session).await?;
            Ok::<_, SaiError>((token, summary))
        } => result?,
    };
    if let Some((copied, tool)) = Clipboard::read().await
        && copied == token
        && !tool.clear().await
    {
        eprintln!("Warning: could not clear the clipboard");
    }
    emit(&summary, json)
}

struct HiddenInput<'a> {
    stdin: &'a io::Stdin,
    original: Termios,
    flags: OFlags,
}

impl<'a> HiddenInput<'a> {
    fn new(stdin: &'a io::Stdin) -> io::Result<Self> {
        let original = tcgetattr(stdin)?;
        let mut hidden = original.clone();
        hidden
            .local_modes
            .remove(LocalModes::ECHO | LocalModes::ECHONL);
        hidden
            .local_modes
            .insert(LocalModes::ICANON | LocalModes::ISIG);
        hidden
            .input_modes
            .remove(InputModes::IGNCR | InputModes::INLCR);
        hidden.input_modes.insert(InputModes::ICRNL);
        let flags = fcntl_getfl(stdin)?;
        let guard = Self {
            stdin,
            original,
            flags,
        };
        tcsetattr(stdin, OptionalActions::Flush, &hidden)?;
        fcntl_setfl(stdin, flags | OFlags::NONBLOCK)?;
        Ok(guard)
    }
}

impl Drop for HiddenInput<'_> {
    fn drop(&mut self) {
        let _ = fcntl_setfl(self.stdin, self.flags);
        let _ = tcsetattr(self.stdin, OptionalActions::Now, &self.original);
    }
}

async fn hidden_line(terminal: &AsyncFd<BorrowedFd<'_>>) -> io::Result<Zeroizing<String>> {
    let mut token = Zeroizing::new(Vec::new());
    let mut buffer = Zeroizing::new([0u8; 1024]);
    loop {
        let size = terminal
            .async_io(Interest::READABLE, |stdin| {
                rustix::io::read(*stdin, &mut buffer[..]).map_err(Into::into)
            })
            .await?;
        if size == 0 && token.is_empty() {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        token.extend_from_slice(&buffer[..size]);
        if size == 0 || token.ends_with(b"\n") {
            break;
        }
    }

    let text = std::str::from_utf8(&token).map_err(io::Error::other)?;
    Ok(Zeroizing::new(text.trim().to_owned()))
}

async fn prompt_token(stdin: &io::Stdin) -> Result<Zeroizing<String>, SaiError> {
    let _hidden = HiddenInput::new(stdin).map_err(|_| aborted())?;
    // SAFETY: BorrowedFd keeps stdin's descriptor valid and unchanged while
    // the registration is live. HiddenInput has already enabled nonblocking reads.
    let terminal = unsafe { AsyncFd::register_with_interest(stdin.as_fd(), Interest::READABLE) }
        .map_err(|_| aborted())?;
    io::stderr()
        .write_all(b"Paste the Sai session token (Enter reads the clipboard): ")
        .and_then(|()| io::stderr().flush())
        .map_err(|_| aborted())?;
    let result = hidden_line(&terminal).await.map_err(|_| aborted());
    eprintln!();
    result
}

fn aborted() -> SaiError {
    SaiError::new(Code::InvalidRequest, "Aborted")
}

async fn read_token() -> Result<Zeroizing<String>, SaiError> {
    let stdin = io::stdin();
    let terminal = stdin.is_terminal();
    if terminal {
        let typed = prompt_token(&stdin).await?;
        if !typed.is_empty() {
            return Ok(typed);
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
  --addr ADDR  Require the token to match this EVM address.
  --help       Show this message and exit."#
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
