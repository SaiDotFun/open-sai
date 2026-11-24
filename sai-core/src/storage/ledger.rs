use std::{fs, os::unix::fs::PermissionsExt, str::FromStr};

use chrono::{NaiveDateTime, Utc};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior::Immediate, params};
use serde::Serialize;

use super::{Storage, StorageError, StorageEvent, endpoint_origin};

const SCHEMA_VERSION: i64 = 1;
const CREATE_INTENTS: &str = "CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, entity TEXT NOT NULL DEFAULT '', created_at REAL)";

#[derive(Clone, Copy)]
pub enum IntentStatus {
    Unknown,
    Accepted,
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
            Self::Terminal => "terminal",
            Self::Rejected => "rejected",
            Self::NotSent => "not_sent",
            Self::Absent => "absent",
        }
    }
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

fn migrate(connection: &mut Connection) -> Result<(), StorageError> {
    if schema_version(connection)? == SCHEMA_VERSION {
        return Ok(());
    }

    let transaction = connection.transaction_with_behavior(Immediate)?;
    if schema_version(&transaction)? < SCHEMA_VERSION {
        transaction.execute(CREATE_INTENTS, [])?;
        let has_timestamp: bool = transaction.query_row(
            "SELECT EXISTS(SELECT 1 FROM pragma_table_info('intents') WHERE name = 'created_at')",
            [],
            |row| row.get(0),
        )?;
        if !has_timestamp {
            transaction.execute("ALTER TABLE intents ADD COLUMN created_at REAL", [])?;
        }
        transaction.pragma_update(None, "user_version", SCHEMA_VERSION)?;
    }
    transaction.commit()?;
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
    /// Open and migrate the ledger.
    pub fn open_ledger(&self) -> Result<Connection, StorageError> {
        let path = self.directory()?.join("intents.sqlite3");
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
        let mut connection = self.open_ledger()?;
        let transaction = connection.transaction_with_behavior(Immediate)?;
        let addr = account.to_lowercase();
        let unresolved: Option<String> = transaction
            .query_row(
                "SELECT id FROM intents WHERE endpoint=?1 AND account=?2
                 AND (status=?3 OR (status=?4 AND entity <> '' AND entity=?5)) LIMIT 1",
                params![
                    endpoint,
                    addr,
                    IntentStatus::Unknown.as_str(),
                    IntentStatus::Accepted.as_str(),
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
    ) -> Result<(), StorageError> {
        let mut connection = self.open_ledger()?;
        let transaction = connection.transaction_with_behavior(Immediate)?;
        let addr = account.to_lowercase();
        let current: Option<(String, String)> = transaction
            .query_row(
                "SELECT kind, status FROM intents WHERE id=?1 AND endpoint=?2 AND account=?3",
                params![key, endpoint, addr],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        transaction.execute(
            "UPDATE intents SET status=?1 WHERE id=?2 AND endpoint=?3 AND account=?4",
            params![status.as_str(), key, endpoint, addr],
        )?;
        transaction.commit()?;
        if let Some((kind, previous)) = current
            && previous != status.as_str()
        {
            self.emit(StorageEvent::IntentStatus {
                key,
                kind: &kind,
                previous: &previous,
                status,
                addr: account,
                endpoint: &endpoint_origin(endpoint),
            });
        }
        Ok(())
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
                "SELECT status, created_at, kind FROM intents WHERE id=?1 AND endpoint=?2 AND account=?3",
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
            "SELECT id, kind, status FROM intents WHERE endpoint=?1 AND account=?2 ORDER BY rowid DESC",
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
