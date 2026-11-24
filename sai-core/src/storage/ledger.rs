use std::{fs, os::unix::fs::PermissionsExt, str::FromStr};

use chrono::{NaiveDateTime, Utc};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior::Immediate, params};
use serde::Serialize;

use super::{Storage, StorageError, StorageEvent, endpoint_origin};

const OPEN_TRADE: &str = "OPEN_TRADE";

const SCHEMA_VERSION: i64 = 1;
const LEDGER_FILE: &str = "intents.sqlite3";
const CREATE_INTENTS: &str = "CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, entity TEXT NOT NULL DEFAULT '', created_at REAL)";

#[derive(Clone, Copy, Eq, PartialEq)]
pub enum IntentStatus {
    Unknown,
    Accepted,
    Pending,
    Terminal,
    Rejected,
    NotSent,
    Absent,
}

impl IntentStatus {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Unknown => "unknown",
            Self::Accepted => "accepted",
            Self::Pending => "pending",
            Self::Terminal => "terminal",
            Self::Rejected => "rejected",
            Self::NotSent => "not_sent",
            Self::Absent => "absent",
        }
    }

    fn follows(self, previous: &str) -> bool {
        matches!(
            (previous, self),
            (
                "unknown",
                Self::Accepted | Self::Pending | Self::Terminal | Self::Rejected | Self::NotSent
            ) | ("accepted" | "absent", Self::Pending | Self::Terminal)
                | ("pending", Self::Terminal)
        )
    }
}

#[derive(Clone, Copy, Default)]
pub struct StatusUpdateOptions {
    /// Explicit evidence of pending execution can repair an unverified terminal row.
    pub repair_legacy_terminal: bool,
    /// Account-wide reconciliation uses this to restore a block after a late admission.
    pub only_if_absent: bool,
}

#[derive(Serialize)]
pub struct IntentRow {
    #[serde(rename = "intentKey")]
    pub key: String,
    pub kind: String,
    pub status: String,
}

fn schema_version(connection: &Connection) -> Result<i64, StorageError> {
    let version = connection.pragma_query_value(None, "user_version", |row| row.get(0))?;
    if version > SCHEMA_VERSION {
        return Err(StorageError::NewerLedger);
    }
    Ok(version)
}

fn has_column(connection: &Connection, name: &str) -> rusqlite::Result<bool> {
    connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('intents') WHERE name = ?1)",
        [name],
        |row| row.get(0),
    )
}

fn migrate(connection: &mut Connection) -> Result<(), StorageError> {
    if schema_version(connection)? == SCHEMA_VERSION && has_column(connection, "keeper_status")? {
        return Ok(());
    }

    let transaction = connection.transaction_with_behavior(Immediate)?;
    if schema_version(&transaction)? < SCHEMA_VERSION {
        transaction.execute(CREATE_INTENTS, [])?;
        if !has_column(&transaction, "created_at")? {
            transaction.execute("ALTER TABLE intents ADD COLUMN created_at REAL", [])?;
        }
        transaction.pragma_update(None, "user_version", SCHEMA_VERSION)?;
    }
    if !has_column(&transaction, "keeper_status")? {
        // Keep user_version 1 so the v0.1.0 client can still open the ledger.
        transaction.execute("ALTER TABLE intents ADD COLUMN keeper_status TEXT", [])?;
    }
    transaction.commit()?;
    Ok(())
}

fn check_open_pending(
    connection: &Connection,
    endpoint: &str,
    account: &str,
    allow_concurrent_open: bool,
    storage: &Storage<'_>,
) -> Result<(), StorageError> {
    let key: Option<String> = connection
        .query_row(
            "SELECT id FROM intents WHERE endpoint=?1 AND account=?2 AND kind=?3
             AND (keeper_status='pending' OR (NOT ?4 AND status IN ('unknown', 'accepted')))
             ORDER BY rowid DESC LIMIT 1",
            params![
                endpoint,
                account.to_lowercase(),
                OPEN_TRADE,
                allow_concurrent_open
            ],
            |row| row.get(0),
        )
        .optional()?;
    if let Some(key) = key {
        let hint = storage.intent_status_hint(endpoint, account, &key);
        return Err(StorageError::RecentOpenPending { key, hint });
    }
    Ok(())
}

fn unix_time() -> f64 {
    let now = Utc::now();
    now.timestamp() as f64 + f64::from(now.timestamp_subsec_nanos()) / 1e9
}

fn key_time(key: &str) -> Option<f64> {
    // Older rows have no created_at. The key starts with a UTC timestamp
    // followed by three millisecond digits.
    let seconds = NaiveDateTime::parse_from_str(key.get(..15)?, "%Y%m%dT%H%M%S").ok()?;
    let fraction = key.get(15..key.len().min(18))?;
    if !fraction.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let fraction = u32::from_str(fraction).ok()?;
    let precision = key.len().saturating_sub(15).min(3) as i32;
    Some(seconds.and_utc().timestamp() as f64 + f64::from(fraction) / 10_f64.powi(precision))
}

impl Storage<'_> {
    /// Find a recorded account and its effective status on this Keeper endpoint.
    pub fn intent_record(
        &self,
        endpoint: &str,
        key: &str,
    ) -> Result<Option<(String, String)>, StorageError> {
        if !self.trader.join(LEDGER_FILE).try_exists()? {
            return Ok(None);
        }
        Ok(self
            .open_ledger()?
            .query_row(
                "SELECT account, COALESCE(keeper_status, status) FROM intents
                 WHERE id=?1 AND endpoint=?2",
                params![key, endpoint],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?)
    }

    /// Open and migrate the ledger.
    pub fn open_ledger(&self) -> Result<Connection, StorageError> {
        let path = self.directory()?.join(LEDGER_FILE);
        // Closing a separate descriptor would release this process's SQLite
        // POSIX locks. The enclosing directory is private even on first open.
        let mut connection = Connection::open(&path)?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
        connection.busy_timeout(self.busy_timeout)?;
        migrate(&mut connection)?;
        Ok(connection)
    }

    /// Reserve before dispatch. An interrupted command leaves an unknown row.
    pub fn pending(
        &self,
        key: &str,
        endpoint: &str,
        account: &str,
        kind: &str,
        entity: &str,
    ) -> Result<(), StorageError> {
        self.reserve(key, endpoint, account, kind, entity, false)
    }

    /// Check early for an unresolved open. Reservation repeats the check under the write lock.
    pub fn check_open_pending(
        &self,
        endpoint: &str,
        account: &str,
        allow_concurrent_open: bool,
    ) -> Result<(), StorageError> {
        let connection = self.open_ledger()?;
        check_open_pending(&connection, endpoint, account, allow_concurrent_open, self)
    }

    /// An explicit override skips unknown or accepted opens, but never Keeper-pending rows.
    pub fn reserve(
        &self,
        key: &str,
        endpoint: &str,
        account: &str,
        kind: &str,
        entity: &str,
        allow_concurrent_open: bool,
    ) -> Result<(), StorageError> {
        let mut connection = self.open_ledger()?;
        let transaction = connection.transaction_with_behavior(Immediate)?;
        let addr = account.to_lowercase();
        let concurrent_open = kind == OPEN_TRADE && allow_concurrent_open;
        if kind == OPEN_TRADE {
            check_open_pending(&transaction, endpoint, account, concurrent_open, self)?;
        }
        let unresolved: Option<String> = transaction
            .query_row(
                "SELECT id FROM intents WHERE endpoint=?1 AND account=?2
                 AND ((status='unknown' AND (NOT ?3 OR kind<>?4))
                 OR keeper_status='pending' OR (status='accepted' AND entity <> '' AND entity=?5)) LIMIT 1",
                params![
                    endpoint,
                    addr,
                    concurrent_open,
                    OPEN_TRADE,
                    entity
                ],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(key) = unresolved {
            return Err(StorageError::UncertainIntent(key));
        }
        transaction.execute(
            "INSERT INTO intents (id, endpoint, account, kind, status, entity, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                key,
                endpoint,
                addr,
                kind,
                IntentStatus::Unknown.as_str(),
                entity,
                unix_time()
            ],
        )?;
        transaction.commit()?;
        self.emit(StorageEvent::IntentReserved {
            key,
            kind,
            addr: account,
            entity,
            endpoint: &endpoint_origin(endpoint),
        });
        Ok(())
    }

    pub fn set_status(
        &self,
        key: &str,
        endpoint: &str,
        account: &str,
        status: IntentStatus,
    ) -> Result<Option<String>, StorageError> {
        self.set_status_with(
            key,
            endpoint,
            account,
            status,
            StatusUpdateOptions::default(),
        )
    }

    /// Advance a local row to an allowed Keeper status and report its resulting status.
    pub fn set_status_with(
        &self,
        key: &str,
        endpoint: &str,
        account: &str,
        status: IntentStatus,
        options: StatusUpdateOptions,
    ) -> Result<Option<String>, StorageError> {
        if !self.trader.join(LEDGER_FILE).try_exists()? {
            return Ok(None);
        }
        let mut connection = self.open_ledger()?;
        let transaction = connection.transaction_with_behavior(Immediate)?;
        let addr = account.to_lowercase();
        let current: Option<(String, String, Option<String>)> = transaction
            .query_row(
                "SELECT kind, status, keeper_status FROM intents WHERE id=?1 AND endpoint=?2 AND account=?3",
                params![key, endpoint, addr],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        let Some((kind, stored, keeper_status)) = current else {
            return Ok(None);
        };
        let previous = keeper_status
            .as_deref()
            .filter(|status| !status.is_empty())
            .unwrap_or(&stored);
        if options.only_if_absent && previous != IntentStatus::Absent.as_str() {
            return Ok(Some(previous.into()));
        }
        let status =
            if previous == IntentStatus::Absent.as_str() && status == IntentStatus::Accepted {
                IntentStatus::Pending
            } else {
                status
            };
        let legacy_terminal = stored == IntentStatus::Terminal.as_str() && keeper_status.is_none();
        let repair = legacy_terminal
            && (status == IntentStatus::Terminal
                || (status == IntentStatus::Pending && options.repair_legacy_terminal));
        if !status.follows(previous) && !repair {
            return Ok(Some(previous.into()));
        }
        let (stored, keeper_status) = match status {
            // Older clients treat unknown as an account-wide block.
            IntentStatus::Pending => (IntentStatus::Unknown.as_str(), Some(status.as_str())),
            IntentStatus::Terminal => (status.as_str(), Some(status.as_str())),
            _ => (status.as_str(), None),
        };
        transaction.execute(
            "UPDATE intents SET status=?1, keeper_status=?2 WHERE id=?3 AND endpoint=?4 AND account=?5",
            params![stored, keeper_status, key, endpoint, addr],
        )?;
        transaction.commit()?;
        if previous != status.as_str() {
            self.emit(StorageEvent::IntentStatus {
                key,
                kind: &kind,
                previous,
                status,
                addr: account,
                endpoint: &endpoint_origin(endpoint),
            });
        }
        Ok(Some(status.as_str().into()))
    }

    /// Mark an unknown row absent after Keeper returned no record and its
    /// admission window has elapsed. Return the status after the check.
    pub fn mark_absent(
        &self,
        key: &str,
        endpoint: &str,
        account: &str,
        older_than: f64,
        now: Option<f64>,
    ) -> Result<Option<String>, StorageError> {
        let now = now.unwrap_or_else(unix_time);
        let mut connection = self.open_ledger()?;
        let transaction = connection.transaction_with_behavior(Immediate)?;
        let addr = account.to_lowercase();
        let current: Option<(String, Option<f64>, String)> = transaction
            .query_row(
                "SELECT COALESCE(keeper_status, status), created_at, kind FROM intents
                 WHERE id=?1 AND endpoint=?2 AND account=?3",
                params![key, endpoint, addr],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        let Some((previous, timestamp, kind)) = current else {
            return Ok(None);
        };
        if previous != IntentStatus::Unknown.as_str()
            || !timestamp
                .or_else(|| key_time(key))
                .is_some_and(|created| now - created > older_than)
        {
            return Ok(Some(previous));
        }
        let status = IntentStatus::Absent;
        transaction.execute(
            "UPDATE intents SET status=?1 WHERE id=?2 AND endpoint=?3 AND account=?4",
            params![status.as_str(), key, endpoint, addr],
        )?;
        transaction.commit()?;
        self.emit(StorageEvent::IntentStatus {
            key,
            kind: &kind,
            previous: &previous,
            status,
            addr: account,
            endpoint: &endpoint_origin(endpoint),
        });
        Ok(Some(status.as_str().into()))
    }

    /// List every local intent for the account, newest first.
    pub fn list_pending(
        &self,
        endpoint: &str,
        account: &str,
    ) -> Result<Vec<IntentRow>, StorageError> {
        let connection = self.open_ledger()?;
        let mut statement = connection.prepare(
            "SELECT id, kind, COALESCE(keeper_status, status) FROM intents
             WHERE endpoint=?1 AND account=?2 ORDER BY rowid DESC",
        )?;
        let rows = statement.query_map(params![endpoint, account.to_lowercase()], |row| {
            Ok(IntentRow {
                key: row.get(0)?,
                kind: row.get(1)?,
                status: row.get(2)?,
            })
        })?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }
}
