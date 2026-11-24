use std::{
    fs,
    io::{self, Read, Write},
    os::unix::fs::PermissionsExt,
    process::{self, Child, Command, Stdio},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

use rusqlite::Connection;
use sai_core::storage::{
    IntentStatus, RemovedSession, Session, Storage, StorageError, StorageEvent,
};
use tempfile::tempdir;
use zeroize::Zeroizing;

const ENDPOINT: &str = "http://127.0.0.1:9345/query";
const ADDR: &str = "0xABCD";
const KIND: &str = "CLOSE_POSITION";

#[test]
fn sessions_are_private_and_reads_do_not_create_files() {
    let home = tempdir().unwrap();
    let storage = Storage::new(home.path());
    let trader = home.path().join("trader");
    assert_eq!(storage.load_accounts().unwrap().iter().count(), 0);
    assert!(!trader.exists());

    let fixture = "synthetic-session-token";
    let saved = AtomicBool::new(false);
    let sink = |event: StorageEvent<'_>| {
        if let StorageEvent::SessionSaved {
            connected_accounts, ..
        } = event
        {
            assert_eq!(connected_accounts, 1);
            assert!(trader.join("sessions.json").is_file());
            saved.store(true, Ordering::Relaxed);
        }
    };
    storage
        .with_event_sink(&sink)
        .save_session(Session::new(
            Zeroizing::new(fixture.into()),
            ADDR.into(),
            "2099-01-01T00:00:00+00:00".into(),
            ENDPOINT.into(),
        ))
        .unwrap();
    assert!(saved.load(Ordering::Relaxed));

    assert_eq!(
        fs::metadata(&trader).unwrap().permissions().mode() & 0o777,
        0o700
    );
    for name in ["sessions.json", "sessions.lock"] {
        let path = trader.join(name);
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert!(!fs::read_to_string(path).unwrap().contains(fixture));
    }
    let storage = Storage::new(home.path());
    let loaded = storage
        .load_active_session(ENDPOINT, Some("0xabcd"))
        .unwrap();
    assert_eq!(loaded.addr(), ADDR);
    assert!(loaded.token() == fixture);
    assert!(
        storage
            .load_active_session("http://other.invalid", None)
            .is_err()
    );

    fs::write(trader.join("sessions.json"), b"damaged").unwrap();
    assert!(storage.delete_session(Some(ADDR)).is_err());
    assert!(matches!(
        storage.delete_session(None).unwrap(),
        Some(RemovedSession::Unreadable)
    ));
    assert!(!storage.session_exists());
}

#[test]
fn migrates_v0_rows_and_rejects_future_schemas() {
    let home = tempdir().unwrap();
    let trader = home.path().join("trader");
    fs::create_dir(&trader).unwrap();
    let path = trader.join("intents.sqlite3");
    let old = Connection::open(&path).unwrap();
    old.execute_batch(
        "CREATE TABLE intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, account TEXT NOT NULL,
         kind TEXT NOT NULL, status TEXT NOT NULL, entity TEXT NOT NULL DEFAULT '');
         INSERT INTO intents VALUES ('20200101T000000123Z_OLD', 'http://127.0.0.1:9345/query',
         '0xabcd', 'CLOSE_POSITION', 'unknown', '7');",
    )
    .unwrap();
    drop(old);

    let storage = Storage::new(home.path());
    let db = storage.open_ledger().unwrap();
    let version: i64 = db
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap();
    assert_eq!(version, 1);
    let timestamp: Option<f64> = db
        .query_row("SELECT created_at FROM intents", [], |row| row.get(0))
        .unwrap();
    assert_eq!(timestamp, None);
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(
        storage
            .mark_absent(
                "20200101T000000123Z_OLD",
                ENDPOINT,
                ADDR,
                10.0,
                Some(1_577_836_810.0)
            )
            .unwrap()
            .as_deref(),
        Some("unknown")
    );
    assert_eq!(
        storage
            .mark_absent(
                "20200101T000000123Z_OLD",
                ENDPOINT,
                ADDR,
                10.0,
                Some(1_577_836_811.0)
            )
            .unwrap()
            .as_deref(),
        Some("absent")
    );

    db.pragma_update(None, "user_version", 2).unwrap();
    assert!(matches!(
        storage.open_ledger(),
        Err(StorageError::NewerLedger)
    ));
    let version: i64 = db
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap();
    assert_eq!(version, 2);
}

#[test]
fn accepted_intents_block_only_the_same_nonempty_entity() {
    let home = tempdir().unwrap();
    let storage = Storage::new(home.path());
    storage.pending("first", ENDPOINT, ADDR, KIND, "7").unwrap();
    assert!(
        matches!(storage.pending("blocked", ENDPOINT, ADDR, KIND, "8"), Err(StorageError::UncertainIntent(key)) if key == "first")
    );
    storage
        .set_status("first", ENDPOINT, ADDR, IntentStatus::Accepted)
        .unwrap();
    assert!(
        matches!(storage.pending("blocked", ENDPOINT, ADDR, KIND, "7"), Err(StorageError::UncertainIntent(key)) if key == "first")
    );
    storage
        .pending("second", ENDPOINT, "0xabcd", KIND, "8")
        .unwrap();
    storage
        .pending("other-account", ENDPOINT, "0xabcf", KIND, "7")
        .unwrap();
    storage
        .pending("other-endpoint", "http://other.invalid", ADDR, KIND, "7")
        .unwrap();
    storage
        .set_status("second", ENDPOINT, ADDR, IntentStatus::Terminal)
        .unwrap();
    let rows = storage.list_pending(ENDPOINT, ADDR).unwrap();
    let actual: Vec<_> = rows
        .iter()
        .map(|row| (row.key.as_str(), row.status.as_str()))
        .collect();
    assert_eq!(actual, [("second", "terminal"), ("first", "accepted")]);
}

#[test]
fn doctor_can_bound_the_migration_busy_timeout() {
    let home = tempdir().unwrap();
    let storage = Storage::new(home.path());
    let db = storage.open_ledger().unwrap();
    db.pragma_update(None, "user_version", 0).unwrap();
    db.execute_batch("BEGIN IMMEDIATE").unwrap();

    let start = Instant::now();
    assert!(matches!(
        storage.with_busy_timeout(Duration::from_millis(20)).list_pending(ENDPOINT, ADDR),
        Err(StorageError::Sqlite(rusqlite::Error::SqliteFailure(error, _)))
            if error.code == rusqlite::ErrorCode::DatabaseBusy
    ));
    assert!(start.elapsed() < Duration::from_secs(3));
}

#[test]
fn reservation_subprocess() {
    let Ok(key) = std::env::var("SAI_TEST_RESERVE_KEY") else {
        return;
    };
    let home = std::env::var_os("SAI_HOME").unwrap();
    let mut input = io::stdin().lock();
    input.read_exact(&mut [0]).unwrap();
    match Storage::new(home).pending(&key, ENDPOINT, ADDR, KIND, "7") {
        Ok(()) => {
            io::stderr().write_all(b"reserved\n").unwrap();
            if std::env::var_os("SAI_TEST_RESERVE_WAIT").is_some() {
                let _ = input.read_exact(&mut [0]);
            }
        }
        Err(StorageError::UncertainIntent(_)) => process::exit(4),
        Err(error) => panic!("Reserve failed: {error}"),
    }
}

fn child(home: &std::path::Path, key: &str, wait: bool) -> Child {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(["--exact", "reservation_subprocess", "--nocapture"])
        .env("SAI_HOME", home)
        .env("SAI_TEST_RESERVE_KEY", key)
        .env_remove("SAI_TEST_RESERVE_WAIT")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    if wait {
        command.env("SAI_TEST_RESERVE_WAIT", "1");
    }
    command.spawn().unwrap()
}

#[test]
fn two_processes_can_only_reserve_one_intent() {
    let home = tempdir().unwrap();
    let mut left = child(home.path(), "left", false);
    let mut right = child(home.path(), "right", false);
    for child in [&mut left, &mut right] {
        child.stdin.take().unwrap().write_all(b"1").unwrap();
    }
    let mut codes = [left.wait().unwrap().code(), right.wait().unwrap().code()];
    codes.sort();
    assert_eq!(codes, [Some(0), Some(4)]);
    assert_eq!(
        Storage::new(home.path())
            .list_pending(ENDPOINT, ADDR)
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn killing_the_writer_after_reservation_leaves_the_trade_guard_in_place() {
    let home = tempdir().unwrap();
    let mut first = child(home.path(), "first", true);
    first.stdin.as_mut().unwrap().write_all(b"1").unwrap();
    let mut ready = [0; 9];
    first
        .stderr
        .as_mut()
        .unwrap()
        .read_exact(&mut ready)
        .unwrap();
    assert_eq!(&ready, b"reserved\n");
    first.kill().unwrap();
    first.wait().unwrap();

    let storage = Storage::new(home.path());
    assert!(
        matches!(storage.pending("second", ENDPOINT, ADDR, KIND, "8"), Err(StorageError::UncertainIntent(key)) if key == "first")
    );
}
