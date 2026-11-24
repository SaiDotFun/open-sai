use std::{
    env, fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::Path,
    process::Command,
    time::Duration,
};

use sai_core::{
    log::{EventContext, LogWriter, Surface},
    settings::Environment,
};
use serde_json::{Value, json};

use super::{RETIRED_NAME, repair_after_update};

const CODEX: &str = r#"#!/bin/sh
if [ "$3" != sai ]; then
    printf "Error: No MCP server named '%s' found.\n" "$3" >&2
    exit 1
fi
if grep -Fq "$HOME/.local/bin/sai" "$CODEX_HOME/config.toml"; then
    [ ! -f "$HOME/reject-repair" ] || exit 9
    launcher="$HOME/.local/bin/sai"
else
    launcher="$HOME/retired/sai"
fi
printf '{"transport":{"type":"stdio","command":"%s","args":["mcp","serve"]}}\n' "$launcher"
"#;

#[test]
fn standalone_foreground() {
    replay("standalone_foreground", "standalone/current/sai", false);
}

#[test]
fn uv_background() {
    replay(
        "uv_background",
        ".local/share/uv/tools/sai-cli/bin/sai",
        true,
    );
}

fn replay(test: &str, installed: &str, background: bool) {
    // Run with an isolated HOME, PATH and working directory. The test harness
    // can run these tests concurrently without changing its process environment.
    if env::var_os("SAI_MCP_UPDATE_REPLAY").is_some() {
        let home = env::home_dir().unwrap();
        let project = env::current_dir().unwrap();
        check_fixture(&home, &project, installed, background);
        return;
    }

    let fixture = tempfile::tempdir().unwrap();
    let home = fixture.path().join("home");
    let project = fixture.path().join("project");
    fs::create_dir(&home).unwrap();
    fs::create_dir(&project).unwrap();
    let path = env::join_paths([home.join("bin"), "/usr/bin".into(), "/bin".into()]).unwrap();
    let name = format!("{}::{test}", module_path!().split_once("::").unwrap().1);
    let output = Command::new(env::current_exe().unwrap())
        .args(["--exact", &name, "--nocapture"])
        .env_clear()
        .env("SAI_MCP_UPDATE_REPLAY", "1")
        .env("HOME", &home)
        .env("CODEX_HOME", home.join(".codex"))
        .env("PATH", path)
        .current_dir(&project)
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success() && stdout.contains("1 passed"),
        "{stdout}\n{}",
        String::from_utf8_lossy(&output.stderr),
    );
}

fn write(path: &Path, contents: impl AsRef<[u8]>, mode: u32) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, contents).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}

fn cursor_entry(command: &Path) -> Value {
    json!({"mcpServers": {
        "sai": {"command": command, "args": ["mcp", "serve"], "env": {"SAI_HOME": "/fixture/profile"}, "custom": true},
        "unrelated": {"url": "https://example.invalid/fixture-private-endpoint"},
    }})
}

fn check_fixture(home: &Path, project: &Path, installed: &str, background: bool) {
    let installed = home.join(installed);
    write(&installed, "#!/bin/sh\n[ \"$*\" = 'mcp serve' ]\n", 0o755);
    let launcher = home.join(".local/bin/sai");
    fs::create_dir_all(launcher.parent().unwrap()).unwrap();
    symlink(&installed, &launcher).unwrap();
    write(&home.join("bin/sai"), "#!/bin/sh\nexit 9\n", 0o755);
    write(&home.join("bin/codex"), CODEX, 0o755);

    let old = home.join("retired/sai");
    let codex = home.join(".codex/config.toml");
    let codex_bytes = format!(
        "# keep this comment\nmodel = 'fixture'\n\n[mcp_servers.sai]\ncommand = '{}'\nargs = ['mcp', 'serve']\n\n[mcp_servers.sai.env]\nSAI_HOME = '/fixture/profile'\n",
        old.display(),
    );
    write(&codex, &codex_bytes, 0o640);
    if background {
        write(&home.join("reject-repair"), "", 0o600);
    }

    fs::create_dir(home.join(".claude")).unwrap();
    let claude = home.join(".claude.json");
    let claude_bytes = json!({"mcpServers": {
        (RETIRED_NAME): {"command": old, "args": ["mcp", "serve"]},
        "unrelated": {"url": "https://example.invalid/fixture-private-endpoint"},
    }})
    .to_string();
    write(&claude, &claude_bytes, 0o600);

    let pinned = home.join("pinned/sai");
    write(&pinned, "#!/bin/sh\nexit 0\n", 0o755);
    let user_cursor = home.join(".cursor/mcp.json");
    let user_entry = cursor_entry(if background { &old } else { &pinned }).to_string();
    write(
        &user_cursor,
        &user_entry,
        if background { 0o444 } else { 0o640 },
    );
    let project_cursor = project.join(".cursor/mcp.json");
    let project_entry = cursor_entry(&old);
    write(&project_cursor, project_entry.to_string(), 0o640);

    let sai_home = home.join("sai-home");
    fs::create_dir(&sai_home).unwrap();
    let writer = LogWriter::start(Environment::new(sai_home.clone(), |_| None)).unwrap();
    let events = EventContext {
        writer: Some(&writer),
        surface: Surface::Update,
        command: None,
    };
    let result = repair_after_update(&launcher, background, events);
    let fields = result.json_fields();
    let reports = fields["mcp"].as_array().unwrap();
    let report = |client| reports.iter().find(|r| r["client"] == client).unwrap();
    assert_eq!(report("claude")["legacyName"], RETIRED_NAME);
    assert_eq!(
        report("claude")["manualRepairs"],
        json!([{"scope": "user", "name": RETIRED_NAME}])
    );
    assert_eq!(fs::read_to_string(&claude).unwrap(), claude_bytes);
    assert_eq!(fs::read_to_string(&user_cursor).unwrap(), user_entry);
    assert_eq!(
        fs::metadata(&codex).unwrap().permissions().mode() & 0o777,
        0o640
    );

    if background {
        assert_eq!(report("codex")["status"], "fail");
        assert_eq!(fs::read_to_string(&codex).unwrap(), codex_bytes);
        assert_eq!(report("cursor")["status"], "fail");
        assert_eq!(
            fs::read_to_string(&project_cursor).unwrap(),
            project_entry.to_string()
        );
        assert_eq!(
            fs::metadata(user_cursor).unwrap().permissions().mode() & 0o777,
            0o444
        );
        assert_eq!(result.state_fields()["mcpNotes"], json!(["background"]));
        assert!(
            fields["mcpNotes"][0]
                .as_str()
                .unwrap()
                .starts_with("Background updates")
        );
    } else {
        assert_eq!(report("codex")["status"], "fixed");
        let updated = fs::read_to_string(&codex).unwrap();
        assert!(updated.contains(launcher.to_str().unwrap()));
        assert!(updated.contains("# keep this comment"));
        assert!(updated.contains("SAI_HOME = '/fixture/profile'"));
        assert_eq!(report("cursor")["pinned"], true);
        assert_eq!(
            report("cursor")["changes"],
            json!(["Repaired the Sai registration in project scope"])
        );
        let updated: Value = serde_json::from_slice(&fs::read(&project_cursor).unwrap()).unwrap();
        assert_eq!(updated, cursor_entry(&launcher));
        assert!(
            Command::new(&launcher)
                .args(["mcp", "serve"])
                .status()
                .unwrap()
                .success()
        );
        assert!(!fields.contains_key("mcpNotes"));
    }
    writer.flush(Duration::from_secs(2));
    let log_path = fs::read_dir(sai_home.join("logs"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("update-")
        })
        .unwrap();
    let log = fs::read_to_string(log_path).unwrap();
    assert!(log.contains("update.mcp_repair"));
    assert!(!log.contains("fixture-private-endpoint"));
}
