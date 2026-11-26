use std::{
    env, fs,
    io::{IsTerminal, Read, Write},
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::Path,
    process::{Command, Stdio},
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
    if [ -f "$HOME/.cursor/pending-save.json" ]; then
        mv "$HOME/.cursor/pending-save.json" "$HOME/.cursor/mcp.json" || exit 10
    fi
    launcher="$HOME/.local/bin/sai"
else
    launcher="$HOME/retired/sai"
fi
printf '{"transport":{"type":"stdio","command":"%s","args":["mcp","serve"]}}\n' "$launcher"
"#;

#[test]
fn standalone_foreground() {
    replay("standalone");
}

#[test]
fn uv_background() {
    replay("uv-background");
}

#[test]
fn uv_background_preserves_cursor_save() {
    replay("uv-success");
}

#[test]
#[ignore = "fixture subprocess invoked by the replay tests"]
fn replay_child() {
    let mut request = String::new();
    if std::io::stdin().is_terminal() || std::io::stdin().read_to_string(&mut request).unwrap() == 0
    {
        return;
    }
    let (root, mode) = request.split_once('\n').unwrap();
    let root = Path::new(root);
    assert!(root.is_absolute());
    assert_eq!(
        fs::symlink_metadata(root).unwrap().permissions().mode() & 0o777,
        0o700
    );
    let home = root.join("home");
    let project = root.join("project");
    assert_eq!(home.canonicalize().unwrap(), home);
    assert_eq!(project.canonicalize().unwrap(), project);
    assert!(fs::read_dir(&home).unwrap().next().is_none());
    assert!(fs::read_dir(&project).unwrap().next().is_none());
    assert_eq!(env::home_dir().as_ref(), Some(&home));
    assert_eq!(env::current_dir().unwrap(), project);
    assert_eq!(
        env::var_os("CODEX_HOME").as_deref(),
        Some(home.join(".codex").as_os_str())
    );
    let (installed, background, reject) = match mode {
        "standalone" => ("standalone/current/sai", false, false),
        "uv-background" => (".local/share/uv/tools/sai-cli/bin/sai", true, true),
        "uv-success" => (".local/share/uv/tools/sai-cli/bin/sai", true, false),
        _ => panic!("Unknown update fixture mode"),
    };
    check_fixture(&home, &project, installed, background, reject);
}

fn replay(mode: &str) {
    // Create the private home in the parent. The child receives the fixture
    // path through stdin, so an inherited environment cannot select it.
    let fixture = tempfile::Builder::new()
        .permissions(fs::Permissions::from_mode(0o700))
        .tempdir()
        .unwrap();
    let root = fixture.path().canonicalize().unwrap();
    let home = root.join("home");
    let project = root.join("project");
    fs::create_dir(&home).unwrap();
    fs::create_dir(&project).unwrap();
    let path = env::join_paths([home.join("bin"), "/usr/bin".into(), "/bin".into()]).unwrap();
    let mut child = Command::new(env::current_exe().unwrap())
        .args([
            "--ignored",
            "--exact",
            &replay_test_name("replay_child"),
            "--nocapture",
        ])
        .env_clear()
        .env("HOME", &home)
        .env("CODEX_HOME", home.join(".codex"))
        .env("PATH", path)
        .current_dir(&project)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    write!(child.stdin.take().unwrap(), "{}\n{mode}", root.display()).unwrap();
    assert_replay(child.wait_with_output().unwrap());
}

fn replay_test_name(test: &str) -> String {
    format!("{}::{test}", module_path!().split_once("::").unwrap().1)
}

#[test]
fn inherited_replay_environment_cannot_write_to_ambient_home() {
    let ambient = tempfile::tempdir().unwrap();
    let home = ambient.path().join("home");
    let project = ambient.path().join("project");
    fs::create_dir(&project).unwrap();
    let codex = home.join(".codex/config.toml");
    let launcher = home.join("bin/sai");
    write(&codex, "ambient fixture", 0o600);
    write(&launcher, "ambient launcher", 0o755);
    let output = Command::new(env::current_exe().unwrap())
        .args(["--exact", &replay_test_name("standalone_foreground")])
        .env_clear()
        .env("HOME", &home)
        .env("CODEX_HOME", home.join(".codex"))
        .env("PATH", "/usr/bin:/bin")
        .env("SAI_MCP_UPDATE_REPLAY", "0")
        .current_dir(&project)
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert_eq!(fs::read_to_string(codex).unwrap(), "ambient fixture");
    assert_eq!(fs::read_to_string(launcher).unwrap(), "ambient launcher");
    assert_replay(output);
}

fn assert_replay(output: std::process::Output) {
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

fn check_fixture(home: &Path, project: &Path, installed: &str, background: bool, reject: bool) {
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
    if reject {
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
        if reject { 0o444 } else { 0o640 },
    );
    let project_cursor = project.join(".cursor/mcp.json");
    let project_entry = cursor_entry(&old);
    write(&project_cursor, project_entry.to_string(), 0o640);

    // The fixture Codex process publishes a Cursor save during background repair.
    let cursor_save = if background && !reject {
        let staged = home.join(".cursor/pending-save.json");
        let mut document = cursor_entry(&old);
        document["mcpServers"]["new-server"] = json!({"url": "http://127.0.0.1:8765/mcp"});
        let bytes = document.to_string();
        write(&staged, &bytes, 0o600);
        Some((bytes, fs::metadata(staged).unwrap().ino()))
    } else {
        None
    };

    let sai_home = home.join("sai-home");
    fs::create_dir(&sai_home).unwrap();
    let writer = LogWriter::start(Environment::new(sai_home.clone(), |_| None)).unwrap();
    let events = EventContext::new(Some(&writer), Surface::Update, None);
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
    assert_eq!(
        fs::metadata(&codex).unwrap().permissions().mode() & 0o777,
        0o640
    );

    if reject {
        assert_eq!(report("codex")["status"], "fail");
        assert_eq!(fs::read_to_string(&codex).unwrap(), codex_bytes);
        assert_eq!(report("cursor")["status"], "fail");
        assert_eq!(fs::read_to_string(&user_cursor).unwrap(), user_entry);
        assert_eq!(
            fs::metadata(&user_cursor).unwrap().permissions().mode() & 0o777,
            0o444
        );
    } else {
        assert_eq!(report("codex")["status"], "fixed");
        let updated = fs::read_to_string(&codex).unwrap();
        assert!(updated.contains(launcher.to_str().unwrap()));
        assert!(updated.contains("# keep this comment"));
        assert!(updated.contains("SAI_HOME = '/fixture/profile'"));
    }
    if let Some((bytes, inode)) = cursor_save {
        assert_eq!(fs::read_to_string(&user_cursor).unwrap(), bytes);
        assert_eq!(fs::metadata(&user_cursor).unwrap().ino(), inode);
        assert_eq!(report("cursor")["status"], "fail");
        assert_eq!(report("cursor")["changes"], json!([]));
        assert!(
            report("cursor")["fix"]
                .as_str()
                .unwrap()
                .contains("sai doctor")
        );
    } else if !background {
        assert_eq!(report("cursor")["pinned"], true);
        assert_eq!(fs::read_to_string(&user_cursor).unwrap(), user_entry);
        assert_eq!(
            report("cursor")["changes"],
            json!(["Repaired the Sai registration in project scope"])
        );
        let updated: Value = serde_json::from_slice(&fs::read(&project_cursor).unwrap()).unwrap();
        assert_eq!(updated, cursor_entry(&launcher));
        let saved = &updated["mcpServers"]["sai"];
        let args = saved["args"]
            .as_array()
            .unwrap()
            .iter()
            .map(|arg| arg.as_str().unwrap());
        assert!(
            Command::new(saved["command"].as_str().unwrap())
                .args(args)
                .status()
                .unwrap()
                .success()
        );
    }
    if background {
        assert_eq!(
            fs::read_to_string(&project_cursor).unwrap(),
            project_entry.to_string()
        );
        assert_eq!(result.state_fields()["mcpNotes"], json!(["background"]));
        assert!(
            fields["mcpNotes"][0]
                .as_str()
                .unwrap()
                .starts_with("Background updates")
        );
    } else {
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
