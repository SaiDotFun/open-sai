use std::{
    env,
    ffi::OsStr,
    fs,
    io::{self, Read, Write},
    path::{Path, PathBuf},
};

use semver::Version;
use serde::{Deserialize, Serialize};
use tempfile::{NamedTempFile, TempDir};
use tokio::process::Command;
use toml_edit::DocumentMut;

use crate::update::{
    release::{self, Releases},
    smoke, state,
};

use super::{Change, Result, io as io_error, launcher_path, remove_record};

const SHIM_PREFIX: &str = "SAI_NATIVE_UV_PREFIX";
const TOOL: &str = "sai-cli";
const LOCK: &str = ".sai-cli-update.lock";
const CACHE: &str = ".sai-cli-native-wheels";
const RECORD: &str = ".sai-cli-native-pending.json";

pub(in crate::update) struct Uv {
    prefix: PathBuf,
    launcher: PathBuf,
}

#[derive(Deserialize, Serialize)]
struct Wheel {
    version: String,
    digest: String,
}

impl Uv {
    pub(super) fn discover(exe: &Path, home: &Path) -> Option<Self> {
        // uvx may share the same native cache. The shim supplies its actual
        // sys.prefix, and only an installed tool has a receipt and launcher.
        let prefix = PathBuf::from(env::var_os(SHIM_PREFIX)?);
        if !prefix.is_absolute() || !exe.starts_with(home.join("bin").canonicalize().ok()?) {
            return None;
        }
        let prefix = prefix.canonicalize().ok()?;
        if prefix.file_name()? != TOOL {
            return None;
        }
        let launcher = registered_launcher(&prefix)?;
        Some(Self { prefix, launcher })
    }

    pub(super) fn prefix(&self) -> &Path {
        &self.prefix
    }

    fn tools(&self) -> &Path {
        self.prefix.parent().expect("uv tool prefix has a parent")
    }

    pub(super) fn launcher(&self) -> &Path {
        &self.launcher
    }

    pub(super) fn lock_path(&self) -> PathBuf {
        self.tools().join(LOCK)
    }

    pub(super) fn owner(&self, allow_missing: bool) -> Result<()> {
        let link = match fs::read_link(&self.launcher) {
            Ok(link) => link,
            Err(err) if allow_missing && err.kind() == io::ErrorKind::NotFound => {
                return Ok(());
            }
            Err(_) => {
                return Err(format!(
                    "{} is missing or no longer a uv tool launcher",
                    self.launcher.display()
                ));
            }
        };
        let target = self
            .launcher
            .parent()
            .expect("launcher has a parent")
            .join(link);
        let inside = self.prefix.join("bin/sai");
        if target == inside
            || (target.canonicalize().is_ok()
                && target.canonicalize().ok() == inside.canonicalize().ok())
        {
            Ok(())
        } else {
            Err(format!(
                "{} runs a different install than {}",
                self.launcher.display(),
                self.prefix.display()
            ))
        }
    }

    async fn check_destination(&self, uv: &Path, home: &Path) -> Result<()> {
        for (extra, expected) in [
            (&[][..], self.tools()),
            (
                &["--bin"][..],
                self.launcher.parent().expect("launcher has a parent"),
            ),
        ] {
            let mut command = Command::new(uv);
            command.current_dir(home).args(["tool", "dir"]).args(extra);
            let output = smoke::uv_output(command, "`uv tool dir`").await?;
            let found = Path::new(output.trim()).canonicalize().ok();
            if found.as_deref() != Some(expected) {
                return Err(format!(
                    "uv selects {}, expected {}.",
                    output.trim(),
                    expected.display()
                ));
            }
        }
        Ok(())
    }

    fn python(&self) -> Result<PathBuf> {
        self.prefix
            .join("bin/python")
            .canonicalize()
            .map_err(io_error)
    }

    fn wheel_path(&self, wheel: &Wheel) -> Result<PathBuf> {
        let version = release::version(&wheel.version)
            .ok_or_else(|| "the wheel record has an invalid version".to_owned())?;
        if !release::valid_digest(&wheel.digest) {
            return Err("the wheel record has an invalid digest".into());
        }
        Ok(self
            .tools()
            .join(CACHE)
            .join(&wheel.digest)
            .join(wheel_name(&version)?))
    }

    async fn stage(&self, releases: &Releases, version: &Version) -> Result<Wheel> {
        let (bytes, digest) = releases
            .verified(version, &wheel_name(version)?)
            .await
            .map_err(|err| err.0)?;
        let wheel = Wheel {
            version: version.to_string(),
            digest,
        };
        let path = self.wheel_path(&wheel)?;
        let parent = path.parent().expect("wheel path has a parent");
        state::private_dir(parent).map_err(io_error)?;
        if parent.canonicalize().ok().as_deref() != Some(parent) {
            return Err("the verified wheel directory has changed".into());
        }
        if path.canonicalize().is_ok() {
            self.verify_wheel(&wheel)?;
        } else {
            let mut file = NamedTempFile::new_in(parent).map_err(io_error)?;
            file.write_all(&bytes).map_err(io_error)?;
            file.as_file().sync_all().map_err(io_error)?;
            file.persist_noclobber(&path)
                .map_err(|err| io_error(err.error))?;
            state::sync_dir(parent).map_err(io_error)?;
        }
        Ok(wheel)
    }

    fn verify_wheel(&self, wheel: &Wheel) -> Result<PathBuf> {
        let path = self.wheel_path(wheel)?;
        if path.canonicalize().ok().as_deref() != Some(path.as_path()) {
            return Err("the verified wheel path is missing or has changed".into());
        }
        let mut bytes = Vec::new();
        fs::File::open(&path)
            .map_err(io_error)?
            .take(release::MAX_ASSET as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(io_error)?;
        if bytes.len() > release::MAX_ASSET || release::sha256(&bytes) != wheel.digest {
            return Err("the verified wheel no longer matches its digest".into());
        }
        Ok(path)
    }

    async fn install(
        &self,
        uv: &Path,
        python: &Path,
        wheel: &Wheel,
        home: &Path,
        trial: Option<&Path>,
    ) -> Result<()> {
        let path = self.verify_wheel(wheel)?;
        let mut command = Command::new(uv);
        command
            .current_dir(home)
            .args(["tool", "install", "--force", "--offline", "--python"])
            .arg(python)
            .arg(path);
        if let Some(trial) = trial {
            command
                .env("UV_TOOL_DIR", trial.join("tools"))
                .env("UV_TOOL_BIN_DIR", trial.join("bin"));
        }
        smoke::uv_output(command, "`uv tool install`").await?;
        Ok(())
    }

    async fn restore(
        &self,
        uv: &Path,
        python: &Path,
        wheel: &Wheel,
        home: &Path,
        shim_base: Option<&str>,
    ) -> Result<Version> {
        let version = release::version(&wheel.version)
            .ok_or_else(|| "the rollback wheel record has an invalid version".to_owned())?;
        self.owner(true)?;
        self.install(uv, python, wheel, home, None).await?;
        self.owner(false)?;
        if smoke::version(&self.launcher, home, shim_base).await? != version {
            return Err(format!("the restored launcher did not report {version}"));
        }
        remove_record(&self.tools().join(RECORD))?;
        Ok(version)
    }

    pub(super) async fn recover(&self, home: &Path) -> Result<Version> {
        match fs::read(self.tools().join(RECORD)) {
            Ok(bytes) => {
                let wheel = serde_json::from_slice(&bytes)
                    .map_err(|_| "could not read the pending uv install record".to_owned())?;
                let scratch = TempDir::new().map_err(io_error)?;
                let uv = find_uv()?;
                self.check_destination(&uv, scratch.path()).await?;
                let python = self.python()?;
                self.restore(&uv, &python, &wheel, home, None).await
            }
            Err(err) if err.kind() == io::ErrorKind::NotFound => {
                smoke::version(&self.launcher, home, None).await
            }
            Err(err) => Err(io_error(err)),
        }
    }

    pub(super) async fn change(
        &self,
        releases: &Releases,
        installed: &Version,
        target: &Version,
        allowed: impl Fn() -> bool,
    ) -> Change {
        let result = async {
            let home = TempDir::new().map_err(io_error)?;
            let trial = TempDir::new().map_err(io_error)?;
            let uv = find_uv()?;
            if let Err(reason) = self.check_destination(&uv, home.path()).await {
                return Ok(Change::Unsupported(reason));
            }
            let python = self.python()?;
            let shim_base = releases.shim_fixture_base();
            let next = self.stage(releases, target).await?;
            let check = async {
                self.install(&uv, &python, &next, home.path(), Some(trial.path()))
                    .await?;
                smoke::verify(
                    &trial.path().join("bin/sai"),
                    home.path(),
                    target,
                    shim_base,
                )
                .await
            }
            .await;
            check.map_err(|err| format!("the release failed its trial install: {err}"))?;
            let previous = self
                .stage(releases, installed)
                .await
                .map_err(|err| format!("could not prepare the rollback wheel: {err}"))?;
            let prepare = async {
                self.install(&uv, &python, &previous, home.path(), Some(trial.path()))
                    .await?;
                smoke::verify(
                    &trial.path().join("bin/sai"),
                    home.path(),
                    installed,
                    shim_base,
                )
                .await
            }
            .await;
            prepare.map_err(|err| format!("the rollback wheel failed its trial install: {err}"))?;
            if !allowed() {
                return Ok(Change::Available);
            }
            self.owner(false)?;
            let record = self.tools().join(RECORD);
            state::atomic_json(&record, &previous).map_err(io_error)?;
            let changed = async {
                self.install(&uv, &python, &next, home.path(), None).await?;
                self.owner(false)?;
                smoke::verify(&self.launcher, home.path(), target, shim_base).await?;
                remove_record(&record)
            }
            .await;
            match changed {
                Ok(()) => Ok(Change::Updated),
                Err(reason) => {
                    if self.owner(false).is_ok()
                        && smoke::verify(&self.launcher, home.path(), installed, shim_base)
                            .await
                            .is_ok()
                    {
                        let _ = remove_record(&record);
                        return Ok(Change::unchanged(reason, installed));
                    }
                    let (reason, rolled_back) = match self
                        .restore(&uv, &python, &previous, home.path(), shim_base)
                        .await
                    {
                        Ok(_) => (format!("{reason}. Rolled back to sai {installed}."), true),
                        Err(err) => (
                            format!(
                                "{reason}. Rolling back to sai {installed} also failed: {err}."
                            ),
                            false,
                        ),
                    };
                    Ok(Change::Failed {
                        reason,
                        rolled_back,
                    })
                }
            }
        }
        .await;
        result.unwrap_or_else(|reason: String| Change::unchanged(reason, installed))
    }
}

fn registered_launcher(prefix: &Path) -> Option<PathBuf> {
    let receipt = fs::read_to_string(prefix.join("uv-receipt.toml")).ok()?;
    let receipt: DocumentMut = receipt.parse().ok()?;
    let tool = receipt.get("tool")?;
    for entry in tool.get("entrypoints")?.as_array()?.iter() {
        let Some(entry) = entry.as_inline_table() else {
            continue;
        };
        if entry.get("name").and_then(toml_edit::Value::as_str) != Some("sai") {
            continue;
        }
        let path = Path::new(entry.get("install-path")?.as_str()?);
        if path.is_absolute() && path.file_name()? == "sai" {
            return launcher_path(path);
        }
    }
    None
}

fn find_uv() -> Result<PathBuf> {
    smoke::find_executable(OsStr::new("uv"))
        .ok_or_else(|| "uv is not on PATH, so sai cannot update".to_owned())
}

fn wheel_name(version: &Version) -> Result<String> {
    release::wheel_name(version)
        .ok_or_else(|| "the shim supports stable versions and release candidates".into())
}
