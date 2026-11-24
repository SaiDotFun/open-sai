use std::{fs, process::Command};

use serde_json::{Value, json};
use tempfile::tempdir;

fn sai() -> Command {
    Command::new(env!("CARGO_BIN_EXE_sai"))
}

#[test]
fn prints_workspace_version_without_reading_config() {
    let root = tempdir().unwrap();
    fs::write(root.path().join("config.toml"), "invalid TOML").unwrap();
    let output = sai()
        .env("SAI_HOME", root.path())
        .arg("--version")
        .output()
        .unwrap();

    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        format!("sai {}\n", env!("CARGO_PKG_VERSION"))
    );
}

#[test]
fn lists_recognized_keys_without_values() {
    let root = tempdir().unwrap();
    let path = root.path().join("config.toml");
    let contents = r#"
[keeper]
endpoint = "https://example.invalid/query?token=fixture"
future_setting = "fixture"
[update]
auto = false
"#;
    fs::write(&path, contents).unwrap();
    let output = sai()
        .env("SAI_HOME", root.path())
        .args(["config", "list", "--json"])
        .output()
        .unwrap();

    assert!(output.status.success());
    assert_eq!(
        serde_json::from_slice::<Value>(&output.stdout).unwrap(),
        json!({"path": path, "configuredKeys": ["keeper.endpoint", "update.auto"]})
    );
    assert!(output.stderr.is_empty());
    assert_eq!(fs::read_to_string(path).unwrap(), contents);
}

#[test]
fn missing_relative_config_leaves_sai_home_absent() {
    let root = tempdir().unwrap();
    let output = sai()
        .current_dir(root.path())
        .env("SAI_HOME", "relative-home")
        .args(["--json", "config", "list"])
        .output()
        .unwrap();

    assert!(output.status.success());
    assert_eq!(
        serde_json::from_slice::<Value>(&output.stdout).unwrap(),
        json!({
            "path": std::path::Path::new("relative-home").join("config.toml"),
            "configuredKeys": [],
        })
    );
    assert!(!root.path().join("relative-home").exists());
}

#[test]
fn malformed_config_fails_without_echoing_file_contents() {
    let root = tempdir().unwrap();
    fs::write(
        root.path().join("config.toml"),
        "[keeper]\nendpoint = fixture_secret\n",
    )
    .unwrap();
    let output = sai()
        .env("SAI_HOME", root.path())
        .args(["config", "list", "--json"])
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("config.toml"));
    assert!(!stderr.contains("fixture_secret"));
}
