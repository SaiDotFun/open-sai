use std::{
    env,
    fs::{self, Metadata},
    path::{Path, PathBuf},
    process::Stdio,
    time::{Duration, SystemTime},
};

use tokio::{io::AsyncReadExt, process::Command, time::timeout};

pub(super) const RUNNING_VERSION: &str = env!("CARGO_PKG_VERSION");

pub(super) struct Installation {
    launcher: Option<PathBuf>,
    running: Option<FileStamp>,
}

impl Installation {
    pub(super) fn detect() -> Self {
        let executable = env::current_exe().ok();
        let launcher = executable.as_deref().and_then(launcher_path).or(executable);
        let running = launcher.as_deref().and_then(FileStamp::read);
        Self { launcher, running }
    }

    pub(super) fn directory(&self) -> Option<&Path> {
        self.launcher.as_deref().and_then(Path::parent)
    }

    pub(super) async fn version(&self) -> Option<String> {
        let path = self.launcher.as_deref()?;
        if self.running.as_ref() == FileStamp::read(path).as_ref() {
            return self.running.as_ref().map(|_| RUNNING_VERSION.to_owned());
        }
        timeout(Duration::from_secs(1), probe_version(path))
            .await
            .ok()?
    }
}

pub(crate) fn launcher_path(executable: &Path) -> Option<PathBuf> {
    let invoked = PathBuf::from(env::args_os().next()?);
    let belongs_to_process =
        |path: &Path| path.canonicalize().is_ok_and(|target| target == executable);
    if invoked.components().count() > 1 || invoked.is_absolute() {
        let path = std::path::absolute(invoked).ok()?;
        return belongs_to_process(&path).then_some(path);
    }
    env::split_paths(&env::var_os("PATH")?)
        .filter_map(|dir| std::path::absolute(dir.join(&invoked)).ok())
        .find(|path| belongs_to_process(path))
}

#[derive(PartialEq, Eq)]
struct FileStamp {
    modified: Option<SystemTime>,
    length: u64,
    #[cfg(unix)]
    inode: (u64, u64),
}

impl FileStamp {
    fn read(path: &Path) -> Option<Self> {
        let file: Metadata = fs::metadata(path).ok()?;
        #[cfg(unix)]
        use std::os::unix::fs::MetadataExt;
        Some(Self {
            modified: file.modified().ok(),
            length: file.len(),
            #[cfg(unix)]
            inode: (file.dev(), file.ino()),
        })
    }
}

async fn probe_version(path: &Path) -> Option<String> {
    let mut child = Command::new(path)
        .arg("--version")
        .env_clear()
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .ok()?;
    let mut output = String::new();
    child
        .stdout
        .take()?
        .take(128)
        .read_to_string(&mut output)
        .await
        .ok()?;
    if !child.wait().await.ok()?.success() {
        return None;
    }
    let output = output.trim();
    let version = output
        .strip_prefix("sai ")
        .or_else(|| output.strip_prefix("sai, version "))?;
    is_version(version).then(|| version.to_owned())
}

pub(super) fn is_version(version: &str) -> bool {
    let mut parts = version.split('.');
    parts.clone().count() == 3
        && parts.all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
}
