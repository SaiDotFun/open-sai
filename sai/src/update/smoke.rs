use std::{ffi::OsStr, path::Path, process::Stdio, time::Duration};

use semver::Version;
use serde_json::{Value, json};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::Command,
    time::timeout,
};

use super::install::Result;

const VERIFY_TIMEOUT: Duration = Duration::from_secs(60);
const UV_TIMEOUT: Duration = Duration::from_secs(600);
const MAX_OUTPUT: u64 = 64 << 10;

pub(super) async fn uv_output(command: Command, step: &str) -> Result<String> {
    output(command, step, UV_TIMEOUT).await
}

async fn output(mut command: Command, step: &str, budget: Duration) -> Result<String> {
    timeout(budget, async {
        let mut child = command
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|err| format!("could not run {step}: {err}"))?;
        let mut buffer = Vec::new();
        child
            .stdout
            .take()
            .expect("piped stdout")
            .take(MAX_OUTPUT + 1)
            .read_to_end(&mut buffer)
            .await
            .map_err(|_| format!("could not read {step} output"))?;
        if buffer.len() as u64 > MAX_OUTPUT {
            return Err(format!("{step} produced too much output"));
        }
        if !child.wait().await.is_ok_and(|status| status.success()) {
            return Err(format!("{step} failed"));
        }
        String::from_utf8(buffer).map_err(|_| format!("{step} returned invalid UTF-8"))
    })
    .await
    .map_err(|_| format!("{step} timed out"))?
}

fn sai(path: &Path, home: &Path, shim_base: Option<&str>) -> Command {
    let mut command = Command::new(path);
    command
        .current_dir(home)
        .env("SAI_HOME", home)
        .env("SAI_LOG", "false")
        .env("CI", "1");
    if let Some(base) = shim_base {
        command.env("SAI_NATIVE_RELEASE_BASE", base);
    }
    command
}

pub(super) async fn version(path: &Path, home: &Path, shim_base: Option<&str>) -> Result<Version> {
    let mut command = sai(path, home, shim_base);
    command.arg("--version");
    let stdout = output(command, "`sai --version`", VERIFY_TIMEOUT).await?;
    stdout
        .trim()
        .strip_prefix("sai ")
        .or_else(|| stdout.trim().strip_prefix("sai, version "))
        .and_then(super::release::version)
        .ok_or_else(|| "`sai --version` did not report a release version".into())
}

pub(super) async fn verify(
    path: &Path,
    home: &Path,
    expected: &Version,
    shim_base: Option<&str>,
) -> Result<()> {
    if version(path, home, shim_base).await? != *expected {
        return Err(format!("`sai --version` did not report {expected}"));
    }
    let mut command = sai(path, home, shim_base);
    command.args(["config", "get", "update.auto"]);
    output(command, "`sai config get update.auto`", VERIFY_TIMEOUT).await?;
    timeout(VERIFY_TIMEOUT, mcp(path, home, shim_base))
        .await
        .map_err(|_| "`sai mcp serve` timed out during initialization".to_owned())?
}

async fn mcp(path: &Path, home: &Path, shim_base: Option<&str>) -> Result<()> {
    let invalid = || "`sai mcp serve` did not initialize".to_owned();
    let mut child = sai(path, home, shim_base)
        .args(["mcp", "serve"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .map_err(|_| invalid())?;
    let mut stdin = child.stdin.take().expect("piped stdin");
    let mut stdout = BufReader::new(child.stdout.take().expect("piped stdout").take(MAX_OUTPUT));
    let request = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "sai-update", "version": "1.0.0"},
        },
    });
    stdin
        .write_all(format!("{request}\n").as_bytes())
        .await
        .map_err(|_| invalid())?;
    let mut line = Vec::new();
    stdout
        .read_until(b'\n', &mut line)
        .await
        .map_err(|_| invalid())?;
    let reply: Value = serde_json::from_slice(&line).map_err(|_| invalid())?;
    if line.last() != Some(&b'\n')
        || reply["id"] != 1
        || reply["jsonrpc"] != "2.0"
        || reply["result"]["protocolVersion"]
            .as_str()
            .is_none_or(str::is_empty)
        || reply["result"]["serverInfo"]["name"]
            .as_str()
            .is_none_or(str::is_empty)
        || !reply["result"]["capabilities"].is_object()
    {
        return Err(invalid());
    }
    drop(stdin);
    if !child.wait().await.is_ok_and(|status| status.success()) {
        return Err(invalid());
    }
    Ok(())
}

pub(super) fn find_executable(name: &OsStr) -> Option<std::path::PathBuf> {
    use std::{env, fs, os::unix::fs::PermissionsExt};

    env::split_paths(&env::var_os("PATH")?)
        .map(|directory| directory.join(name))
        .find(|path| {
            fs::metadata(path)
                .is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
        })
        .and_then(|path| std::path::absolute(path).ok())
}
