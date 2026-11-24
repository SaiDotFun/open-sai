use std::{
    fs::{self, DirBuilder, File, OpenOptions},
    io,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::Duration,
};

use thiserror::Error;

mod ledger;
mod sessions;

pub use ledger::{IntentRow, IntentStatus};
pub use sessions::{Accounts, RemovedSession, Session, SessionSummary};

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
    #[error("{0}")]
    Session(#[from] SessionError),
}

#[derive(Debug, Error)]
#[error("{0}")]
pub struct SessionError(String);

impl SessionError {
    pub(crate) fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

/// Persisted account sessions and trade reservations under a single Sai home.
pub struct Storage<'a> {
    trader: PathBuf,
    busy_timeout: Duration,
    events: Option<&'a (dyn Fn(StorageEvent<'_>) + Sync)>,
}

/// State changes to pass to the shared log writer. The event sink runs only
/// after the operation has committed, and receives no session token.
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
    IntentStatus {
        key: &'a str,
        kind: &'a str,
        previous: &'a str,
        status: IntentStatus,
        addr: &'a str,
        endpoint: &'a str,
    },
}

impl<'a> Storage<'a> {
    pub fn new(sai_home: impl AsRef<Path>) -> Self {
        Self {
            trader: sai_home.as_ref().join("trader"),
            busy_timeout: Duration::from_secs(10),
            events: None,
        }
    }

    /// Bound how long each SQLite lock can wait on this storage handle.
    pub fn with_busy_timeout(mut self, timeout: Duration) -> Self {
        self.busy_timeout = timeout;
        self
    }

    pub fn with_event_sink(mut self, sink: &'a (dyn Fn(StorageEvent<'_>) + Sync)) -> Self {
        self.events = Some(sink);
        self
    }

    pub(crate) fn emit(&self, event: StorageEvent<'_>) {
        if let Some(sink) = self.events {
            sink(event);
        }
    }

    fn directory(&self) -> io::Result<&Path> {
        DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&self.trader)?;
        fs::set_permissions(&self.trader, fs::Permissions::from_mode(0o700))?;
        Ok(&self.trader)
    }
}

fn private_file(path: &Path) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path)?;
    file.set_permissions(fs::Permissions::from_mode(0o600))?;
    Ok(file)
}

fn remove_if_exists(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        result => result,
    }
}
