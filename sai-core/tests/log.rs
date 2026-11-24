#![cfg(unix)]

use std::{
    fs::{self, File},
    os::unix::fs::{PermissionsExt, symlink},
    path::Path,
    time::{Duration, SystemTime},
};

use chrono::{Days, Utc};
use sai_core::{
    log::{Field, Level, LogWriter, Surface, remember},
    settings::Environment,
};
use serde_json::{Value, json};
use tempfile::tempdir;

fn writer(home: &Path) -> LogWriter {
    LogWriter::start(Environment::new(home.into(), |_| None)).unwrap()
}

fn read(path: &Path, surface: &str) -> Vec<Value> {
    let name = format!("{surface}-{}.jsonl", Utc::now().date_naive());
    fs::read_to_string(path.join("logs").join(name))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

fn finish(writer: LogWriter) {
    assert!(writer.finish(Duration::from_secs(5)));
}

#[test]
fn redaction_applies_to_nested_text_url_origins_arguments_and_untrusted_errors()
{
    let home = tempdir().unwrap();
    let opaque = "opaque-synthetic-token-for-rust-log-tests";
    remember([opaque.to_owned()]);
    let writer = writer(home.path());
    let jwt = "eyJabcdefghi.abcdefghij.abcdefghij";
    let signature = "a".repeat(130);
    let digest = "b".repeat(64);
    writer.event(Surface::Mcp, "fixture.started", Level::Info, [
        ("nested", json!({opaque: [jwt, &signature, &digest]}).into()),
        ("url", Field::Endpoint("https://bob:fixture@EXAMPLE.com:443/path?hide=1".into())),
        ("argv", Field::argv(["--endpoint=https://bob:fixture@EXAMPLE.com:443/path?hide=1 more text".into()])),
        ("error", Field::Untrusted(format!("Bad {digest} https://example.com/key and private text"))),
    ]);
    finish(writer);

    let events = read(home.path(), "mcp");
    assert_eq!(events.len(), 1);
    assert_eq!(
        events[0]["nested"],
        json!({
            "[redacted session token]": ["[redacted session token]", "[redacted hex]", digest]
        })
    );
    assert_eq!(events[0]["url"], "https://example.com:443");
    assert_eq!(
        events[0]["argv"],
        json!(["--endpoint=https://example.com:443"])
    );
    assert_eq!(
        events[0]["error"],
        "Bad [redacted hex] https://example.com [cut]"
    );
    assert_eq!(events[0]["surface"], "mcp");
    assert_eq!(events[0]["level"], "info");
    assert_eq!(events[0]["pid"], std::process::id());
    assert_eq!(events[0]["version"], env!("CARGO_PKG_VERSION"));
    assert_eq!(events[0]["runId"].as_str().unwrap().len(), 32);
    assert!(events[0]["seq"].is_u64());
    assert!(
        chrono::DateTime::parse_from_rfc3339(events[0]["ts"].as_str().unwrap())
            .is_ok()
    );
}

#[test]
fn oversize_event_keeps_common_fields_and_omits_nulls() {
    let home = tempdir().unwrap();
    let logger = writer(home.path());
    let content = "ࢢ".repeat(1950);
    logger.event(
        Surface::Update,
        "fixture.one",
        Level::Warn,
        [
            ("first", Field::Text(content.clone())),
            ("second", Field::Text(content.clone())),
            ("third", Field::Text(content)),
            ("omitted", Value::Null.into()),
        ],
    );
    finish(logger);

    let events = read(home.path(), "update");
    assert_eq!(events[0]["truncated"], json!(["first", "second", "third"]));
    assert_eq!(events[0]["event"], "fixture.one");
    assert!(events[0].get("omitted").is_none());
}

#[test]
fn disabled_or_invalid_log_settings_do_not_create_a_directory() {
    for input in [
        "[logs]\nenabled = false\nkeep_days = 'bad'\n",
        "[logs]\nkeep_days = 0\n",
        "[keeper\n",
    ] {
        let home = tempdir().unwrap();
        fs::write(home.path().join("config.toml"), input).unwrap();
        let logger = writer(home.path());
        logger.event(Surface::Cli, "fixture", Level::Info, []);
        finish(logger);
        assert!(!home.path().join("logs").exists());
    }
}

#[test]
fn retention_prunes_sai_files_once_per_day_across_writers() {
    let home = tempdir().unwrap();
    let logs = home.path().join("logs");
    fs::create_dir(&logs).unwrap();
    fs::write(home.path().join("config.toml"), "[logs]\nkeep_days = 2\n")
        .unwrap();
    let today = Utc::now().date_naive();
    let yesterday = today - Days::new(1);
    let older = today - Days::new(2);
    let oldest = today - Days::new(3);
    let stale = logs.join(format!("mcp-{older}.jsonl"));
    let recent = logs.join(format!("cli-{yesterday}.jsonl"));
    let unrelated = logs.join(format!("other-{oldest}.jsonl"));
    let invalid = logs.join("update-2026-02-30.jsonl");
    let link = logs.join(format!("update-{oldest}.jsonl"));
    for path in [&stale, &recent, &unrelated, &invalid] {
        fs::write(path, "").unwrap();
    }
    symlink(&unrelated, &link).unwrap();

    let logger = writer(home.path());
    logger.event(Surface::Cli, "fixture", Level::Info, []);
    finish(logger);
    assert!(!stale.exists());
    assert!(
        recent.exists()
            && unrelated.exists()
            && invalid.exists()
            && link.exists()
    );

    // A second writer leaves the file in place until the marker expires.
    fs::write(&stale, "").unwrap();
    let logger = writer(home.path());
    logger.event(Surface::Cli, "fixture", Level::Info, []);
    finish(logger);
    assert!(stale.exists());
    let marker = File::options()
        .write(true)
        .open(logs.join(".pruned"))
        .unwrap();
    marker
        .set_modified(SystemTime::now() - Duration::from_secs(86_400))
        .unwrap();
    let logger = writer(home.path());
    logger.event(Surface::Cli, "fixture", Level::Info, []);
    finish(logger);
    assert!(!stale.exists());
}

#[test]
fn output_modes_are_private_and_log_files_do_not_follow_links_or_fifos() {
    let home = tempdir().unwrap();
    let logs = home.path().join("logs");
    let today = Utc::now().date_naive();
    fs::create_dir(&logs).unwrap();
    fs::set_permissions(&logs, fs::Permissions::from_mode(0o755)).unwrap();
    let external = home.path().join("other");
    fs::write(&external, "sentinel").unwrap();
    let symlink_path = logs.join(format!("cli-{today}.jsonl"));
    let hardlink_path = logs.join(format!("mcp-{today}.jsonl"));
    let fifo = logs.join(format!("update-{today}.jsonl"));
    symlink(&external, &symlink_path).unwrap();
    fs::hard_link(&external, &hardlink_path).unwrap();
    rustix::fs::mkfifoat(
        rustix::fs::CWD,
        &fifo,
        rustix::fs::Mode::RUSR | rustix::fs::Mode::WUSR,
    )
    .unwrap();

    let logger = writer(home.path());
    for surface in [Surface::Cli, Surface::Mcp, Surface::Update] {
        logger.event(surface, "fixture", Level::Info, []);
    }
    finish(logger);
    assert_eq!(fs::read_to_string(&external).unwrap(), "sentinel");
    let mode =
        |path: &Path| fs::metadata(path).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode(&logs), 0o700);
    assert_eq!(mode(&logs.join(".pruned")), 0o600);

    fs::remove_file(&symlink_path).unwrap();
    fs::write(&symlink_path, "").unwrap();
    fs::set_permissions(&symlink_path, fs::Permissions::from_mode(0o644))
        .unwrap();
    let logger = writer(home.path());
    logger.event(Surface::Cli, "fixture", Level::Info, []);
    finish(logger);
    assert_eq!(read(home.path(), "cli").len(), 1);
    assert_eq!(mode(&symlink_path), 0o600);
}
