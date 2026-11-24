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
    symlink(&binary, &launcher).unwrap();
    let record = root.join(RECORD);
    state::atomic_json(&record, &serde_json::json!({"old": null, "new": new})).unwrap();

    let install = Standalone::discover(&binary).unwrap();
    assert!(
        install
            .recover()
            .unwrap_err()
            .contains("first Sai install was rolled back")
    );
    assert!(!launcher.is_symlink());
    assert!(!record.exists());
    assert!(binary.is_file());
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
    symlink(&old, &launcher).unwrap();
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
    assert_eq!(fs::read_link(launcher).unwrap(), old);
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
