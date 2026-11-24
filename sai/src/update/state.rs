use std::{
    fs::{self, File, OpenOptions},
    io::{self, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::Path,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use chrono::{SecondsFormat, Utc};
use serde::Serialize;
use serde_json::{Map, Value, json};
use tempfile::NamedTempFile;

const STATE: &str = "update.json";
const STATE_LOCK: &str = "update.lock";
const CHECK_INTERVAL: f64 = 24.0 * 60.0 * 60.0;
const RETRY_INTERVAL: f64 = 15.0 * 60.0;

pub(super) fn private_dir(path: &Path) -> io::Result<()> {
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)
}

pub(super) fn try_lock(path: &Path) -> io::Result<Option<File>> {
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(path)?;
    match file.try_lock() {
        Ok(()) => Ok(Some(file)),
        Err(std::fs::TryLockError::WouldBlock) => Ok(None),
        Err(std::fs::TryLockError::Error(error)) => Err(error),
    }
}

pub(super) async fn lock(path: &Path, wait: Duration) -> io::Result<Option<File>> {
    let deadline = tokio::time::Instant::now() + wait;
    loop {
        if let Some(file) = try_lock(path)? {
            return Ok(Some(file));
        }
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return Ok(None);
        }
        tokio::time::sleep(remaining.min(Duration::from_millis(50))).await;
    }
}

pub(super) fn stamp() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, false)
}

pub(super) fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

pub(super) fn due(state: &Map<String, Value>, now: f64) -> bool {
    let Some(scheduled) = state.get("scheduledAt").and_then(Value::as_f64) else {
        return true;
    };
    let age = now - scheduled;
    let began = state
        .get("startedAt")
        .and_then(Value::as_f64)
        .is_some_and(|started| started >= scheduled);
    age >= CHECK_INTERVAL || (!began && age >= RETRY_INTERVAL)
}

pub(super) fn read(home: &Path) -> Map<String, Value> {
    let data = fs::read(home.join(STATE))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Map<String, Value>>(&bytes).ok())
        .filter(|value| value.get("schema") == Some(&json!(1)));
    data.unwrap_or_default()
}

pub(super) fn atomic_json(path: &Path, value: &impl Serialize) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::other("missing parent directory"))?;
    let mut temp = NamedTempFile::new_in(parent)?;
    serde_json::to_writer(&mut temp, value)?;
    temp.write_all(b"\n")?;
    temp.as_file().sync_all()?;
    temp.persist(path)?;
    sync_dir(parent)
}

pub(super) fn sync_dir(path: &Path) -> io::Result<()> {
    let result = File::open(path)?.sync_all();
    #[cfg(target_os = "macos")]
    if result
        .as_ref()
        .is_err_and(|error| error.kind() == io::ErrorKind::InvalidInput)
    {
        return Ok(());
    }
    result
}

pub(super) fn merge(home: &Path, changes: Map<String, Value>) -> io::Result<()> {
    let mut state = read(home);
    state.extend(changes);
    state.insert("schema".into(), json!(1));
    atomic_json(&home.join(STATE), &state)
}

pub(super) fn try_state_lock(home: &Path) -> io::Result<Option<File>> {
    private_dir(home)?;
    try_lock(&home.join(STATE_LOCK))
}

pub(super) async fn save(home: &Path, changes: Value) -> io::Result<()> {
    private_dir(home)?;
    let Some(_lock) = lock(&home.join(STATE_LOCK), Duration::from_secs(5)).await? else {
        return Err(io::Error::other("update state is busy"));
    };
    let Value::Object(changes) = changes else {
        return Err(io::Error::other("update state changes must be an object"));
    };
    merge(home, changes)
}
