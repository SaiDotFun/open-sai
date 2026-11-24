use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

use serde_json::{Value, json};
use tempfile::tempdir;

fn sai(home: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_sai"));
    command.env("SAI_HOME", home);
    for name in [
        "SAI_ENDPOINT",
        "SAI_WS_ENDPOINT",
        "SAI_LCD_ENDPOINT",
        "SAI_UPDATE_AUTO",
        "SAI_LOG",
        "SAI_LOG_KEEP_DAYS",
    ] {
        command.env_remove(name);
    }
    command
}

fn read_json(output: &Output) -> Value {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn prints_workspace_version_without_reading_config() {
    let root = tempdir().unwrap();
    fs::write(root.path().join("config.toml"), "invalid TOML").unwrap();
    let output = sai(root.path()).arg("--version").output().unwrap();
    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        format!("sai {}\n", env!("CARGO_PKG_VERSION"))
    );
    assert!(!root.path().join("logs").exists());
}

#[test]
fn lists_values_and_sources_with_redacted_urls_and_unknown_key_warnings() {
    let root = tempdir().unwrap();
    let path = root.path().join("config.toml");
    let contents = "[keeper]\nendpoint = \"https://who:fixture@EXAMPLE.invalid:443/query?token=fixture\"\nfuture_setting = 1\n[update]\nauto = false\n";
    fs::write(&path, contents).unwrap();
    let lcd_origin = "https://lcd.example:8443";
    let lcd = format!("{lcd_origin}/fixture-lcd-path?key=fixture-lcd-query");
    let output = sai(root.path())
        .args(["--lcd-endpoint", &lcd, "--json", "config", "list"])
        .output()
        .unwrap();
    let result = read_json(&output);
    assert_eq!(result["path"], json!(path));
    assert_eq!(
        result["settings"]["keeper.endpoint"],
        json!({
            "value": "https://example.invalid:443",
            "source": "file",
            "env": "SAI_ENDPOINT",
            "default": "https://sai-keeper.nibiru.fi",
        })
    );
    assert_eq!(
        result["settings"]["keeper.ws_endpoint"]["value"],
        "wss://example.invalid:443"
    );
    assert_eq!(
        result["settings"]["keeper.ws_endpoint"]["source"],
        "derived"
    );
    assert_eq!(result["settings"]["update.auto"]["value"], false);
    assert_eq!(result["settings"]["logs.keep_days"]["value"], 14);
    assert_eq!(result["settings"]["lcd.endpoint"]["value"], lcd_origin);
    assert_eq!(result["settings"]["lcd.endpoint"]["source"], "flag");
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("ignoring unknown setting 'keeper.future_setting'")
    );
    assert_eq!(fs::read_to_string(path).unwrap(), contents);

    let logs: String = fs::read_dir(root.path().join("logs"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| {
            path.extension()
                .is_some_and(|extension| extension == "jsonl")
        })
        .map(|path| fs::read_to_string(path).unwrap())
        .collect();
    assert!(logs.contains(lcd_origin));
    for secret in ["fixture-lcd-path", "fixture-lcd-query"] {
        assert!(!String::from_utf8_lossy(&output.stdout).contains(secret));
        assert!(!String::from_utf8_lossy(&output.stderr).contains(secret));
        assert!(!logs.contains(secret));
    }
    let exact = sai(root.path())
        .args(["--lcd-endpoint", &lcd, "config", "get", "lcd.endpoint"])
        .output()
        .unwrap();
    assert!(exact.status.success());
    assert_eq!(String::from_utf8(exact.stdout).unwrap(), format!("{lcd}\n"));
}

#[test]
fn config_set_acknowledges_only_the_endpoint_origin() {
    for (key, scheme) in [
        ("lcd.endpoint", "https"),
        ("keeper.endpoint", "https"),
        ("keeper.ws_endpoint", "wss"),
    ] {
        let root = tempdir().unwrap();
        let origin = format!("{scheme}://provider.example:8443");
        let endpoint = format!("{origin}/fixture-private-path?key=fixture-private-query");
        let set = sai(root.path())
            .args(["--json", "config", "set", key, &endpoint])
            .output()
            .unwrap();
        assert_eq!(read_json(&set)["value"], origin);
        for secret in ["fixture-private-path", "fixture-private-query"] {
            assert!(!String::from_utf8_lossy(&set.stdout).contains(secret));
            assert!(!String::from_utf8_lossy(&set.stderr).contains(secret));
        }
        let get = sai(root.path())
            .args(["config", "get", key])
            .output()
            .unwrap();
        assert_eq!(get.stdout, format!("{endpoint}\n").as_bytes());
        assert!(get.status.success());
    }
}

#[test]
fn missing_relative_config_leaves_home_absent_with_logging_disabled() {
    let root = tempdir().unwrap();
    let output = sai(Path::new("relative-home"))
        .current_dir(root.path())
        .env("SAI_LOG", "off")
        .args(["--json", "config", "list"])
        .output()
        .unwrap();
    let result = read_json(&output);
    assert_eq!(result["path"], "relative-home/config.toml");
    assert_eq!(result["settings"]["keeper.endpoint"]["source"], "default");
    assert!(!root.path().join("relative-home").exists());
}

#[test]
fn malformed_config_fails_without_echoing_file_contents_or_logging() {
    let root = tempdir().unwrap();
    let path = root.path().join("config.toml");
    fs::write(&path, "[keeper]\nendpoint = fixture_secret\n").unwrap();
    let output = sai(root.path())
        .args(["--json", "config", "list"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stderr.is_empty());
    let error = &serde_json::from_slice::<Value>(&output.stdout).unwrap()["error"];
    assert_eq!(error["code"], "INVALID_REQUEST");
    let message = error["message"].as_str().unwrap();
    assert!(message.starts_with(&format!("Cannot parse {}:", path.display())));
    assert!(!message.contains("fixture_secret"));
    assert!(!root.path().join("logs").exists());
}

#[test]
fn get_set_and_unset_use_click_envelopes_and_text() {
    let root = tempdir().unwrap();
    let path = root.path().join("config.toml");
    let output = sai(root.path())
        .env("SAI_UPDATE_AUTO", "yes")
        .args(["--json", "config", "set", "update.auto", "off"])
        .output()
        .unwrap();
    assert_eq!(
        read_json(&output),
        json!({"key": "update.auto", "value": false, "path": path})
    );
    assert_eq!(
        output.stderr,
        b"Warning: SAI_UPDATE_AUTO is set and overrides update.auto in the file\n"
    );

    let output = sai(root.path())
        .args(["config", "get", "update.auto"])
        .output()
        .unwrap();
    assert_eq!(output.stdout, b"false\n");
    let output = sai(root.path())
        .args(["--json", "config", "unset", "update.auto"])
        .output()
        .unwrap();
    assert_eq!(
        read_json(&output),
        json!({"key": "update.auto", "removed": true, "path": path})
    );
    let output = sai(root.path())
        .args(["config", "unset", "update.auto"])
        .output()
        .unwrap();
    assert_eq!(
        output.stdout,
        format!("Already unset: update.auto in {}\n", path.display()).as_bytes()
    );
}

#[test]
fn invalid_value_and_unknown_key_are_usage_errors() {
    let root = tempdir().unwrap();
    for args in [
        &["config", "set", "logs.keep_days", "0"][..],
        &["config", "get", "fixture.unknown"],
    ] {
        let output = sai(root.path()).args(args).output().unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(stderr.starts_with("Usage: sai config "));
        assert!(stderr.contains("Valid keys: keeper.endpoint, keeper.ws_endpoint"));
    }
    assert!(!root.path().join("config.toml").exists());
    assert!(!root.path().join(".config.lock").exists());
}

#[cfg(unix)]
fn logs(home: &Path) -> Vec<Value> {
    let path = fs::read_dir(home.join("logs"))
        .unwrap()
        .flatten()
        .find(|entry| entry.file_name().to_string_lossy().ends_with(".jsonl"))
        .unwrap()
        .path();
    fs::read_to_string(path)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

#[cfg(unix)]
#[test]
fn command_logs_scrub_argv_and_record_exit_errors() {
    let root = tempdir().unwrap();
    let args = [
        "--endpoint=https://user:pw@keeper.example:443/private?q=hidden#also",
        "config",
        "set",
        "logs.keep_days",
        "0",
    ];
    let output = sai(root.path()).args(args).output().unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let events = logs(root.path());
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["event"], "command.start");
    assert_eq!(events[0]["command"], "config set");
    assert_eq!(
        events[0]["argv"][0],
        "--endpoint=https://keeper.example:443"
    );
    assert_eq!(events[1]["event"], "command.end");
    assert_eq!(events[1]["level"], "warn");
    assert_eq!(events[1]["exitCode"], 2);
    assert_eq!(events[1]["seq"], 2);
    assert_eq!(events[0]["runId"], events[1]["runId"]);
    assert!(
        events[1]["error"]
            .as_str()
            .unwrap()
            .contains("Invalid value for logs.keep_days")
    );
}

#[cfg(unix)]
#[test]
fn multiple_processes_append_whole_json_lines() {
    let root = tempdir().unwrap();
    let children: Vec<_> = (0..8)
        .map(|_| {
            sai(root.path())
                .args(["config", "get", "logs.enabled"])
                .stdout(std::process::Stdio::null())
                .spawn()
                .unwrap()
        })
        .collect();
    for mut child in children {
        assert!(child.wait().unwrap().success());
    }

    let events = logs(root.path());
    assert_eq!(events.len(), 16);
    let mut by_run = std::collections::BTreeMap::<&str, Vec<u64>>::new();
    for event in &events {
        let run = event["runId"].as_str().unwrap();
        by_run
            .entry(run)
            .or_default()
            .push(event["seq"].as_u64().unwrap());
    }
    assert_eq!(by_run.len(), 8);
    assert!(by_run.values().all(|seqs| seqs == &[1, 2]));
}

#[cfg(unix)]
#[test]
fn inaccessible_logs_do_not_change_command_output() {
    use std::os::unix::fs::symlink;

    let root = tempdir().unwrap();
    let external = tempdir().unwrap();
    symlink(external.path(), root.path().join("logs")).unwrap();
    let output = sai(root.path())
        .args(["config", "get", "logs.enabled"])
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(output.stdout, b"true\n");
    assert!(output.stderr.is_empty());
    assert_eq!(fs::read_dir(external.path()).unwrap().count(), 0);
}

#[test]
fn multiline_inline_table_is_invalid_in_toml_1_0() {
    let home = tempdir().unwrap();
    let path = home.path().join("config.toml");
    fs::write(
        &path,
        include_str!("fixtures/config-multiline-inline-table.toml"),
    )
    .unwrap();
    let output = sai(home.path())
        .args(["--json", "config", "list"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stderr.is_empty());
    let error = &serde_json::from_slice::<Value>(&output.stdout).unwrap()["error"];
    assert_eq!(error["code"], "INVALID_REQUEST");
    assert!(error["message"].as_str().unwrap().contains("Cannot parse"));
}

#[test]
fn oversized_unknown_integers_do_not_hide_known_keys() {
    let home = tempdir().unwrap();
    let path = home.path().join("config.toml");
    let contents = include_str!("fixtures/config-oversized-unknown-integer.toml");
    fs::write(&path, contents).unwrap();
    let output = sai(home.path())
        .args(["--json", "config", "list"])
        .output()
        .unwrap();
    assert_eq!(
        read_json(&output)["settings"]["update.auto"]["source"],
        "file"
    );
    assert_eq!(fs::read_to_string(path).unwrap(), contents);
}

#[test]
fn config_accepts_the_last_root_flag_value() {
    let home = tempdir().unwrap();
    let output = sai(home.path())
        .args([
            "--endpoint",
            "https://first.example",
            "--endpoint=https://last.example",
            "config",
            "get",
            "keeper.endpoint",
        ])
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(output.stdout, b"https://last.example\n");
    assert!(output.stderr.is_empty());
}

#[test]
fn nested_cli_usage_stays_on_stderr() {
    let home = tempdir().unwrap();
    let output = sai(home.path())
        .args(["config", "list", "extra", "second"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_eq!(output.stderr, b"Usage: sai config list [OPTIONS]\nTry 'sai config list --help' for help.\n\nError: Got unexpected extra arguments (extra second)\n");
}

#[cfg(unix)]
#[test]
fn json_config_escapes_non_utf8_home_bytes_like_python() {
    use std::{ffi::OsStr, os::unix::ffi::OsStrExt};

    let temp = tempdir().unwrap();
    let home = temp
        .path()
        .join(OsStr::from_bytes(b"home-\xff-\xe2\x82-\xc3\xa9-\"-\\"));
    let suffix = r#"home-\udcff-\udce2\udc82-\u00e9-\"-\\/config.toml"#;
    for (command, args) in [
        ("set", &["update.auto", "false"][..]),
        ("list", &[][..]),
        ("unset", &["update.auto"][..]),
    ] {
        let output = sai(&home)
            .env("SAI_LOG", "0")
            .args(["--json", "config", command])
            .args(args)
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        assert!(output.stderr.is_empty());
        let expected = format!("\"path\": \"{}/{suffix}\"", temp.path().display());
        assert!(
            String::from_utf8(output.stdout)
                .unwrap()
                .contains(&expected)
        );
        let config = fs::read_to_string(home.join("config.toml")).unwrap();
        assert_eq!(
            config.lines().any(|line| line == "auto = false"),
            command != "unset"
        );
    }
}

#[cfg(unix)]
#[test]
fn undecodable_environment_values_keep_their_precedence() {
    use std::{ffi::OsStr, os::unix::ffi::OsStrExt};

    let root = tempdir().unwrap();
    let fixture = OsStr::from_bytes(b"\xff");
    let output = sai(root.path())
        .env("SAI_ENDPOINT", fixture)
        .env("SAI_LOG", "0")
        .args(["config", "get", "keeper.endpoint"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(output.stderr, b"Error: SAI_ENDPOINT: Invalid value for keeper.endpoint: expected a URL starting with https:// or http://\n");

    let output = sai(root.path())
        .env("SAI_ENDPOINT", fixture)
        .env("SAI_LOG", fixture)
        .args([
            "--endpoint",
            "https://flag.example",
            "--json",
            "config",
            "list",
        ])
        .output()
        .unwrap();
    let result = read_json(&output);
    assert_eq!(result["settings"]["keeper.endpoint"]["source"], "flag");
    assert_eq!(result["settings"]["logs.enabled"]["value"], false);
    assert_eq!(result["settings"]["logs.enabled"]["source"], "invalid");
    assert_eq!(
        output.stderr,
        b"SAI_LOG: Invalid value for logs.enabled: expected true, false, yes, no, on, off, 1, or 0; logging is off until it is fixed\n",
    );
    assert!(!root.path().join("logs").exists());
}

#[test]
fn invalid_environment_settings_never_echo_values_to_stderr() {
    let home = tempdir().unwrap();
    let invalid = "not-a-setting-value-private-fixture";
    let cases = [
        (
            "SAI_ENDPOINT",
            "keeper.endpoint",
            "a URL starting with https:// or http://",
            1,
        ),
        (
            "SAI_WS_ENDPOINT",
            "keeper.ws_endpoint",
            "a URL starting with wss:// or ws://",
            1,
        ),
        (
            "SAI_LCD_ENDPOINT",
            "lcd.endpoint",
            "a URL starting with https:// or http://",
            1,
        ),
        (
            "SAI_UPDATE_AUTO",
            "update.auto",
            "true, false, yes, no, on, off, 1, or 0",
            1,
        ),
        (
            "SAI_LOG",
            "logs.enabled",
            "true, false, yes, no, on, off, 1, or 0",
            0,
        ),
        (
            "SAI_LOG_KEEP_DAYS",
            "logs.keep_days",
            "a whole number, 1 or more",
            0,
        ),
    ];
    for (variable, key, expected, exit_code) in cases {
        let output = sai(home.path())
            .env(variable, invalid)
            .args(["config", "get", key])
            .output()
            .unwrap();
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert_eq!(output.status.code(), Some(exit_code), "{variable}");
        assert!(!stderr.contains(invalid), "{variable} echoed its value");
        assert!(
            stderr.contains(&format!(
                "{variable}: Invalid value for {key}: expected {expected}"
            )),
            "{stderr}"
        );
    }
}

#[test]
fn rejects_malformed_bracketed_authorities_before_writing() {
    let root = tempdir().unwrap();
    for authority in ["[::1]suffix", "prefix[::1]", "[::1]]", "[::1%zone%foo]"] {
        let endpoint = format!("https://{authority}/path");
        let output = sai(root.path())
            .env("SAI_LOG", "0")
            .args(["config", "set", "keeper.endpoint", &endpoint])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2), "{output:?}");
        assert!(output.stdout.is_empty());
        assert!(!root.path().join("config.toml").exists());
        assert!(!root.path().join(".config.lock").exists());
    }

    let output = sai(root.path())
        .args([
            "--endpoint",
            "https://user:pw@[::1]:8443/path",
            "config",
            "get",
            "keeper.ws_endpoint",
        ])
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(output.stdout, b"wss://user:pw@[::1]:8443/path\n");
}

#[test]
fn repeating_the_key_reports_only_the_extra_operand() {
    let root = tempdir().unwrap();
    let output = sai(root.path())
        .args([
            "--endpoint",
            "get",
            "config",
            "get",
            "logs.enabled",
            "logs.enabled",
        ])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        b"Usage: sai config get [OPTIONS] KEY\nTry 'sai config get --help' for help.\n\nError: Got unexpected extra argument (logs.enabled)\n",
    );
    let events = logs(root.path());
    assert_eq!(
        events[1]["error"],
        "Got unexpected extra argument (logs.enabled)"
    );
}

#[test]
fn usage_errors_keep_json_at_every_supported_option_position() {
    let home = tempdir().unwrap();
    for args in [
        ["--json", "intent", "status"],
        ["intent", "--json", "status"],
        ["intent", "status", "--json"],
        ["auth", "--json", "use"],
        ["auth", "use", "--json"],
    ] {
        let output = sai(home.path()).args(args).output().unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stderr.is_empty());
        let body: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(body["error"]["code"], "INVALID_REQUEST");
        assert_eq!(body["error"]["message"], "Missing argument 'ADDR'.");
    }
    let output = sai(home.path())
        .args(["intent", "status", "--", "--json", "extra"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert!(!output.stderr.is_empty());
}
