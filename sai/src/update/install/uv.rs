mod provenance;

use std::{
    env,
    ffi::OsStr,
    fs,
    io::{self, Read, Write},
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
};

use semver::Version;
use serde::{Deserialize, Serialize};
use tempfile::{NamedTempFile, TempDir};
use tokio::process::Command;
use toml_edit::{DocumentMut, InlineTable};

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
const RECEIPT: &str = "uv-receipt.toml";

fn recovery_bin(tools: &Path) -> PathBuf {
    tools.join(CACHE).join("restore-bin")
}

pub(in crate::update) struct Uv {
    prefix: PathBuf,
    launcher: PathBuf,
    web_base: String,
}

#[derive(Clone, Deserialize, Serialize)]
struct Wheel {
    version: String,
    digest: String,
}

#[derive(Deserialize, Serialize)]
struct Pending {
    #[serde(flatten)]
    wheel: Wheel,
    launcher: PathBuf,
    python: PathBuf,
    #[serde(default)]
    cwd: Option<PathBuf>,
    #[serde(default)]
    next: Option<Wheel>,
    #[serde(default)]
    original_record: Option<String>,
}

impl Uv {
    pub(super) fn discover(exe: &Path, home: &Path, web_base: &str) -> Option<Self> {
        // uvx may share the same native cache. The shim supplies its actual
        // sys.prefix, and only an installed tool has a receipt and launcher.
        let prefix = shim_prefix(exe, home)?;
        if prefix.file_name()? != TOOL {
            return None;
        }
        let tools = prefix.parent()?.canonicalize().ok()?;
        Self::in_tools(&tools, web_base)
    }

    pub(super) async fn discover_recovery(web_base: &str) -> Result<Self> {
        let uv = find_uv()?;
        let cwd = env::current_dir().map_err(io_error)?;
        let (_, tools) = uv_directory(&uv, &cwd, &[]).await?;
        tools
            .as_deref()
            .and_then(|tools| Self::in_tools(tools, web_base))
            .filter(|install| install.pending_path().is_file())
            .ok_or_else(|| {
                "No pending Sai installation was found in the configured uv tool directory.".into()
            })
    }

    fn in_tools(tools: &Path, web_base: &str) -> Option<Self> {
        let prefix = tools.join(TOOL);
        if prefix.symlink_metadata().is_ok() && prefix.canonicalize().ok()? != prefix {
            return None;
        }
        let launcher = registered_launcher(&prefix)
            .filter(|path| path != &recovery_bin(tools).join("sai"))
            .or_else(|| {
                let pending: Pending =
                    serde_json::from_slice(&fs::read(tools.join(RECORD)).ok()?).ok()?;
                pending_launcher(&pending)
            })?;
        Some(Self {
            prefix,
            launcher,
            web_base: web_base.into(),
        })
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

    fn bin_dir(&self) -> &Path {
        self.launcher.parent().expect("launcher has a parent")
    }

    pub(super) fn lock_path(&self) -> PathBuf {
        self.tools().join(LOCK)
    }

    fn pending_path(&self) -> PathBuf {
        self.tools().join(RECORD)
    }

    pub(super) fn require_pending(&self) -> Result<()> {
        self.pending()?
            .map(|_| ())
            .ok_or_else(|| "No pending Sai installation needs recovery.".into())
    }

    fn pending(&self) -> Result<Option<Pending>> {
        let bytes = match fs::read(self.pending_path()) {
            Ok(bytes) => bytes,
            Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(err) => return Err(io_error(err)),
        };
        let record: Pending = serde_json::from_slice(&bytes)
            .map_err(|_| "could not read the pending uv install record".to_owned())?;
        if pending_launcher(&record).as_deref() != Some(self.launcher())
            || !record.python.is_absolute()
            || record.python.canonicalize().ok().as_deref() != Some(record.python.as_path())
            || !record.python.is_file()
            || record.python.starts_with(&self.prefix)
            || record.cwd.as_ref().is_some_and(|path| !path.is_absolute())
        {
            return Err(
                "the pending uv install record names a different launcher or invalid interpreter"
                    .into(),
            );
        }
        Ok(Some(record))
    }

    pub(super) fn kind(&self) -> &'static str {
        provenance::kind(&self.prefix, &self.web_base)
    }

    pub(super) fn authorized(&self, recover: bool) -> Result<bool> {
        if recover && let Some(record) = self.pending()? {
            self.check_recoverable_package(&record)?;
            return Ok(true);
        }
        if self.kind() != "uv-tool-release" {
            return Err("This uv tool was not installed from an official Sai release. Use its installer to update.".into());
        }
        Ok(false)
    }

    fn check_recoverable_package(&self, record: &Pending) -> Result<()> {
        let registered = registered_launcher(&self.prefix);
        let staged = recovery_bin(self.tools()).join("sai");
        // Private staging retires the public receipt before uv replaces the
        // environment. Until a public receipt is back, the validated pending
        // record identifies the install. uv can leave any subset of its files
        // behind. restore verifies the launcher and rollback wheel before writing.
        if self
            .prefix
            .join(RECEIPT)
            .symlink_metadata()
            .is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
            || registered.as_ref() == Some(&staged)
        {
            return Ok(());
        }
        let current = provenance::installed_version(&self.prefix);
        let original = current
            .as_ref()
            .is_some_and(|version| version.to_string() == record.wheel.version)
            && record.original_record.as_ref().is_none_or(|digest| {
                provenance::installed_digest(&self.prefix).as_ref() == Some(digest)
            })
            && provenance::is_release(&self.prefix, &self.web_base);
        let expected = || std::iter::once(&record.wheel).chain(record.next.as_ref());
        let file = provenance::file_origin(&self.prefix);
        let matches_wheel_origin = |wheel: &Wheel| {
            file.as_ref().is_some_and(|origin| {
                self.wheel_path(wheel).is_ok_and(|path| path == *origin)
                    && self.verify_wheel(wheel).is_ok()
            })
        };
        let matches_version = |wheel: &Wheel| {
            current
                .as_ref()
                .is_some_and(|version| version.to_string() == wheel.version)
        };
        // The Python bridge replaces the package in place and leaves uv's
        // receipt and public symlink intact even if uninstall was interrupted.
        let interrupted_in_place = record
            .original_record
            .as_deref()
            .is_none_or(release::valid_digest)
            && registered.as_deref() == Some(self.launcher())
            && self.python().is_ok_and(|python| python == record.python)
            && provenance::dist_info(&self.prefix).is_none_or(|info| {
                // uv removes files before removing their dist-info directory.
                // A directory for either recorded wheel may outlive METADATA.
                let remaining_record = provenance::installed_digest(&self.prefix);
                let owned = remaining_record
                    .as_ref()
                    .is_some_and(|digest| record.original_record.as_ref() == Some(digest))
                    || expected().any(matches_wheel_origin)
                    || (remaining_record.is_none()
                        && (!provenance::has_origin(&self.prefix)
                            || provenance::is_release(&self.prefix, &self.web_base)
                            || provenance::matches_original_origin(&self.prefix, record)));
                info.symlink_metadata().is_ok_and(|entry| entry.is_dir())
                    && info
                        .join("METADATA")
                        .symlink_metadata()
                        .is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
                    && expected().any(|wheel| {
                        self.wheel_path(wheel).ok().is_some_and(|path| {
                            let Some(stem) = path
                                .file_name()
                                .and_then(OsStr::to_str)
                                .and_then(|name| name.strip_suffix("-py3-none-any.whl"))
                            else {
                                return false;
                            };
                            info.ends_with(format!("{stem}.dist-info"))
                        })
                    })
                    && owned
            })
            && self
                .prefix
                .join("bin/sai")
                .symlink_metadata()
                .is_err_and(|error| error.kind() == io::ErrorKind::NotFound);
        if original || interrupted_in_place {
            return Ok(());
        }
        if expected().any(|wheel| matches_version(wheel) && matches_wheel_origin(wheel)) {
            return Ok(());
        }
        Err("A different package now occupies the pending uv installation.".into())
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

    fn launcher_identity(&self) -> Result<(u64, u64)> {
        let before = self.launcher.symlink_metadata().map_err(io_error)?;
        self.owner(false)?;
        let after = self.launcher.symlink_metadata().map_err(io_error)?;
        if (before.dev(), before.ino()) != (after.dev(), after.ino()) {
            return Err("the public uv launcher changed during the update".into());
        }
        Ok((after.dev(), after.ino()))
    }

    fn check_launcher(&self, expected: Option<(u64, u64)>, allow_missing: bool) -> Result<()> {
        let Some(expected) = expected else {
            return self.owner(allow_missing);
        };
        if self.launcher_identity()? != expected {
            return Err("the public uv launcher changed during the update".into());
        }
        Ok(())
    }

    async fn check_destination(&self, uv: &Path, cwd: &Path) -> Result<()> {
        for (extra, expected) in [(&[][..], self.tools()), (&["--bin"][..], self.bin_dir())] {
            let (output, found) = uv_directory(uv, cwd, extra).await?;
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
        let python = self
            .prefix
            .join("bin/python")
            .canonicalize()
            .map_err(io_error)?;
        if !python.is_file() || python.starts_with(&self.prefix) {
            return Err("uv's base Python interpreter must be outside the tool environment".into());
        }
        Ok(python)
    }

    pub(super) fn version(&self) -> Version {
        provenance::installed_version(&self.prefix).unwrap_or_else(|| {
            Version::parse(super::super::RUNNING).expect("Cargo version is semantic")
        })
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

    async fn stage(
        releases: &Releases,
        version: &Version,
        directory: &Path,
    ) -> Result<(Wheel, PathBuf)> {
        let name = wheel_name(version)?;
        let (bytes, digest) = releases
            .verified(version, &name)
            .await
            .map_err(|err| err.0)?;
        let path = directory.canonicalize().map_err(io_error)?.join(name);
        fs::write(&path, bytes).map_err(io_error)?;
        Ok((
            Wheel {
                version: version.to_string(),
                digest,
            },
            path,
        ))
    }

    fn publish_wheel(&self, wheel: &Wheel, staged: &Path) -> Result<PathBuf> {
        let path = self.wheel_path(wheel)?;
        let parent = path.parent().expect("wheel path has a parent");
        state::private_dir(parent).map_err(io_error)?;
        if parent.canonicalize().ok().as_deref() != Some(parent) {
            return Err("the verified wheel directory has changed".into());
        }
        if path
            .symlink_metadata()
            .is_err_and(|err| err.kind() == io::ErrorKind::NotFound)
        {
            verify_file(wheel, staged)?;
            let mut file = NamedTempFile::new_in(parent).map_err(io_error)?;
            io::copy(&mut fs::File::open(staged).map_err(io_error)?, &mut file)
                .map_err(io_error)?;
            file.as_file().sync_all().map_err(io_error)?;
            file.persist_noclobber(&path)
                .map_err(|err| io_error(err.error))?;
            state::sync_dir(parent).map_err(io_error)?;
        }
        self.verify_wheel(wheel)
    }

    fn verify_wheel(&self, wheel: &Wheel) -> Result<PathBuf> {
        let path = self.wheel_path(wheel)?;
        verify_file(wheel, &path)?;
        Ok(path)
    }

    async fn install(
        uv: &Path,
        python: &Path,
        wheel: &Wheel,
        path: &Path,
        home: &Path,
        (tools, bin): (&Path, &Path),
    ) -> Result<()> {
        verify_file(wheel, path)?;
        let mut command = Command::new(uv);
        command
            .current_dir(home)
            .args(["tool", "install", "--force", "--offline", "--python"])
            .arg(python)
            .arg(path)
            .env("UV_TOOL_DIR", tools)
            .env("UV_TOOL_BIN_DIR", bin);
        smoke::uv_output(command, "`uv tool install`").await?;
        Ok(())
    }

    async fn restore(
        &self,
        uv: &Path,
        python: &Path,
        pending: &Pending,
        home: &Path,
        shim_base: Option<&str>,
        launcher: Option<(u64, u64)>,
    ) -> Result<Version> {
        let wheel = &pending.wheel;
        let version = release::version(&wheel.version)
            .ok_or_else(|| "the rollback wheel record has an invalid version".to_owned())?;
        self.check_launcher(launcher, true)?;
        self.check_recoverable_package(pending)?;
        let path = self.verify_wheel(wheel)?;
        let receipt = registered_launcher(&self.prefix);
        let staged = if receipt.as_deref() == Some(self.launcher())
            && self.python().is_ok_and(|current| current == python)
        {
            let mut command = Command::new(uv);
            command
                .current_dir(home)
                .args([
                    "pip",
                    "install",
                    "--reinstall-package",
                    TOOL,
                    "--offline",
                    "--python",
                ])
                .arg(self.prefix.join("bin/python"))
                .arg(&path);
            smoke::uv_output(command, "`uv pip install`").await?;
            None
        } else {
            Some(self.stage_tool(uv, python, wheel, &path, home).await?)
        };
        self.check_launcher(launcher, true)?;
        if launcher.is_none()
            && self
                .launcher
                .symlink_metadata()
                .is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
        {
            symlink(self.prefix.join("bin/sai"), &self.launcher).map_err(io_error)?;
            state::sync_dir(self.bin_dir()).map_err(io_error)?;
        }
        self.check_launcher(launcher, false)?;
        if smoke::version(&self.prefix.join("bin/sai"), home, shim_base).await? != version {
            return Err(format!("the restored launcher did not report {version}"));
        }
        self.check_launcher(launcher, false)?;
        if let Some(staged) = staged {
            self.bind_staged_receipt(&staged, launcher)?;
        } else {
            self.pin_rollback_receipt(&path)?;
        }
        provenance::record(&self.prefix, wheel, &path)?;
        remove_record(&self.pending_path())?;
        Ok(version)
    }

    async fn stage_tool(
        &self,
        uv: &Path,
        python: &Path,
        wheel: &Wheel,
        path: &Path,
        home: &Path,
    ) -> Result<PathBuf> {
        let bin = recovery_bin(self.tools());
        let staged = bin.join("sai");
        state::private_dir(&bin).map_err(io_error)?;
        let info = bin.symlink_metadata().map_err(io_error)?;
        if bin.canonicalize().map_err(io_error)? != bin
            || !info.is_dir()
            || info.uid() != rustix::process::geteuid().as_raw()
            || info.mode() & 0o077 != 0
        {
            return Err("the uv staging bin must be a private directory".into());
        }
        let receipt = self.prefix.join(RECEIPT);
        let registered = registered_launcher(&self.prefix);
        // uv's forced installation may replace an entry from its prior
        // receipt. Keep it off the public path even when rebuilding the tool.
        if registered.as_deref() == Some(self.launcher()) || registered.as_ref() == Some(&staged) {
            // Retire the owned receipt before invoking uv so it cannot act
            // on entry points from the previous install attempt.
            fs::remove_file(&receipt).map_err(io_error)?;
            state::sync_dir(&self.prefix).map_err(io_error)?;
        } else if !receipt
            .symlink_metadata()
            .is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
        {
            return Err("could not safely recreate the uv tool with an existing receipt".into());
        }
        Self::install(uv, python, wheel, path, home, (self.tools(), &bin)).await?;
        Ok(staged)
    }

    fn bind_staged_receipt(&self, staged: &Path, launcher: Option<(u64, u64)>) -> Result<()> {
        let original = self.read_receipt()?;
        let mut document = original.clone();
        let entry = document
            .get_mut("tool")
            .and_then(|tool| tool.get_mut("entrypoints"))
            .and_then(toml_edit::Item::as_array_mut)
            .and_then(|entries| {
                entries
                    .iter_mut()
                    .filter_map(toml_edit::Value::as_inline_table_mut)
                    .find(|entry| receipt_entry(entry) == Some(staged))
            })
            .ok_or_else(|| "uv did not register the staged Sai launcher".to_owned())?;
        let public = self
            .launcher
            .to_str()
            .ok_or_else(|| "the uv launcher path is not valid UTF-8".to_owned())?;
        entry.insert("install-path", public.into());
        self.check_launcher(launcher, false)?;
        self.write_receipt(&document)?;
        if let Err(reason) = self.check_launcher(launcher, false) {
            self.write_receipt(&original)?;
            return Err(reason);
        }
        Ok(())
    }

    fn pin_rollback_receipt(&self, wheel: &Path) -> Result<()> {
        let path = wheel
            .to_str()
            .ok_or_else(|| "the rollback wheel path is not valid UTF-8".to_owned())?;
        let mut document = self.read_receipt()?;
        let requirement = document
            .get_mut("tool")
            .and_then(|tool| tool.get_mut("requirements"))
            .and_then(toml_edit::Item::as_array_mut)
            .and_then(|requirements| {
                requirements
                    .iter_mut()
                    .filter_map(toml_edit::Value::as_inline_table_mut)
                    .find(|entry| {
                        entry.get("name").and_then(toml_edit::Value::as_str) == Some(TOOL)
                    })
            })
            .ok_or_else(|| "uv's receipt has no Sai requirement".to_owned())?;
        // uv pip leaves uv tool's requirements alone. Pin the restored wheel so a
        // subsequent uv tool upgrade cannot reinstall a failed candidate.
        let mut rollback = InlineTable::new();
        rollback.insert("name", TOOL.into());
        rollback.insert("path", path.into());
        *requirement = rollback;
        self.write_receipt(&document)
    }

    fn read_receipt(&self) -> Result<DocumentMut> {
        fs::read_to_string(self.prefix.join(RECEIPT))
            .map_err(io_error)?
            .parse()
            .map_err(|_| "could not read the uv receipt".to_owned())
    }

    fn write_receipt(&self, document: &DocumentMut) -> Result<()> {
        let receipt = self.prefix.join(RECEIPT);
        let mut file = NamedTempFile::new_in(&self.prefix).map_err(io_error)?;
        file.write_all(document.to_string().as_bytes())
            .map_err(io_error)?;
        file.as_file().sync_all().map_err(io_error)?;
        file.persist(&receipt)
            .map_err(|error| io_error(error.error))?;
        state::sync_dir(&self.prefix).map_err(io_error)
    }

    pub(super) async fn recover(&self, home: &Path, allowed: impl Fn() -> bool) -> Result<Version> {
        if let Some(pending) = self.pending()? {
            let cwd = env::current_dir().map_err(io_error)?;
            let original = pending
                .cwd
                .as_deref()
                .filter(|path| path.is_dir())
                .unwrap_or(&cwd);
            let uv = find_uv()?;
            self.check_destination(&uv, original).await?;
            if !allowed() {
                return Ok(self.version());
            }
            self.restore(&uv, &pending.python, &pending, home, None, None)
                .await
        } else {
            smoke::version(&self.launcher, home, None).await
        }
    }

    pub(super) async fn change(
        &self,
        releases: &Releases,
        installed: &Version,
        target: &Version,
        profile: &Path,
        allowed: impl Fn() -> bool,
    ) -> Change {
        let result = async {
            let permissions = fs::Permissions::from_mode(0o700);
            let home = tempfile::Builder::new()
                .permissions(permissions.clone())
                .tempdir()
                .map_err(io_error)?;
            fs::set_permissions(home.path(), permissions).map_err(io_error)?;
            let trial = TempDir::new_in(home.path()).map_err(io_error)?;
            let cwd = env::current_dir().map_err(io_error)?;
            let uv = find_uv()?;
            if let Err(reason) = self.check_destination(&uv, &cwd).await {
                return Ok(Change::Unsupported(reason));
            }
            let launcher = Some(self.launcher_identity()?);
            let python = self.python()?;
            let trial_tools = trial.path().join("tools");
            let trial_bin = trial.path().join("bin");
            let trial_dirs = (trial_tools.as_path(), trial_bin.as_path());
            let shim_base = releases.shim_fixture_base();
            let (next, staged_next) = Self::stage(releases, target, home.path()).await?;
            let check = async {
                Self::install(&uv, &python, &next, &staged_next, home.path(), trial_dirs).await?;
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
            let (previous, staged_previous) =
                Self::stage(releases, installed, home.path())
                    .await
                    .map_err(|err| format!("could not prepare the rollback wheel: {err}"))?;
            let prepare = async {
                Self::install(
                    &uv,
                    &python,
                    &previous,
                    &staged_previous,
                    home.path(),
                    trial_dirs,
                )
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
            self.check_launcher(launcher, false)?;
            if let Err(reason) = self.authorized(false) {
                return Ok(Change::Unsupported(reason));
            }
            let published_next = self.publish_wheel(&next, &staged_next)?;
            self.publish_wheel(&previous, &staged_previous)?;
            let record = self.pending_path();
            let pending = Pending {
                wheel: previous,
                launcher: self.launcher.clone(),
                python: python.clone(),
                cwd: Some(cwd),
                next: Some(next.clone()),
                original_record: provenance::installed_digest(&self.prefix),
            };
            state::atomic_json(&record, &pending).map_err(io_error)?;
            let changed = async {
                self.check_launcher(launcher, false)?;
                let staged = self
                    .stage_tool(&uv, &python, &next, &published_next, home.path())
                    .await?;
                self.check_launcher(launcher, false)?;
                let executable = self.prefix.join("bin/sai");
                smoke::verify(&executable, home.path(), target, shim_base).await?;
                // Let the shim verify and cache its binary in the invoking profile.
                if smoke::version(&executable, profile, shim_base).await? != *target {
                    return Err(format!(
                        "the updated launcher did not report {target} in this profile"
                    ));
                }
                provenance::record(&self.prefix, &next, &published_next)?;
                self.bind_staged_receipt(&staged, launcher)?;
                remove_record(&record)
            }
            .await;
            match changed {
                Ok(()) => Ok(Change::Updated),
                Err(reason) => {
                    let (reason, rolled_back) = match self
                        .restore(&uv, &python, &pending, home.path(), shim_base, launcher)
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

fn verify_file(wheel: &Wheel, path: &Path) -> Result<()> {
    if path.canonicalize().ok().as_deref() != Some(path) {
        return Err("the verified wheel path is missing or has changed".into());
    }
    let mut bytes = Vec::new();
    fs::File::open(path)
        .map_err(io_error)?
        .take(release::MAX_ASSET as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(io_error)?;
    if bytes.len() > release::MAX_ASSET || release::sha256(&bytes) != wheel.digest {
        return Err("the verified wheel no longer matches its digest".into());
    }
    Ok(())
}

fn pending_launcher(record: &Pending) -> Option<PathBuf> {
    (record.launcher.is_absolute() && record.launcher.file_name()? == "sai")
        .then(|| launcher_path(&record.launcher))
        .flatten()
}

fn registered_launcher(prefix: &Path) -> Option<PathBuf> {
    let receipt = fs::read_to_string(prefix.join(RECEIPT)).ok()?;
    let receipt: DocumentMut = receipt.parse().ok()?;
    let tool = receipt.get("tool")?;
    for entry in tool.get("entrypoints")?.as_array()?.iter() {
        let Some(entry) = entry.as_inline_table() else {
            continue;
        };
        if let Some(path) = receipt_entry(entry) {
            return launcher_path(path);
        }
    }
    None
}

fn receipt_entry(entry: &InlineTable) -> Option<&Path> {
    if entry.get("name")?.as_str()? != "sai" {
        return None;
    }
    let path = Path::new(entry.get("install-path")?.as_str()?);
    (path.is_absolute() && path.file_name()? == "sai").then_some(path)
}

async fn uv_directory(uv: &Path, cwd: &Path, extra: &[&str]) -> Result<(String, Option<PathBuf>)> {
    let mut command = Command::new(uv);
    command.current_dir(cwd).args(["tool", "dir"]).args(extra);
    let output = smoke::uv_output(command, "`uv tool dir`").await?;
    let found = cwd.join(output.trim()).canonicalize().ok();
    Ok((output, found))
}

fn find_uv() -> Result<PathBuf> {
    smoke::find_executable(OsStr::new("uv"))
        .ok_or_else(|| "uv is not on PATH, so sai cannot update".to_owned())
}

fn wheel_name(version: &Version) -> Result<String> {
    release::wheel_name(version)
        .ok_or_else(|| "the shim supports stable versions and release candidates".into())
}

fn shim_prefix(exe: &Path, home: &Path) -> Option<PathBuf> {
    let prefix = PathBuf::from(env::var_os(SHIM_PREFIX)?);
    (prefix.is_absolute() && exe.starts_with(home.join("bin").canonicalize().ok()?))
        .then_some(prefix)
}

pub(super) fn unmanaged(
    exe: &Path,
    home: &Path,
    web_base: &str,
) -> Option<(PathBuf, &'static str)> {
    let prefix = shim_prefix(exe, home)?;
    let kind = provenance::kind(&prefix, web_base);
    Some((prefix, kind))
}
