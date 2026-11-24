use std::{
    env, fs,
    io::{Read, Write},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
};

use sai_core::storage::{IntentStatus, Session, Storage, StorageError};
use serde_json::Value;
use tempfile::tempdir;

const ENDPOINT: &str = "http://127.0.0.1:9345";
const OTHER_ENDPOINT: &str = "http://127.0.0.1:9346";
const ADDR: &str = "0xAA00000000000000000000000000000000000001";
const SECOND: &str = "0xbb00000000000000000000000000000000000002";

fn clean_env(command: &mut Command, home: &Path) {
    command
        .env("SAI_HOME", home)
        .env("SAI_LOG", "0")
        .env("SAI_UPDATE_AUTO", "0")
        .env_remove("SAI_ENDPOINT")
        .env_remove("SAI_WS_ENDPOINT")
        .env_remove("SAI_LCD_ENDPOINT")
        .env_remove("SAI_LOG_KEEP_DAYS");
}

fn rust(home: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_sai"));
    clean_env(&mut command, home);
    command
}

fn uv(home: &Path) -> Command {
    let root = env::var_os("SAI_PYTHON_REFERENCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join(".."));
    let mut command = Command::new("uv");
    command.args(["--quiet", "run", "--project"]).arg(root);
    clean_env(&mut command, home);
    command
}

fn parity(home: &Path, args: &[&str], code: i32) -> Output {
    let rust = rust(home).args(args).output().unwrap();
    let python = uv(home).arg("sai").args(args).output().unwrap();
    assert_eq!(rust.status.code(), Some(code), "Rust {args:?}");
    assert_eq!(python.status.code(), Some(code), "Python {args:?}");
    assert!(rust.stdout == python.stdout, "stdout for {args:?} differs");
    assert!(rust.stderr == python.stderr, "stderr for {args:?} differs");
    rust
}

fn accounts(home: &Path) -> Value {
    let output = parity(home, &["--json", "auth", "list"], 0);
    serde_json::from_slice::<Value>(&output.stdout).unwrap()["accounts"].clone()
}

fn write_cli_fixture(home: &Path, script: &str, args: &[&str]) {
    let output = uv(home)
        .args(["python", "-c", script])
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "Python fixture failed with {}",
        output.status
    );
}

#[test]
fn missing_home_status_and_list_have_no_side_effects() {
    let root = tempdir().unwrap();
    let home = root.path().join("absent");
    let output = rust(&home)
        .args(["--json", "auth", "list"])
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(output.stdout, b"{\"accounts\": []}\n");
    assert!(output.stderr.is_empty());
    let output = rust(&home)
        .args(["--json", "auth", "status"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(6));
    assert!(output.stderr.is_empty());
    let error =
        &serde_json::from_slice::<Value>(&output.stdout).unwrap()["error"];
    assert_eq!(error["code"], "SIGNED_OUT");
    assert_eq!(
        error["message"],
        "No Sai session; run `sai auth import` or `sai auth login NAME`"
    );
    assert!(!home.exists());
}

#[test]
fn auth_output_shows_only_the_origin_but_checks_the_full_endpoint() {
    let home = tempdir().unwrap();
    let endpoint = "http://fixture-user:fixture-password@[::1]:9345/private?key=fixture#secret";
    Storage::new(home.path())
        .save_session(Session::new(
            String::from("synthetic-session").into(),
            ADDR.into(),
            "2099-01-01T00:00:00+0000".into(),
            endpoint.into(),
        ))
        .unwrap();
    for args in [
        &["auth", "status"][..],
        &["auth", "list"][..],
        &["auth", "use", ADDR][..],
    ] {
        let output = rust(home.path())
            .env("SAI_ENDPOINT", endpoint)
            .arg("--json")
            .args(args)
            .output()
            .unwrap();
        assert!(output.status.success(), "{args:?} failed");
        assert!(output.stderr.is_empty());
        let value: Value = serde_json::from_slice(&output.stdout).unwrap();
        let summary = if args[1] == "list" {
            &value["accounts"][0]
        } else {
            &value
        };
        assert!(summary["endpoint"] == "http://[::1]:9345");
        assert_eq!(summary["expiresAt"], "2099-01-01T00:00:00+0000");
    }
    let output = rust(home.path())
        .args(["--endpoint", "http://[::1]:9345", "auth", "status"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        b"Error: Stored session belongs to another Keeper endpoint\n"
    );
}

#[test]
fn local_commands_reject_an_invalid_lcd_setting_before_accessing_storage() {
    let home = tempdir().unwrap();
    let config = home.path().join("config.toml");
    fs::write(&config, "[lcd]\nendpoint = false\n").unwrap();
    let expected = format!(
        "Error: {}: Invalid value for lcd.endpoint: expected a URL string\n",
        config.display()
    );
    for args in [
        &["auth", "list"][..],
        &["auth", "status"][..],
        &["intent", "local", ADDR][..],
    ] {
        let output = rust(home.path()).args(args).output().unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert_eq!(output.stderr, expected.as_bytes());
    }
    assert!(!home.path().join("trader").exists());
}

#[test]
fn url_validation_rejects_malformed_authorities_without_echoing_them() {
    let home = tempdir().unwrap();
    for bad in [
        "https://fixture-user:fixture-password@example.test：80",
        "https://[::1]suffix",
        "https://[::1%]",
    ] {
        let output = rust(home.path())
            .env("SAI_LCD_ENDPOINT", bad)
            .args(["auth", "list"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert!(output.stderr == b"Error: SAI_LCD_ENDPOINT: Invalid value for lcd.endpoint: expected a URL starting with https:// or http://\n");
    }
}

#[test]
fn invalid_settings_diagnostics_omit_supplied_values() {
    let home = tempdir().unwrap();
    let marker = "synthetic-secret-setting-value";
    for (variable, key, code) in [
        ("SAI_UPDATE_AUTO", "update.auto", 1),
        ("SAI_ENDPOINT", "keeper.endpoint", 1),
        ("SAI_WS_ENDPOINT", "keeper.ws_endpoint", 1),
        ("SAI_LCD_ENDPOINT", "lcd.endpoint", 1),
        ("SAI_LOG", "logs.enabled", 0),
        ("SAI_LOG_KEEP_DAYS", "logs.keep_days", 0),
    ] {
        let output = rust(home.path())
            .env("SAI_LOG", "1")
            .env(variable, marker)
            .args(["--json", "auth", "list"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(code), "{variable}");
        let diagnostic = if code == 0 {
            String::from_utf8(output.stderr).unwrap()
        } else {
            assert!(output.stderr.is_empty());
            let error = &serde_json::from_slice::<Value>(&output.stdout)
                .unwrap()["error"];
            assert_eq!(error["code"], "INVALID_REQUEST");
            error["message"].as_str().unwrap().to_owned()
        };
        assert!(!diagnostic.contains(marker), "{variable} echoed its value");
        assert!(diagnostic.contains(variable) && diagnostic.contains(key));
        if variable == "SAI_UPDATE_AUTO" {
            assert!(
                diagnostic
                    .contains("expected true, false, yes, no, on, off, 1, or 0")
            );
        }
    }

    fs::write(
        home.path().join("config.toml"),
        format!("[update]\nauto = {marker}\n"),
    )
    .unwrap();
    let output = rust(home.path()).args(["auth", "list"]).output().unwrap();
    assert_eq!(output.status.code(), Some(1));
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("Cannot parse") && !stderr.contains(marker));
}

#[test]
#[ignore = "requires uv and a Python reference checkout"]
fn invalid_settings_match_python_for_local_commands() {
    let home = tempdir().unwrap();
    let config = home.path().join("config.toml");
    for invalid in [
        "[lcd]\nendpoint = false\n",
        "[update]\nauto = 'off'\n",
        "[keeper]\nws_endpoint = 'http://localhost'\n",
    ] {
        fs::write(&config, invalid).unwrap();
        parity(home.path(), &["--json", "auth", "list"], 1);
        parity(home.path(), &["--json", "auth", "status"], 1);
        parity(home.path(), &["--json", "intent", "local", ADDR], 1);
    }
    fs::remove_file(config).unwrap();
    for flag in ["--ws-endpoint", "--lcd-endpoint"] {
        parity(home.path(), &["--json", flag, "invalid", "auth", "list"], 1);
    }
    assert!(!home.path().join("trader").exists());

    fs::write(
        home.path().join("config.toml"),
        "[future]\nnumber = 9223372036854775809\n[logs]\nkeep_days = 9223372036854775809\n",
    )
    .unwrap();
    parity(home.path(), &["--json", "auth", "list"], 0);
}

#[test]
#[ignore = "requires uv and a Python reference checkout"]
fn python_import_rust_switch_and_python_logout() {
    let root = tempdir().unwrap();
    let home = root.path();
    let clipboard = root.path().join("clipboard-tools");
    fs::create_dir(&clipboard).unwrap();
    // Give Python's real import command a synthetic token through the system
    // clipboard interface. Neither the process arguments nor output carry it.
    for (name, script) in [
        (
            "pbpaste",
            "#!/bin/sh\nexec cat \"$SAI_HOME/clipboard-tools/contents\"\n",
        ),
        (
            "pbcopy",
            "#!/bin/sh\nexec rm \"$SAI_HOME/clipboard-tools/contents\"\n",
        ),
    ] {
        let path = clipboard.join(name);
        fs::write(&path, script).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }
    let path = env::join_paths(
        std::iter::once(clipboard.clone())
            .chain(env::split_paths(&env::var_os("PATH").unwrap_or_default())),
    )
    .unwrap();
    fs::write(
        home.join("config.toml"),
        format!("[keeper]\nendpoint = {ENDPOINT:?}\n"),
    )
    .unwrap();
    for addr in [ADDR, SECOND] {
        // Generate a fixture JWT with future expiry. Python import checks the
        // payload locally and does not submit it to Keeper.
        write_cli_fixture(
            home,
            r#"
import base64
import json
import os
import sys
from pathlib import Path

payload = {"sub": sys.argv[1], "exp": 4070908800, "purpose": "user_intent"}
encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
Path(os.environ["SAI_HOME"], "clipboard-tools", "contents").write_bytes(
    b"synthetic." + encoded + b".synthetic"
)
"#,
            &[addr],
        );
        let output = uv(home)
            .env("PATH", &path)
            .args(["sai", "--json", "auth", "import"])
            .output()
            .unwrap();
        assert!(output.status.success(), "Python import failed");
        assert!(!output.stdout.windows(10).any(|part| part == b"synthetic."));
        assert!(!clipboard.join("contents").exists());
    }

    let rows = accounts(home);
    assert_eq!(rows[0]["addr"], ADDR);
    assert_eq!(rows[1]["active"], true);
    parity(home, &["--json", "--as", ADDR, "auth", "status"], 0);
    parity(home, &["auth", "status"], 0);
    parity(
        home,
        &["--endpoint", OTHER_ENDPOINT, "--json", "auth", "status"],
        1,
    );

    let addr = ADDR.to_lowercase();
    parity(home, &["--json", "auth", "use", &addr], 0);
    let rows = accounts(home);
    assert_eq!(rows[0]["active"], true);
    assert_eq!(rows[1]["active"], false);
    let envelope = home.join("trader/sessions.json");
    assert_eq!(
        fs::metadata(&envelope).unwrap().permissions().mode() & 0o777,
        0o600
    );

    let rust_home = tempdir().unwrap();
    fs::copy(
        home.join("config.toml"),
        rust_home.path().join("config.toml"),
    )
    .unwrap();
    fs::create_dir(rust_home.path().join("trader")).unwrap();
    fs::copy(envelope, rust_home.path().join("trader/sessions.json")).unwrap();
    let rust = rust(rust_home.path())
        .args(["--json", "auth", "logout"])
        .output()
        .unwrap();
    let python = uv(home)
        .args(["sai", "--json", "auth", "logout"])
        .output()
        .unwrap();
    assert!(python.status.success());
    assert_eq!(rust.status.code(), python.status.code());
    assert!(rust.stdout == python.stdout);
    assert!(rust.stderr == python.stderr);
    assert_eq!(accounts(home), accounts(rust_home.path()));
    parity(home, &["--json", "auth", "status"], 1);
    parity(home, &["--json", "auth", "use", "0xMissing"], 1);
}

#[test]
#[ignore = "requires uv and a Python reference checkout"]
fn rust_saved_session_round_trips_in_python() {
    let home = tempdir().unwrap();
    Storage::new(home.path())
        .save_session(Session::new(
            String::from("synthetic-legacy-session").into(),
            ADDR.into(),
            "2000-01-01T00:00:00+00:00".into(),
            ENDPOINT.into(),
        ))
        .unwrap();
    let status = parity(
        home.path(),
        &["--json", "--endpoint", ENDPOINT, "auth", "status"],
        6,
    );
    let error: Value = serde_json::from_slice(&status.stdout).unwrap();
    assert_eq!(error["error"]["code"], "SESSION_EXPIRED");
    let output = parity(
        home.path(),
        &["--json", "--endpoint", ENDPOINT, "auth", "list"],
        0,
    );
    let rows: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rows["accounts"][0]["expired"], true);
    assert_eq!(rows["accounts"][0]["endpointMatches"], true);
    let output = uv(home.path())
        .args(["sai", "--json", "auth", "logout"])
        .output()
        .unwrap();
    assert!(output.status.success());
    assert!(!Storage::new(home.path()).session_exists());
}

#[test]
#[ignore = "requires uv and a Python reference checkout"]
fn rust_reads_and_migrates_python_v2_session() {
    let home = tempdir().unwrap();
    write_cli_fixture(
        home.path(),
        r#"
import base64
import json
import os
import sys
from sai_cli.core import storage
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

addr, endpoint = sys.argv[1:]
nonce = os.urandom(12)
session = {
    "token": "synthetic-v2-fixture",
    "evmAddress": addr,
    "expiresAt": "2099-01-01T00:00:00+0000",
    "endpoint": endpoint,
    "extra": "preserve this field",
}
encrypted = AESGCM(storage._SESSION_KEY).encrypt(
    nonce, json.dumps(session).encode(), storage._V2_AAD
)
path = storage.directory() / "session.json"
path.touch(mode=0o600)
path.write_text(json.dumps({
    "version": 2,
    "backend": "test",
    "nonce": base64.b64encode(nonce).decode(),
    "ciphertext": base64.b64encode(encrypted).decode(),
}))
"#,
        &[ADDR, ENDPOINT],
    );

    parity(
        home.path(),
        &["--json", "--endpoint", ENDPOINT, "auth", "status"],
        0,
    );
    let trader = home.path().join("trader");
    assert!(trader.join("session.json").exists());
    assert!(!trader.join("sessions.json").exists());
    let result = rust(home.path())
        .args(["--json", "auth", "use", ADDR])
        .output()
        .unwrap();
    assert!(result.status.success());
    assert!(!trader.join("session.json").exists());
    assert!(trader.join("sessions.json").exists());
    parity(
        home.path(),
        &["--json", "--endpoint", ENDPOINT, "auth", "status"],
        0,
    );
    write_cli_fixture(
        home.path(),
        r#"
from sai_cli.core import storage
assert storage.load_session()["extra"] == "preserve this field"
assert storage.load_session()["token"] == "synthetic-v2-fixture"
"#,
        &[],
    );
}

#[test]
#[ignore = "requires uv and a Python reference checkout"]
fn python_and_rust_share_ledger_reservations_and_rows() {
    let home = tempdir().unwrap();
    write_cli_fixture(
        home.path(),
        r#"
import sys
from sai_cli.core import storage
storage.pending("from-python", sys.argv[1], sys.argv[2], "OPEN_TRADE", "")
"#,
        &[ENDPOINT, ADDR],
    );
    let storage = Storage::new(home.path());
    assert!(
        storage
            .pending("from-rust", ENDPOINT, ADDR, "CLOSE_POSITION", "8")
            .is_err()
    );
    storage
        .set_status("from-python", ENDPOINT, ADDR, IntentStatus::Terminal)
        .unwrap();
    storage
        .pending("from-rust", ENDPOINT, ADDR, "CLOSE_POSITION", "8")
        .unwrap();
    write_cli_fixture(
        home.path(),
        r#"
import sys
from sai_cli.core import storage

endpoint, addr = sys.argv[1:]
try:
    storage.pending("blocked-in-python", endpoint, addr, "OPEN_TRADE", "")
except ValueError as error:
    assert "from-rust" in str(error)
else:
    raise AssertionError("Python allowed a second pending intent")
"#,
        &[ENDPOINT, ADDR],
    );
    let output = parity(
        home.path(),
        &["--json", "--endpoint", ENDPOINT, "intent", "local", ADDR],
        0,
    );
    let rows: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(rows[0]["intentKey"], "from-rust");
    assert_eq!(rows[0]["status"], "unknown");
    assert_eq!(rows[1]["intentKey"], "from-python");
    assert_eq!(rows[1]["status"], "terminal");
    parity(
        home.path(),
        &["--endpoint", ENDPOINT, "intent", "local", ADDR],
        0,
    );
}

#[test]
#[ignore = "requires uv and a Python reference checkout"]
fn python_and_rust_can_only_reserve_one_intent_at_a_time() {
    let home = tempdir().unwrap();
    let mut python = uv(home.path())
        .args([
            "python",
            "-c",
            r#"
import sys
from sai_cli.core import storage

endpoint, addr = sys.argv[1:]
sys.stdout.write("ready\n")
sys.stdout.flush()
sys.stdin.buffer.read(1)
try:
    storage.pending("python", endpoint, addr, "OPEN_TRADE", "")
except storage.UnresolvedIntent as error:
    if hasattr(storage, "RecentOpenPending"):
        assert isinstance(error, storage.RecentOpenPending)
        assert error.intent_key == "rust"
    else:
        assert "Reconcile uncertain intent rust " in str(error)
    sys.exit(4)
"#,
            ENDPOINT,
            ADDR,
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut ready = [0; 6];
    python
        .stdout
        .as_mut()
        .unwrap()
        .read_exact(&mut ready)
        .unwrap();
    assert_eq!(&ready, b"ready\n");
    python.stdin.take().unwrap().write_all(b"1").unwrap();

    let storage = Storage::new(home.path());
    let reserved = storage.pending("rust", ENDPOINT, ADDR, "OPEN_TRADE", "");
    let python_status = python.wait().unwrap().code();
    match reserved {
        Ok(()) => assert_eq!(python_status, Some(4)),
        Err(StorageError::RecentOpenPending { key, .. }) => {
            assert_eq!(key, "python");
            assert_eq!(python_status, Some(0));
        }
        Err(error) => panic!("Rust reservation failed: {error}"),
    }
    assert_eq!(storage.list_pending(ENDPOINT, ADDR).unwrap().len(), 1);
}

#[test]
#[ignore = "requires uv and a Python reference checkout"]
fn opening_a_second_rust_connection_keeps_the_reservation_locked() {
    let home = tempdir().unwrap();
    let storage = Storage::new(home.path());
    let writer = storage.open_ledger().unwrap();
    writer.execute_batch("BEGIN IMMEDIATE").unwrap();
    let count: i64 = writer
        .query_row("SELECT count(*) FROM intents", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 0);

    // Use the ordinary ledger-open path while the first connection is still
    // in the reservation transaction and holds SQLite's writer lock.
    let reader = storage.open_ledger().unwrap();
    let reader_count: i64 = reader
        .query_row("SELECT count(*) FROM intents", [], |row| row.get(0))
        .unwrap();
    assert_eq!(reader_count, 0);
    write_cli_fixture(
        home.path(),
        r#"
import sqlite3
import sys
from sai_cli.core import storage

endpoint, addr = sys.argv[1:]
with storage.busy_timeout(0):
    try:
        storage.pending("python", endpoint, addr, "OPEN_TRADE", "")
    except sqlite3.OperationalError as error:
        assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
    else:
        raise AssertionError("Python acquired the writer lock during the Rust reservation")
"#,
        &[ENDPOINT, ADDR],
    );
    drop(reader);

    let account = ADDR.to_lowercase();
    writer
        .execute(
            "INSERT INTO intents (id, endpoint, account, kind, status)
         VALUES ('rust', ?1, ?2, 'OPEN_TRADE', 'unknown')",
            [ENDPOINT, &account],
        )
        .unwrap();
    writer.execute_batch("COMMIT").unwrap();
    write_cli_fixture(
        home.path(),
        r#"
import sys
from sai_cli.core import storage

endpoint, addr = sys.argv[1:]
try:
    storage.pending("python", endpoint, addr, "OPEN_TRADE", "")
except storage.UnresolvedIntent as error:
    if hasattr(storage, "RecentOpenPending"):
        assert isinstance(error, storage.RecentOpenPending)
        assert error.intent_key == "rust"
    else:
        assert "Reconcile uncertain intent rust " in str(error)
else:
    raise AssertionError("Python reserved a second intent")
"#,
        &[ENDPOINT, ADDR],
    );
    let rows = storage.list_pending(ENDPOINT, ADDR).unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].key, "rust");
}

#[test]
#[ignore = "requires uv and a Python reference with next-oracle outcomes"]
fn next_oracle_reservations_and_terminal_outcomes_round_trip() {
    let home = tempdir().unwrap();
    let storage = Storage::new(home.path());
    storage
        .pending("from-rust", ENDPOINT, ADDR, "OPEN_TRADE", "")
        .unwrap();
    assert_eq!(
        storage
            .set_status("from-rust", ENDPOINT, ADDR, IntentStatus::Pending)
            .unwrap()
            .as_deref(),
        Some("pending")
    );
    parity(
        home.path(),
        &["--json", "--endpoint", ENDPOINT, "intent", "local", ADDR],
        0,
    );
    write_cli_fixture(
        home.path(),
        r#"
import sys
import time

from sai_cli.core import storage

endpoint, addr = sys.argv[1:]
try:
    storage.pending("blocked", endpoint, addr, "CLOSE_POSITION", "7")
except ValueError as error:
    assert "Reconcile uncertain intent from-rust " in str(error)
else:
    raise AssertionError("Python allowed a new trade during pending execution")

assert storage.set_status("from-rust", endpoint, addr, "accepted") == "pending"
assert storage.set_status("from-rust", endpoint, addr, "terminal") == "terminal"
storage.pending("from-python", endpoint, addr, "OPEN_TRADE", "")
assert storage.mark_absent("from-python", endpoint, addr, 0, time.time() + 1) == "absent"
assert storage.set_status("from-python", endpoint, addr, "accepted", only_if_absent=True) == "pending"
"#,
        &[ENDPOINT, ADDR],
    );
    parity(
        home.path(),
        &["--json", "--endpoint", ENDPOINT, "intent", "local", ADDR],
        0,
    );
    assert!(matches!(
        storage.pending("blocked", ENDPOINT, ADDR, "CLOSE_POSITION", "7"),
        Err(StorageError::UncertainIntent(key)) if key == "from-python"
    ));
    assert_eq!(
        storage
            .set_status("from-python", ENDPOINT, ADDR, IntentStatus::Terminal)
            .unwrap()
            .as_deref(),
        Some("terminal")
    );
    parity(
        home.path(),
        &["--json", "--endpoint", ENDPOINT, "intent", "local", ADDR],
        0,
    );
}
