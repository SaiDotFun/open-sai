use std::{
    fs::{self, File},
    io::{self, Read, Write},
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
};

use flate2::read::MultiGzDecoder;
use semver::Version;
use serde::{Deserialize, Serialize};
use tempfile::TempDir;

use crate::update::{
    release::{self, Releases},
    smoke, state,
};

use super::{Change, Result, io as io_error, remove_record};

const ROOT: &str = ".sai-native";
const RECORD: &str = "pending.json";
const LAUNCHER_STAGE: &str = ".launcher-switch/sai";
const LOCK: &str = ".sai-update.lock";
const MAX_BINARY: usize = 96 << 20;
const MAX_TAR: usize = MAX_BINARY + (1 << 20);

pub(in crate::update) struct Standalone {
    root: PathBuf,
    launcher: PathBuf,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Pending {
    // A first install has no launcher to restore.
    #[serde(deserialize_with = "Option::deserialize")]
    old: Option<String>,
    new: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    launcher_switch: Option<LauncherSwitch>,
}

type LauncherEntry = (u64, u64, Option<PathBuf>);

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct LauncherSwitch {
    staged: String,
    expected: LauncherEntry,
    published: Option<LauncherEntry>,
}

fn launcher_entry(path: &Path) -> io::Result<Option<LauncherEntry>> {
    let info = match path.symlink_metadata() {
        Ok(info) => info,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(err),
    };
    let link = info.is_symlink().then(|| fs::read_link(path)).transpose()?;
    let checked = path.symlink_metadata()?;
    if (info.dev(), info.ino()) != (checked.dev(), checked.ino()) {
        return Err(io::Error::other(
            "the Sai launcher changed while reading its owner",
        ));
    }
    Ok(Some((info.dev(), info.ino(), link)))
}

fn rename_exclusive(source: &Path, destination: &Path) -> rustix::io::Result<()> {
    use rustix::fs::{CWD, RenameFlags, renameat_with};

    renameat_with(CWD, source, CWD, destination, RenameFlags::NOREPLACE)
}

fn unsupported_rename(error: rustix::io::Errno) -> bool {
    use rustix::io::Errno;

    matches!(error, Errno::NOSYS | Errno::NOTSUP | Errno::INVAL)
}

fn link_displaced(source: &Path, destination: &Path) -> rustix::io::Result<()> {
    use rustix::fs::{AtFlags, CWD, linkat};

    linkat(CWD, source, CWD, destination, AtFlags::empty())
}

fn publish_displaced(source: &Path, launcher: &Path) -> io::Result<()> {
    let active = launcher_entry(launcher)?;
    if active.is_some() && active == launcher_entry(source)? {
        return Ok(());
    }
    match rename_exclusive(source, launcher) {
        Ok(()) => Ok(()),
        Err(error) if unsupported_rename(error) => {
            // The caller clears the staged link once publication is durable.
            link_displaced(source, launcher).map_err(Into::into)
        }
        Err(error) => Err(error.into()),
    }
}

fn dir_version(name: &str) -> Option<Version> {
    let (version, digest) = name.rsplit_once('-')?;
    release::valid_digest(digest)
        .then(|| release::version(version))
        .flatten()
}

impl Standalone {
    pub(super) fn discover(exe: &Path) -> Option<Self> {
        if exe.file_name()? != "sai" {
            return None;
        }
        let version_dir = exe.parent()?;
        dir_version(version_dir.file_name()?.to_str()?)?;
        let root = version_dir.parent()?;
        if root.file_name()? != ROOT
            || !fs::metadata(root).ok()?.is_dir()
            || fs::metadata(root).ok()?.permissions().mode() & 0o022 != 0
        {
            return None;
        }
        let launcher = root.parent()?.join("sai");
        Some(Self {
            root: root.to_owned(),
            launcher,
        })
    }

    pub(super) fn root(&self) -> &Path {
        &self.root
    }

    pub(super) fn launcher(&self) -> &Path {
        &self.launcher
    }

    pub(super) fn lock_path(&self) -> PathBuf {
        self.root.with_file_name(LOCK)
    }

    fn target(&self, dir: &str) -> Option<PathBuf> {
        dir_version(dir)?;
        let binary = self.root.join(dir).join("sai");
        let canonical = binary.canonicalize().ok()?;
        (canonical == binary && fs::metadata(&canonical).ok()?.is_file()).then_some(canonical)
    }

    pub(super) fn active(&self) -> Result<String> {
        let outside = || {
            format!(
                "{} no longer points to a managed Sai binary.",
                self.launcher.display()
            )
        };
        let link = fs::read_link(&self.launcher).map_err(|_| outside())?;
        let target = self
            .launcher
            .parent()
            .expect("launcher has a parent")
            .join(link);
        let directory = target
            .parent()
            .and_then(Path::file_name)
            .and_then(|name| name.to_str())
            .ok_or_else(outside)?;
        if self.target(directory).as_deref() != Some(&target) {
            return Err(outside());
        }
        Ok(directory.to_owned())
    }

    fn switch(&self, from: &str, to: &str) -> Result<()> {
        let parent = self.launcher.parent().expect("launcher has a parent");
        if self.target(to).is_none() {
            return Err("the replacement binary is missing".into());
        }
        let stage = TempDir::new_in(parent).map_err(io_error)?;
        let link = stage.path().join("sai");
        symlink(Path::new(ROOT).join(to).join("sai"), &link).map_err(io_error)?;
        if self.active()? != from {
            return Err("the Sai launcher changed during the update".into());
        }
        fs::rename(link, &self.launcher).map_err(io_error)?;
        state::sync_dir(parent).map_err(io_error)
    }

    pub(super) fn version(&self) -> Result<Version> {
        self.active()
            .map(|active| dir_version(&active).expect("validated managed target"))
    }

    fn settle_launcher_switch(&self, switch: &LauncherSwitch) -> Result<bool> {
        if switch.staged != LAUNCHER_STAGE {
            return Err("the pending Sai launcher switch names an invalid staging path".into());
        }
        let displaced = self.root.join(LAUNCHER_STAGE);
        let stage = displaced.parent().expect("staged launcher has a parent");
        let info = match stage.symlink_metadata() {
            Ok(info) => info,
            Err(err) if err.kind() == io::ErrorKind::NotFound => {
                return Ok(false);
            }
            Err(err) => return Err(io_error(err)),
        };
        if !info.is_dir()
            || info.uid() != rustix::process::geteuid().as_raw()
            || info.mode() & 0o077 != 0
        {
            return Err("the pending Sai launcher staging directory is not private".into());
        }
        let entry = launcher_entry(&displaced).map_err(io_error)?;
        let external = entry.is_some()
            && entry.as_ref() != Some(&switch.expected)
            && entry != switch.published;
        if external {
            self.restore_displaced(switch, &displaced, stage)
                .map_err(|error| format!(
                    "The Sai launcher changed during the switch. The displaced launcher is at {}: {error}",
                    displaced.display()
                ))?;
        }
        state::sync_dir(self.launcher.parent().expect("launcher has a parent"))
            .map_err(io_error)?;
        fs::remove_dir_all(stage).map_err(io_error)?;
        state::sync_dir(&self.root).map_err(io_error)?;
        Ok(external)
    }

    fn restore_displaced(
        &self,
        switch: &LauncherSwitch,
        displaced: &Path,
        stage: &Path,
    ) -> io::Result<()> {
        let withdrawn = stage.join("public");
        let active = launcher_entry(&self.launcher)?;
        if active.is_some() && active == launcher_entry(displaced)? {
            return Ok(());
        }
        let Some(ours) = switch.published.as_ref() else {
            return publish_displaced(displaced, &self.launcher);
        };
        if launcher_entry(&withdrawn)?.is_none() && active.as_ref() == Some(ours) {
            match rename_exclusive(&self.launcher, &withdrawn) {
                Ok(()) => {}
                Err(error) if unsupported_rename(error) => {
                    // Confirm the filesystem can publish the competitor before
                    // making the public path vacant.
                    let probe = TempDir::new_in(stage)?;
                    link_displaced(displaced, &probe.path().join("sai"))?;
                    fs::rename(&self.launcher, &withdrawn)?;
                }
                Err(error) => return Err(error.into()),
            }
        }
        match launcher_entry(&withdrawn)?.as_ref() {
            Some(entry) if entry == ours => publish_displaced(displaced, &self.launcher),
            Some(_) => {
                publish_displaced(&withdrawn, &self.launcher)?;
                Err(io::Error::other("a newer launcher owns the public path"))
            }
            None => Err(io::Error::other("the public launcher is no longer ours")),
        }
    }

    fn pending(&self) -> Result<Option<Pending>> {
        let bytes = match fs::read(self.root.join(RECORD)) {
            Ok(bytes) => bytes,
            Err(err) if err.kind() == io::ErrorKind::NotFound => {
                return Ok(None);
            }
            Err(err) => return Err(io_error(err)),
        };
        serde_json::from_slice(&bytes)
            .map(Some)
            .map_err(|_| "could not read the pending Sai install record".to_owned())
    }

    pub(super) fn recover_staged_launcher(&self) -> Result<()> {
        if let Some(switch) = self.pending()?.and_then(|pending| pending.launcher_switch)
            && self.settle_launcher_switch(&switch)?
        {
            remove_record(&self.root.join(RECORD))?;
            return Err("the Sai launcher changed during the switch".into());
        }
        Ok(())
    }

    pub(super) fn recover(&self) -> Result<Version> {
        let Some(pending) = self.pending()? else {
            return self.version();
        };
        let record = self.root.join(RECORD);
        let active = self.active()?;
        let invalid =
            || "the pending Sai install record names a missing or invalid binary".to_owned();
        self.target(&pending.new).ok_or_else(invalid)?;
        let Some(old) = pending.old else {
            if active != pending.new || self.active()? != pending.new {
                return Err("the Sai launcher changed during an interrupted install".into());
            }
            fs::remove_file(&self.launcher).map_err(io_error)?;
            state::sync_dir(self.launcher.parent().expect("launcher has a parent"))
                .map_err(io_error)?;
            remove_record(&record)?;
            return Err(
                "Interrupted first Sai install was rolled back. Run the installer again.".into(),
            );
        };
        let previous = dir_version(&old)
            .filter(|_| self.target(&old).is_some())
            .ok_or_else(invalid)?;
        if active == pending.new {
            self.switch(&pending.new, &old)?;
        } else if active != old {
            return Err("the Sai launcher changed during an interrupted update".into());
        }
        remove_record(&record)?;
        Ok(previous)
    }

    async fn stage(
        &self,
        releases: &Releases,
        target: &Version,
        home: &Path,
    ) -> Result<(TempDir, String)> {
        let name = release::archive_name(target)
            .ok_or_else(|| "no native archive is available for this platform".to_owned())?;
        let (archive, digest) = releases
            .verified(target, &name)
            .await
            .map_err(|err| err.0)?;
        let stage = TempDir::new_in(&self.root).map_err(io_error)?;
        let binary = stage.path().join("sai");
        unpack(&archive, &binary)?;
        smoke::verify(&binary, home, target, None)
            .await
            .map_err(|err| format!("the release failed its trial run: {err}"))?;
        Ok((stage, format!("{target}-{digest}")))
    }

    fn publish(&self, stage: TempDir, directory: &str) -> Result<()> {
        let published = self.root.join(directory);
        if published.symlink_metadata().is_ok() {
            let existing = self
                .target(directory)
                .ok_or_else(|| "the existing version directory is invalid".to_owned())?;
            if file_hash(&existing)? != file_hash(&stage.path().join("sai"))? {
                return Err("the existing version directory contains different bytes".into());
            }
        } else {
            state::sync_dir(stage.path()).map_err(io_error)?;
            fs::rename(stage.path(), published).map_err(io_error)?;
            state::sync_dir(&self.root).map_err(io_error)?;
        }
        Ok(())
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
            let old = self.active()?;
            if dir_version(&old).as_ref() != Some(installed) {
                return Err("the Sai launcher changed during the update".into());
            }
            let (stage, new) = self.stage(releases, target, home.path()).await?;
            if !allowed() {
                return Ok(Change::Available);
            }
            self.publish(stage, &new)?;
            let record = self.root.join(RECORD);
            let pending = Pending {
                old: Some(old.clone()),
                new,
                launcher_switch: None,
            };
            state::atomic_json(&record, &pending).map_err(io_error)?;
            let switched = async {
                self.switch(&old, &pending.new)?;
                smoke::verify(&self.launcher, home.path(), target, None).await?;
                remove_record(&record)
            }
            .await;

            match switched {
                Ok(()) => Ok(Change::Updated),
                Err(reason) => {
                    let rollback = self.active().and_then(|active| {
                        if active == pending.new {
                            self.switch(&pending.new, &old)?;
                            let _ = remove_record(&record);
                            Ok(true)
                        } else if active == old {
                            let _ = remove_record(&record);
                            Ok(false)
                        } else {
                            Err("the launcher now points to a different install".into())
                        }
                    });
                    Ok(match rollback {
                        Ok(true) => Change::Failed {
                            reason: format!("{reason}. Rolled back to sai {installed}."),
                            rolled_back: true,
                        },
                        Ok(false) => Change::unchanged(reason, installed),
                        Err(error) => Change::Failed {
                            reason: format!(
                                "{reason}. Rolling back to sai {installed} also failed: {error}."
                            ),
                            rolled_back: false,
                        },
                    })
                }
            }
        }
        .await;
        result.unwrap_or_else(|reason: String| Change::unchanged(reason, installed))
    }
}

fn file_hash(path: &Path) -> Result<String> {
    let mut data = Vec::new();
    File::open(path)
        .map_err(io_error)?
        .take(MAX_BINARY as u64 + 1)
        .read_to_end(&mut data)
        .map_err(io_error)?;
    if data.len() > MAX_BINARY {
        return Err("the existing binary is too large".into());
    }
    Ok(release::sha256(&data))
}

fn unpack(compressed: &[u8], binary: &Path) -> Result<()> {
    let read_error = |error: io::Error| format!("could not read the native archive: {error}");
    let stream = MultiGzDecoder::new(compressed).take(MAX_TAR as u64);
    let mut archive = tar::Archive::new(stream);
    archive.set_ignore_zeros(true);
    let mut output = None;
    for entry in archive.entries().map_err(read_error)?.raw(true) {
        let mut entry = entry.map_err(read_error)?;
        if output.is_some()
            || !entry.header().entry_type().is_file()
            || entry.path_bytes().as_ref() != b"sai"
            || entry.size() == 0
            || entry.size() > MAX_BINARY as u64
        {
            return Err(
                "the native archive must contain exactly one regular file named sai".into(),
            );
        }
        let mut file = File::create_new(binary).map_err(io_error)?;
        let copied = io::copy(&mut entry, &mut file).map_err(read_error)?;
        if copied != entry.size() {
            return Err("the native archive has a truncated sai binary".into());
        }
        file.flush().map_err(io_error)?;
        output = Some(file);
    }
    if archive.into_inner().limit() == 0 {
        return Err("the expanded native archive is too large".into());
    }
    let file = output.ok_or_else(|| "the native archive does not contain sai".to_owned())?;
    file.set_permissions(fs::Permissions::from_mode(0o700))
        .map_err(io_error)?;
    file.sync_all().map_err(io_error)
}

#[cfg(test)]
mod tests;
