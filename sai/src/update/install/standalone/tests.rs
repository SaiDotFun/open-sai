use std::time::Duration;

use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::TcpListener,
    sync::oneshot,
    time,
};

use super::*;

#[test]
fn recovery_rolls_back_a_first_install_without_an_old_target() {
    let temp = TempDir::new().unwrap();
    let directory = temp.path().canonicalize().unwrap();
    let root = directory.join(ROOT);
    let new = format!("0.2.0-{}", "a".repeat(64));
    let binary = root.join(&new).join("sai");
    state::private_dir(binary.parent().unwrap()).unwrap();
    fs::write(&binary, "candidate").unwrap();
    let launcher = directory.join("sai");
    let selector = root.join(SELECTOR);
    symlink(Path::new(&new).join("sai"), &selector).unwrap();
    symlink(Path::new(ROOT).join(SELECTOR), &launcher).unwrap();
    let original = launcher_entry(&launcher).unwrap();
    let record = root.join(RECORD);
    state::atomic_json(&record, &serde_json::json!({"old": null, "new": new})).unwrap();

    let install = Standalone::discover(&binary).unwrap();
    let _lock = state::try_lock(&install.lock_path()).unwrap().unwrap();
    assert!(
        install
            .recover()
            .unwrap_err()
            .contains("first Sai install was rolled back")
    );
    assert_eq!(launcher_entry(&launcher).unwrap(), original);
    assert!(!selector.is_symlink());
    assert!(!record.exists());
    assert!(binary.is_file());
}

#[test]
fn rollback_stops_when_a_competitor_takes_the_public_launcher() {
    let temp = TempDir::new().unwrap();
    let bin = temp.path().canonicalize().unwrap();
    let root = bin.join(ROOT);
    let old = format!("0.1.0-{}", "a".repeat(64));
    let new = format!("0.2.0-{}", "b".repeat(64));
    for version in [&old, &new] {
        let directory = root.join(version);
        state::private_dir(&directory).unwrap();
        fs::write(directory.join("sai"), "managed binary").unwrap();
    }
    let launcher = bin.join("sai");
    let selector = root.join(SELECTOR);
    let selected = Path::new(&new).join("sai");
    symlink(&selected, &selector).unwrap();
    let public_target = Path::new(ROOT).join(SELECTOR);
    symlink(&public_target, &launcher).unwrap();

    let install = Standalone::discover(&root.join(&new).join("sai")).unwrap();
    let _lock = state::try_lock(&install.lock_path()).unwrap().unwrap();
    let original = install.launcher_identity().unwrap();
    fs::rename(&launcher, bin.join("saved-launcher")).unwrap();
    symlink(&public_target, &launcher).unwrap();
    let competitor = launcher_entry(&launcher).unwrap().unwrap();
    assert_ne!(original, competitor);

    for previous in [None, Some(old.as_str())] {
        assert!(
            install
                .switch_selector(&original, &new, previous)
                .unwrap_err()
                .contains("launcher changed during the update")
        );
        assert_eq!(
            launcher_entry(&launcher).unwrap().as_ref(),
            Some(&competitor)
        );
        assert_eq!(fs::read_link(&selector).unwrap(), selected);
    }
}

#[test]
fn recovery_returns_a_competing_launcher_displaced_by_the_installer() {
    recovery_returns_competitor(false);
}

#[test]
fn recovery_can_resume_after_withdrawing_the_public_candidate() {
    recovery_returns_competitor(true);
}

fn recovery_returns_competitor(withdrawn: bool) {
    use std::os::unix::fs::MetadataExt;

    fn entry(path: &Path) -> (u64, u64, Option<PathBuf>) {
        let info = path.symlink_metadata().unwrap();
        (info.dev(), info.ino(), fs::read_link(path).ok())
    }

    let temp = TempDir::new().unwrap();
    let bin = temp.path().canonicalize().unwrap();
    let root = bin.join(ROOT);
    let old = format!("0.1.0-{}", "a".repeat(64));
    let new = format!("0.2.0-{}", "b".repeat(64));
    for name in [&old, &new] {
        let version = root.join(name);
        state::private_dir(&version).unwrap();
        fs::write(version.join("sai"), "managed binary").unwrap();
    }
    let launcher = bin.join("sai");
    symlink(root.join(&old).join("sai"), &launcher).unwrap();
    let expected = entry(&launcher);
    fs::rename(&launcher, bin.join("previous")).unwrap();
    let binary = root.join(&new).join("sai");
    symlink(&binary, &launcher).unwrap();
    let published = entry(&launcher);
    let staged_name = ".launcher-switch/sai";
    let staged = root.join(staged_name);
    let stage = staged.parent().unwrap();
    state::private_dir(stage).unwrap();
    fs::write(&staged, "competing launcher").unwrap();
    let competitor = entry(&staged);
    let record = root.join(RECORD);
    state::atomic_json(
        &record,
        &serde_json::json!({
            "old": old,
            "new": new,
            "launcher_switch": {
                "staged": staged_name,
                "expected": expected,
                "published": published,
            },
        }),
    )
    .unwrap();

    let install = Standalone::discover(&binary).unwrap();
    if withdrawn {
        fs::rename(&launcher, stage.join("public")).unwrap();
    }
    assert!(
        install
            .recover_staged_launcher()
            .unwrap_err()
            .contains("launcher changed during the switch")
    );
    assert_eq!(entry(&launcher), competitor);
    assert!(!stage.exists());
    assert!(!record.exists());
}

#[test]
fn recovery_cleans_up_after_publishing_the_competing_launcher() {
    let temp = TempDir::new().unwrap();
    let bin = temp.path().canonicalize().unwrap();
    let root = bin.join(ROOT);
    let old = format!("0.1.0-{}", "a".repeat(64));
    let binary = root.join(&old).join("sai");
    state::private_dir(binary.parent().unwrap()).unwrap();
    fs::write(&binary, "managed binary").unwrap();
    let launcher = bin.join("sai");
    symlink(&binary, &launcher).unwrap();
    let expected = launcher_entry(&launcher).unwrap().unwrap();
    fs::remove_file(&launcher).unwrap();

    let staged = root.join(LAUNCHER_STAGE);
    let stage = staged.parent().unwrap();
    state::private_dir(stage).unwrap();
    let target = std::ffi::OsStr::from_bytes(b"competitor-\xff");
    symlink(target, &staged).unwrap();
    let competitor = launcher_entry(&staged).unwrap().unwrap();
    let mut pending = Pending {
        old: Some(old),
        new: format!("0.2.0-{}", "b".repeat(64)),
        launcher_switch: Some(LauncherSwitch {
            staged: LAUNCHER_STAGE.into(),
            expected,
            published: None,
            competitor: None,
        }),
    };
    let record = root.join(RECORD);
    state::atomic_json(&record, &pending).unwrap();
    let install = Standalone::discover(&binary).unwrap();
    install
        .restore_displaced(&mut pending, &staged, stage, &competitor)
        .unwrap();
    // On filesystems that fall back to a hard link, model the same state as
    // exclusive rename by removing the staged name before retrying.
    if staged.symlink_metadata().is_ok() {
        fs::remove_file(&staged).unwrap();
    }
    let recorded = fs::read(&record).unwrap();
    assert_eq!(
        launcher_entry(&launcher).unwrap().as_ref(),
        Some(&competitor)
    );

    let restored = bin.join("restored");
    fs::rename(&launcher, &restored).unwrap();
    symlink(target, &launcher).unwrap();
    let replacement = launcher_entry(&launcher).unwrap();
    assert!(
        install
            .recover_staged_launcher()
            .unwrap_err()
            .contains("no longer matches the recorded competitor")
    );
    assert_eq!(fs::read(&record).unwrap(), recorded);
    assert_eq!(launcher_entry(&launcher).unwrap(), replacement);
    assert!(stage.exists());
    fs::rename(&restored, &launcher).unwrap();

    for stage_exists in [true, false] {
        assert_eq!(stage.exists(), stage_exists);
        if !stage_exists {
            // Model a second interruption after stage cleanup but before record removal.
            fs::write(&record, &recorded).unwrap();
        }
        assert!(
            install
                .recover_staged_launcher()
                .unwrap_err()
                .contains("launcher changed during the switch")
        );
        assert_eq!(
            launcher_entry(&launcher).unwrap().as_ref(),
            Some(&competitor)
        );
        assert!(!record.exists());
        assert!(!stage.exists());
    }
}

#[tokio::test(start_paused = true)]
async fn download_deadline_leaves_the_launcher_untouched() {
    let temp = TempDir::new().unwrap();
    let directory = temp.path().canonicalize().unwrap();
    let installed = Version::new(0, 1, 0);
    let target = Version::new(0, 2, 0);
    let root = directory.join(ROOT);
    let old = root
        .join(format!("{installed}-{}", "a".repeat(64)))
        .join("sai");
    state::private_dir(old.parent().unwrap()).unwrap();
    fs::write(&old, "working installation").unwrap();
    let launcher = directory.join("sai");
    let selector = root.join(SELECTOR);
    symlink(old.strip_prefix(&root).unwrap(), &selector).unwrap();
    symlink(Path::new(ROOT).join(SELECTOR), &launcher).unwrap();
    let install = Standalone::discover(&old).unwrap();

    let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let releases = Releases::new(&base, &base).unwrap();
    let name = release::archive_name(&target).unwrap();
    let asset = b"asset body";
    let manifest = format!("{}  {name}\n", release::sha256(asset));
    let (manifest_ready, manifest_headers) = oneshot::channel();
    let (send_manifest, wait_manifest) = oneshot::channel();
    let (asset_ready, asset_headers) = oneshot::channel();
    let (_hold_asset, wait_asset) = oneshot::channel();
    let server = tokio::spawn(async move {
        hold_response(
            &listener,
            "SHA256SUMS",
            manifest.as_bytes(),
            manifest_ready,
            wait_manifest,
        )
        .await;
        hold_response(&listener, &name, asset, asset_ready, wait_asset).await;
    });

    // Keep the paused clock from auto-advancing while the TCP requests arrive.
    let clock = tokio::spawn(async {
        loop {
            tokio::task::yield_now().await;
        }
    });
    let update = tokio::spawn(async move {
        install
            .change(&releases, &installed, &target, || true)
            .await
    });
    let half = release::DOWNLOAD_TIMEOUT / 2;
    manifest_headers.await.unwrap();
    time::advance(half).await;
    send_manifest.send(()).unwrap();
    asset_headers.await.unwrap();
    time::advance(half + Duration::from_millis(1)).await;
    clock.abort();

    let outcome = time::timeout(Duration::from_secs(1), update)
        .await
        .expect("the total download deadline did not stop the stalled update")
        .unwrap();
    server.abort();
    let Change::Failed {
        reason,
        rolled_back,
    } = outcome
    else {
        panic!("expected a download failure");
    };
    assert!(
        reason.starts_with("timed out downloading the release"),
        "{reason}"
    );
    assert!(!rolled_back);
    assert_eq!(fs::canonicalize(launcher).unwrap(), old);
    assert!(!root.join(RECORD).exists());
}

async fn hold_response(
    listener: &TcpListener,
    name: &str,
    body: &[u8],
    headers_sent: oneshot::Sender<()>,
    send_body: oneshot::Receiver<()>,
) {
    let (stream, _) = listener.accept().await.unwrap();
    let mut stream = BufReader::new(stream);
    let mut line = String::new();
    stream.read_line(&mut line).await.unwrap();
    assert!(line.contains(&format!("/{name} ")), "{line}");
    loop {
        line.clear();
        assert_ne!(stream.read_line(&mut line).await.unwrap(), 0);
        if line == "\r\n" {
            break;
        }
    }
    let mut stream = stream.into_inner();
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(response.as_bytes()).await.unwrap();
    headers_sent.send(()).unwrap();
    if send_body.await.is_ok() {
        stream.write_all(body).await.unwrap();
    }
}
