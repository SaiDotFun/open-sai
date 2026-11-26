use std::{
    fs, io,
    net::Ipv6Addr,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use thiserror::Error;

use crate::{
    errors::{Code, SaiError},
    paths,
    settings::MAINNET_HTTP,
};

mod ledger;
mod sessions;

pub use ledger::{IntentRow, IntentStatus, StatusUpdateOptions};
pub use sessions::{Accounts, RemovedSession, Session, SessionSummary, is_session_envelope};

#[derive(Debug, Error)]
pub enum StorageError {
    #[error(transparent)]
    Io(#[from] io::Error),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error("Intent database was written by a newer sai version")]
    NewerLedger,
    #[error("Reconcile uncertain intent {0} before submitting another trade")]
    UncertainIntent(String),
    #[error(
        "Earlier open intent {key} may still execute. Check its status with `{hint}` before retrying."
    )]
    RecentOpenPending { key: String, hint: String },
    #[error("{0}")]
    Session(#[from] SessionError),
    #[error("{0}")]
    SessionData(#[source] SessionError),
    #[error("{}", crate::session::INVALID_EXPIRY)]
    InvalidSessionExpiry,
}

#[derive(Debug, Error)]
#[error("{0}")]
pub struct SessionError(pub SaiError);

impl SessionError {
    pub(crate) fn new(message: impl Into<String>) -> Self {
        Self(SaiError::new(Code::SessionUnavailable, message))
    }
}

impl From<SessionError> for SaiError {
    fn from(error: SessionError) -> Self {
        error.0
    }
}

impl From<StorageError> for SaiError {
    fn from(error: StorageError) -> Self {
        match error {
            StorageError::Session(error) | StorageError::SessionData(error) => error.into(),
            StorageError::InvalidSessionExpiry => {
                Self::new(Code::SessionUnavailable, crate::session::INVALID_EXPIRY)
            }
            StorageError::RecentOpenPending { ref key, ref hint } => SaiError {
                intent_key: Some(key.clone()),
                hint: Some(hint.clone()),
                ..Self::new(Code::RecentOpenPending, error.to_string())
            },
            StorageError::NewerLedger | StorageError::UncertainIntent(_) => {
                Self::new(Code::InvalidRequest, error.to_string())
            }
            _ => Self::internal(),
        }
    }
}

/// Persisted account sessions and trade reservations under a single Sai home.
#[derive(Clone)]
pub struct Storage<'a> {
    pub(crate) trader: PathBuf,
    busy_timeout: Duration,
    ledger_deadline: Option<Instant>,
    preserve_ledger_modes: bool,
    events: Option<&'a (dyn Fn(StorageEvent<'_>) + Sync)>,
}

/// Storage events for the shared log writer. State changes are emitted after
/// commit. Endpoints contain only the URL origin.
pub enum StorageEvent<'a> {
    SessionSaved {
        addr: &'a str,
        expires_at: &'a str,
        endpoint: Option<&'a str>,
        connected_accounts: usize,
    },
    SessionActive {
        addr: &'a str,
    },
    SessionRemoved {
        addr: &'a str,
        connected_accounts: usize,
    },
    UnreadableSessionRemoved,
    SessionExpired {
        addr: &'a str,
        expires_at: &'a str,
    },
    IntentReserved {
        key: &'a str,
        kind: &'a str,
        addr: &'a str,
        entity: &'a str,
        endpoint: &'a str,
    },
    OpenBlocked {
        key: &'a str,
        addr: &'a str,
        endpoint: &'a str,
    },
    IntentStatus {
        key: &'a str,
        kind: &'a str,
        previous: &'a str,
        status: IntentStatus,
        addr: &'a str,
        endpoint: &'a str,
    },
}

/// Project a stored endpoint onto the same fields as Python's log.origin.
pub(crate) fn endpoint_origin(endpoint: &str) -> String {
    fn origin(endpoint: &str) -> Option<String> {
        let normalized = endpoint.trim().replace(['\t', '\n', '\r'], "");
        let (scheme, rest) = normalized.split_once("://")?;
        let mut chars = scheme.bytes();
        if !chars.next()?.is_ascii_alphabetic()
            || !chars.all(|c| c.is_ascii_alphanumeric() || b"+.-".contains(&c))
        {
            return None;
        }
        let authority = rest.split(['/', '?', '#']).next()?;
        let (_, host_port) = authority.rsplit_once('@').unwrap_or(("", authority));
        let (host, port) = if let Some(bracketed) = host_port.strip_prefix('[') {
            let (address, suffix) = bracketed.split_once(']')?;
            address.parse::<Ipv6Addr>().ok()?;
            let host = &host_port[..address.len() + 2];
            (
                host,
                suffix
                    .strip_prefix(':')
                    .or_else(|| suffix.is_empty().then_some(""))?,
            )
        } else {
            let (host, port) = host_port.split_once(':').unwrap_or((host_port, ""));
            if host.is_empty()
                || !host
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b".-".contains(&c))
            {
                return None;
            }
            (host, port)
        };
        let mut origin = format!(
            "{}://{}",
            scheme.to_ascii_lowercase(),
            host.to_ascii_lowercase()
        );
        if !port.is_empty() {
            if !port.bytes().all(|c| c.is_ascii_digit()) {
                return None;
            }
            let port = port.parse::<u16>().ok()?;
            origin.push_str(&format!(":{port}"));
        }
        Some(origin)
    }

    origin(endpoint).unwrap_or_else(|| "[redacted url]".into())
}

impl<'a> Storage<'a> {
    pub fn new(sai_home: impl AsRef<Path>) -> Self {
        Self {
            trader: sai_home.as_ref().join("trader"),
            busy_timeout: Duration::from_secs(10),
            ledger_deadline: None,
            preserve_ledger_modes: false,
            events: None,
        }
    }

    /// Bound how long each SQLite lock can wait on this storage handle.
    pub fn with_busy_timeout(mut self, timeout: Duration) -> Self {
        self.busy_timeout = timeout;
        self
    }

    /// Bound ledger writes during one doctor reconciliation run.
    pub fn with_ledger_deadline(mut self, deadline: Instant) -> Self {
        self.ledger_deadline = Some(deadline);
        self
    }

    /// Open only an existing ledger without changing its mode or its directory's mode.
    pub fn with_preserved_ledger_modes(mut self) -> Self {
        self.preserve_ledger_modes = true;
        self
    }

    fn remaining_busy_timeout(&self) -> io::Result<Duration> {
        match self.ledger_deadline {
            Some(end) => end
                .checked_duration_since(Instant::now())
                .filter(|remaining| !remaining.is_zero())
                .map(|remaining| self.busy_timeout.min(remaining))
                .ok_or_else(|| io::ErrorKind::TimedOut.into()),
            None => Ok(self.busy_timeout),
        }
    }

    pub fn with_event_sink(mut self, sink: &'a (dyn Fn(StorageEvent<'_>) + Sync)) -> Self {
        self.events = Some(sink);
        self
    }

    pub fn intent_status_hint(&self, endpoint: &str, account: &str, key: &str) -> String {
        self.trade_command_hint(endpoint, &["intent", "status", account, "--key", key])
    }

    pub fn trade_command_hint(&self, endpoint: &str, args: &[&str]) -> String {
        use crate::session::shell_quote;

        let flag = self.keeper_hint_flag(endpoint);
        let args = args
            .iter()
            .map(|arg| shell_quote(arg))
            .collect::<Vec<_>>()
            .join(" ");
        format!("sai --endpoint {flag} {args}")
    }

    pub(crate) fn keeper_hint_flag(&self, endpoint: &str) -> String {
        if self.keeper_hint_uses_url(endpoint, Some(&endpoint_origin(endpoint))) {
            crate::session::shell_quote(endpoint)
        } else {
            crate::session::ORIGINAL_KEEPER_HINT.into()
        }
    }

    pub(crate) fn keeper_hint_uses_url(&self, endpoint: &str, origin: Option<&str>) -> bool {
        (endpoint == MAINNET_HTTP || origin == Some(endpoint))
            && self
                .trader
                .parent()
                .is_some_and(|home| crate::log::redact(home, endpoint) == endpoint)
    }

    pub(crate) fn emit(&self, event: StorageEvent<'_>) {
        if let Some(sink) = self.events {
            sink(event);
        }
    }

    fn directory(&self) -> io::Result<&Path> {
        paths::profile(self.trader.parent().expect("trader has a profile parent"))?;
        paths::managed(&self.trader)?;
        Ok(&self.trader)
    }
}

fn remove_if_exists(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        result => result,
    }
}
