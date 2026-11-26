use serde_json::json;

use super::*;

#[test]
fn cursor_verification_leaves_concurrent_launcher_and_args_changes_intact() {
    let fixture = tempfile::tempdir().unwrap();
    let home = fixture.path();
    let path = home.join("mcp.json");
    let old = json!({"command": "/fixture/old/sai", "args": ["mcp", "serve"]});
    let registration = Registration::new(old.clone(), &old, Scope::Project, false, vec![]).unwrap();
    let target = Target::generated("/fixture/new/sai".into(), None);
    let document = |entry: &Value| json!({"mcpServers": {"sai": entry}}).to_string();
    for changed in [
        old.clone(),
        json!({"command": target.argv[0], "args": ["mcp", "serve", "--bogus"]}),
        json!({"command": target.argv[0], "args": ["mcp", "serve"], "url": "https://fixture.invalid/mcp"}),
    ] {
        fs::write(&path, document(&old)).unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        let published = Client::Cursor
            .write_json(
                &path,
                home,
                Scope::Project,
                "sai",
                &target,
                JsonUpdate::Repair(&registration),
                deadline,
            )
            .unwrap();
        // Simulate Cursor sync replacing the exact scope just published.
        let synced = document(&changed);
        let mut replacement = tempfile::NamedTempFile::new_in(home).unwrap();
        replacement.write_all(synced.as_bytes()).unwrap();
        replacement.persist(&path).unwrap();
        let error = Client::verify_cursor_repair(
            &path,
            Scope::Project,
            "sai",
            &target,
            published,
            deadline,
        )
        .unwrap_err();
        assert!(error.contains("Cursor did not load the repaired Sai registration"));
        assert_eq!(fs::read_to_string(&path).unwrap(), synced);
    }
}

#[test]
fn cursor_verification_timeout_restores_its_own_replacement() {
    let fixture = tempfile::tempdir().unwrap();
    let path = fixture.path().join("mcp.json");
    let original =
        r#"{"mcpServers":{"sai":{"command":"/fixture/old/sai","args":["mcp","serve"]}}}"#;
    fs::write(&path, original).unwrap();
    let replacement = original.replace("old", "new");
    let deadline = Instant::now() + Duration::from_secs(3);
    let published = edit(&path, false, deadline, |_| Ok(replacement.clone())).unwrap();
    let target = Target::generated("/fixture/new/sai".into(), None);
    let expired = Instant::now() - Duration::from_millis(1);
    assert!(
        Client::verify_cursor_repair(&path, Scope::User, "sai", &target, published, expired)
            .is_err()
    );
    assert_eq!(fs::read_to_string(path).unwrap(), original);
}
