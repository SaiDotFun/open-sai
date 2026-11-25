use std::{
    collections::HashSet,
    fs::{Metadata, OpenOptions},
    io,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use rusqlite::Connection;
use sai_core::{
    log::EventContext,
    reconcile,
    settings::Settings,
    storage::{Session, Storage},
};
use serde_json::json;

use super::{Check, DEADLINE, TIMEOUT, files, keeper_hint, plural, short};

const FILES: [&str; 4] = [
    "intents.sqlite3",
    "intents.sqlite3-wal",
    "intents.sqlite3-shm",
    "intents.sqlite3-journal",
];
const SCHEMA_VERSION: i64 = 1;

#[derive(PartialEq, Eq)]
struct Mark {
    device: u64,
    inode: u64,
    size: u64,
    seconds: i64,
    nanos: i64,
}

impl From<Metadata> for Mark {
    fn from(info: Metadata) -> Self {
        Self {
            device: info.dev(),
            inode: info.ino(),
            size: info.size(),
            seconds: info.mtime(),
            nanos: info.mtime_nsec(),
        }
    }
}

struct Unknown {
    key: String,
    endpoint: String,
    account: String,
}

struct Snapshot {
    version: i64,
    integrity: String,
    unknown: Vec<Unknown>,
}

enum SnapshotError {
    Changed,
    Unreadable,
}

fn fingerprint(root: &Path) -> io::Result<Vec<Option<Mark>>> {
    [FILES[0], FILES[1], FILES[3]]
        .into_iter()
        .map(|name| match root.join(name).symlink_metadata() {
            Ok(info) if info.is_file() && info.nlink() == 1 => Ok(Some(info.into())),
            Ok(_) => Err(io::ErrorKind::InvalidData.into()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error),
        })
        .collect()
}

fn copy_snapshot(
    root: &Path,
    expected: Option<&Metadata>,
) -> Result<tempfile::TempDir, SnapshotError> {
    for _ in 0..3 {
        let dir = tempfile::tempdir().map_err(|_| SnapshotError::Unreadable)?;
        let before = fingerprint(root).map_err(|_| SnapshotError::Unreadable)?;
        if expected.is_some_and(|info| before[0].as_ref() != Some(&Mark::from(info.clone()))) {
            return Err(SnapshotError::Changed);
        }
        for (name, mark) in [FILES[0], FILES[1], FILES[3]].into_iter().zip(&before) {
            if let Some(mark) = mark {
                let source = match files::open_regular(&root.join(name)) {
                    Ok(source) if source.metadata().ok().map(Mark::from).as_ref() == Some(mark) => {
                        source
                    }
                    _ => break,
                };
                let mut target = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(dir.path().join(name))
                    .map_err(|_| SnapshotError::Unreadable)?;
                io::copy(&mut &source, &mut target).map_err(|_| SnapshotError::Unreadable)?;
            }
        }
        if fingerprint(root).ok().as_ref() == Some(&before) && dir.path().join(FILES[0]).is_file() {
            // If a journal changed between its lstat and open, retry even
            // when it was restored before the final fingerprint.
            if [FILES[0], FILES[1], FILES[3]]
                .into_iter()
                .zip(&before)
                .all(|(name, mark)| mark.is_some() == dir.path().join(name).is_file())
            {
                return Ok(dir);
            }
        }
    }
    Err(SnapshotError::Changed)
}

fn matches_ledger_schema(db: &Connection, version: i64) -> rusqlite::Result<bool> {
    let table: bool = db.query_row(
        "SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type='table' AND name='intents')",
        [],
        |row| row.get(0),
    )?;
    if !table {
        return Ok(false);
    }
    if !matches!(version, 0 | SCHEMA_VERSION) {
        return Ok(false);
    }
    let mut statement = db.prepare("PRAGMA table_info(intents)")?;
    let columns = statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?.to_ascii_uppercase(),
            row.get::<_, i64>(3)?,
            row.get::<_, i64>(5)?,
        ))
    })?;
    let mut required = HashSet::new();
    for column in columns {
        let (name, kind, not_null, primary) = column?;
        match (name.as_str(), kind.as_str(), not_null, primary) {
            ("id", "TEXT", 0, 1)
            | ("endpoint" | "account" | "kind" | "status" | "entity", "TEXT", 1, 0) => {
                required.insert(name);
            }
            ("created_at", "REAL", 0, 0) if version == SCHEMA_VERSION => {
                required.insert(name);
            }
            ("keeper_status", "TEXT", 0, 0) => {}
            _ => return Ok(false),
        }
    }
    Ok(required.len() == if version == 0 { 6 } else { 7 })
}

pub(super) fn has_ledger_schema(root: &Path, expected: &Metadata) -> bool {
    let has_schema = || {
        let private = copy_snapshot(root, Some(expected)).ok()?;
        let db = Connection::open(private.path().join(FILES[0])).ok()?;
        db.busy_timeout(TIMEOUT).ok()?;
        let version = db
            .pragma_query_value(None, "user_version", |row| row.get(0))
            .ok()?;
        matches_ledger_schema(&db, version).ok()
    };
    has_schema().unwrap_or(false)
}

fn read(root: &Path) -> Result<Snapshot, SnapshotError> {
    let private = copy_snapshot(root, None)?;
    let run = || -> rusqlite::Result<Snapshot> {
        let db = Connection::open(private.path().join(FILES[0]))?;
        db.busy_timeout(TIMEOUT)?;
        let version = db.pragma_query_value(None, "user_version", |row| row.get(0))?;
        let integrity: String = db.pragma_query_value(None, "quick_check", |row| row.get(0))?;
        let unknown = if integrity == "ok" && version <= SCHEMA_VERSION {
            if !matches_ledger_schema(&db, version)? {
                return Err(rusqlite::Error::InvalidQuery);
            }
            let mut statement = db.prepare("SELECT id, endpoint, account FROM intents WHERE status='unknown' ORDER BY rowid DESC")?;
            statement
                .query_map([], |row| {
                    Ok(Unknown {
                        key: row.get(0)?,
                        endpoint: row.get(1)?,
                        account: row.get(2)?,
                    })
                })?
                .collect::<Result<Vec<_>, _>>()?
        } else {
            Vec::new()
        };
        Ok(Snapshot {
            version,
            integrity,
            unknown,
        })
    };
    run().map_err(|_| SnapshotError::Unreadable)
}

fn hard_links(root: &Path) -> Vec<PathBuf> {
    FILES
        .into_iter()
        .map(|name| root.join(name))
        .filter(|path| path.symlink_metadata().is_ok_and(|meta| meta.nlink() > 1))
        .collect()
}

fn identity(path: &Path) -> Option<(u64, u64)> {
    let info = path.symlink_metadata().ok()?;
    info.is_file().then(|| (info.dev(), info.ino()))
}

struct Ledger<'a> {
    home: &'a Path,
    root: PathBuf,
    path: PathBuf,
    identity: Option<(u64, u64)>,
}

impl Ledger<'_> {
    fn unchanged(&self) -> bool {
        self.identity.is_some()
            && identity(&self.path) == self.identity
            && files::verify_profile_owner(self.home).is_ok()
            && files::trader_links(self.home, &FILES).is_empty()
            && hard_links(&self.root).is_empty()
    }

    fn moved(&self) -> Check {
        Check::new(
            "intents",
            "fail",
            format!(
                "{} changed during the check; doctor did not write it",
                self.path.display()
            ),
        )
        .fix("Run sai doctor again; replace any link with a real file by hand")
    }

    fn corrupt(&self) -> String {
        format!(
            "Copy {} aside before any trade and restore it from a backup; it records intents that may still execute",
            self.path.display()
        )
    }

    fn fail(
        &self,
        error: SnapshotError,
        details: serde_json::Value,
        changes: Vec<String>,
    ) -> Check {
        match error {
            SnapshotError::Changed => Check::new(
                "intents",
                "warn",
                "Intent ledger changed while reading; another sai process is writing it",
            )
            .fix("Run sai doctor again once other sai commands finish")
            .details(details)
            .changes(changes),
            SnapshotError::Unreadable => {
                Check::new("intents", "fail", "Intent ledger is unreadable")
                    .fix(self.corrupt())
                    .changes(changes)
            }
        }
    }
}

pub(super) fn check(
    home: &Path,
    settings: &Settings,
    session: Option<&Session>,
    fix: bool,
    offline: bool,
    events: EventContext<'_>,
) -> Check {
    let links = files::trader_links(home, &FILES);
    if !links.is_empty() {
        return Check::new(
            "intents",
            "fail",
            format!(
                "{}: trader path is a symlink; doctor does not read or write the ledger through it",
                files::path_list(&links, ", ")
            ),
        )
        .fix(files::SYMLINK_FIX)
        .details(json!({"symlinks": links}));
    }
    let root = home.join("trader");
    let path = root.join(FILES[0]);
    if !path.is_file() {
        return Check::new("intents", "pass", "No intent ledger yet");
    }
    let ledger = Ledger {
        home,
        root,
        identity: identity(&path),
        path,
    };
    let shared = hard_links(&ledger.root);
    if !shared.is_empty() {
        return Check::new(
            "intents",
            "fail",
            format!(
                "{} has other hard links; fix by hand",
                files::path_list(&shared, ", ")
            ),
        )
        .fix("Replace the hard link with a copy of the file")
        .details(json!({"hardLinks": shared}));
    }
    let mut snapshot = match read(&ledger.root) {
        Ok(snapshot) => snapshot,
        Err(error) => return ledger.fail(error, json!({}), Vec::new()),
    };
    if snapshot.version > SCHEMA_VERSION {
        return Check::new(
            "intents",
            "fail",
            format!(
                "Intent ledger has schema {}; this sai reads up to {SCHEMA_VERSION}",
                snapshot.version
            ),
        )
        .fix("Upgrade sai: uv tool upgrade sai-cli")
        .details(json!({"userVersion": snapshot.version}));
    }
    if snapshot.integrity != "ok" {
        return Check::new(
            "intents",
            "fail",
            "Intent ledger failed its integrity check",
        )
        .fix(ledger.corrupt())
        .details(json!({"userVersion": snapshot.version}));
    }

    let mut changes = Vec::new();
    if snapshot.version < SCHEMA_VERSION && fix {
        if !ledger.unchanged() {
            return ledger.moved();
        }
        if Storage::new(home)
            .with_busy_timeout(TIMEOUT)
            .with_preserved_ledger_modes()
            .open_ledger()
            .is_err()
        {
            return ledger.fail(SnapshotError::Unreadable, json!({}), changes);
        }
        changes.push(format!(
            "migrated the ledger from schema {} to {SCHEMA_VERSION}",
            snapshot.version
        ));
        snapshot = match read(&ledger.root) {
            Ok(snapshot) => snapshot,
            Err(error) => return ledger.fail(error, json!({}), changes),
        };
    }
    let endpoint = settings.endpoint();
    let addr = session
        .filter(|session| session.endpoint() == Some(endpoint))
        .map(Session::addr);
    let canonical = addr.map(str::to_lowercase);
    let blocks = |row: &Unknown| {
        canonical.as_deref() == Some(row.account.as_str()) && row.endpoint == endpoint
    };
    let mut blocking = snapshot
        .unknown
        .iter()
        .filter(|row| blocks(row))
        .map(|row| row.key.clone())
        .collect::<Vec<_>>();
    let mut details = json!({"userVersion": snapshot.version});
    if let Some(addr) = addr
        && !blocking.is_empty()
        && fix
        && !offline
    {
        if !ledger.unchanged() {
            return ledger.moved().changes(changes);
        }
        let probe_events = events.without_command();
        if let Some(error) = reconcile_pending(&ledger, endpoint, addr, &blocking, probe_events) {
            details["reconcileError"] = json!(error);
        }
        snapshot = match read(&ledger.root) {
            Ok(snapshot) => snapshot,
            Err(error) => return ledger.fail(error, details, changes),
        };
        let remaining = snapshot
            .unknown
            .iter()
            .map(|row| row.key.as_str())
            .collect::<HashSet<_>>();
        changes.extend(
            blocking
                .iter()
                .filter(|key| !remaining.contains(key.as_str()))
                .map(|key| format!("reconciled intent {key} with Keeper")),
        );
        blocking = snapshot
            .unknown
            .iter()
            .filter(|row| blocks(row))
            .map(|row| row.key.clone())
            .collect();
    }
    let others = snapshot.unknown.iter().filter(|row| !blocks(row));
    let mut accounts = HashSet::new();
    let mut unknown = 0;
    for row in others {
        accounts.insert((&row.endpoint, &row.account));
        unknown += 1;
    }
    details["unknown"] = json!(snapshot.unknown.len());
    details["blocking"] = json!(blocking);
    details["otherAccounts"] = json!({"unknown": unknown, "accounts": accounts.len()});
    let status = if let (Some(key), Some(addr)) = (blocking.first(), addr) {
        let count = blocking.len();
        let noun = if count == 1 {
            "intent blocks"
        } else {
            "intents block"
        };
        Check::new(
            "intents",
            "fail",
            format!("{count} unknown {noun} trading for {}", short(addr)),
        )
        .fix(keeper_hint(
            home,
            endpoint,
            &["intent", "status", addr, "--key", key],
        ))
    } else if unknown != 0 {
        Check::new(
            "intents",
            "warn",
            format!(
                "{} for {} not signed in here; they do not block this session",
                plural(unknown, "unknown intent"),
                plural(accounts.len(), "account")
            ),
        )
        .fix("Sign in as each account and run `sai intent status ADDR`")
    } else if snapshot.version < SCHEMA_VERSION {
        Check::new(
            "intents",
            "warn",
            format!(
                "Intent ledger has schema {}; the next trade migrates it",
                snapshot.version
            ),
        )
        .fix("sai doctor")
    } else if !changes.is_empty() {
        let mut message = changes.join("; ");
        message[..1].make_ascii_uppercase();
        Check::new("intents", "fixed", message)
    } else {
        Check::new("intents", "pass", "Intent ledger is readable")
    };
    status.details(details).changes(changes)
}

fn reconcile_pending(
    ledger: &Ledger<'_>,
    endpoint: &str,
    addr: &str,
    keys: &[String],
    events: EventContext<'_>,
) -> Option<&'static str> {
    let deadline = Instant::now() + DEADLINE;
    let rt = match super::runtime() {
        Ok(rt) => rt,
        Err(_) => return Some("OSError"),
    };
    let sink =
        |event: sai_core::storage::StorageEvent<'_>| crate::local::storage_event(events, event);
    let storage = Storage::new(ledger.home)
        .with_event_sink(&sink)
        .with_busy_timeout(TIMEOUT)
        .with_ledger_deadline(deadline)
        .with_preserved_ledger_modes();
    let result = rt.block_on(async {
        for key in keys {
            if !ledger.unchanged() {
                return Some("LedgerChanged");
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left == Duration::ZERO {
                return Some("no answer within 7 s");
            }
            let reconcile = reconcile::reconcile(
                endpoint,
                addr,
                Some(key),
                left.min(TIMEOUT),
                &storage,
                events,
            );
            match tokio::time::timeout(left, reconcile).await {
                Ok(Ok(_)) => {}
                Ok(Err(_)) if Instant::now() < deadline => {
                    return Some("KeeperError");
                }
                _ => return Some("no answer within 7 s"),
            }
        }
        None
    });
    rt.shutdown_background();
    result
}
