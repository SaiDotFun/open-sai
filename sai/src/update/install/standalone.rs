use std::{
    fs::{self, File},
    io::{self, Read, Write},
    os::unix::{
        ffi::OsStrExt,
        fs::{MetadataExt, PermissionsExt, symlink},
    },
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
const SELECTOR: &str = "current";
const RECORD: &str = "pending.json";
const LAUNCHER_STAGE: &str = ".launcher-switch/sai";
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
    launcher: Option<(u64, u64)>,
    #[serde(skip_serializing_if = "Option::is_none")]
    launcher_switch: Option<LauncherSwitch>,
}

type LauncherEntry = (u64, u64, Option<PathBuf>);
type CompetitorIdentity = (u64, u64, Option<String>);

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct LauncherSwitch {
    staged: String,
    expected: LauncherEntry,
    published: Option<LauncherEntry>,
    #[serde(skip_serializing_if = "Option::is_none")]
    competitor: Option<CompetitorIdentity>,
}

fn competitor_identity((device, inode, link): &LauncherEntry) -> CompetitorIdentity {
    let digest = link
        .as_ref()
        .map(|target| release::sha256(target.as_os_str().as_bytes()));
    (*device, *inode, digest)
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
        let info = root.symlink_metadata().ok()?;
        if root.file_name()? != ROOT
            || !info.is_dir()
            || info.uid() != rustix::process::geteuid().as_raw()
            || info.permissions().mode() & 0o022 != 0
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
        self.root.with_file_name(state::LAUNCHER_LOCK)
    }

    fn target(&self, dir: &str) -> Option<PathBuf> {
        dir_version(dir)?;
        let binary = self.root.join(dir).join("sai");
        let canonical = binary.canonicalize().ok()?;
        (canonical == binary && fs::metadata(&canonical).ok()?.is_file()).then_some(canonical)
    }

    fn launcher_identity(&self) -> Result<LauncherEntry> {
        let link = Path::new(ROOT).join(SELECTOR);
        launcher_entry(&self.launcher)
            .map_err(io_error)?
            .filter(|entry| entry.2.as_deref().map(Path::as_os_str) == Some(link.as_os_str()))
            .ok_or_else(|| {
                format!(
                    "{} no longer points to a managed Sai binary through {}. Update it through its installer.",
                    self.launcher.display(),
                    link.display(),
                )
            })
    }

    pub(super) fn owner(&self) -> Result<()> {
        self.launcher_identity().map(|_| ())
    }

    fn require_launcher(&self, expected: &LauncherEntry) -> Result<()> {
        if launcher_entry(&self.launcher).map_err(io_error)?.as_ref() != Some(expected) {
            return Err("the Sai launcher changed during the update".into());
        }
        Ok(())
    }

    fn selected_directory(&self) -> Result<Option<String>> {
        let selector = self.root.join(SELECTOR);
        let link = match fs::read_link(&selector) {
            Ok(link) => link,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(io_error(error)),
        };
        let invalid = || {
            format!(
                "{} does not select a managed Sai binary",
                selector.display()
            )
        };
        let directory = link
            .parent()
            .and_then(Path::file_name)
            .and_then(|name| name.to_str())
            .ok_or_else(invalid)?;
        if link.as_os_str() != Path::new(directory).join("sai").as_os_str()
            || self.target(directory).is_none()
        {
            return Err(invalid());
        }
        Ok(Some(directory.to_owned()))
    }

    fn switch_selector(
        &self,
        launcher: &LauncherEntry,
        from: &str,
        to: Option<&str>,
    ) -> Result<()> {
        let selector = self.root.join(SELECTOR);
        let stage = to
            .map(|to| -> Result<TempDir> {
                if self.target(to).is_none() {
                    return Err("the replacement binary is missing".into());
                }
                let stage = TempDir::new_in(&self.root).map_err(io_error)?;
                symlink(Path::new(to).join("sai"), stage.path().join(SELECTOR))
                    .map_err(io_error)?;
                Ok(stage)
            })
            .transpose()?;
        if self.selected_directory()?.as_deref() != Some(from) {
            return Err("the Sai selector changed during the update".into());
        }
        self.require_launcher(launcher)?;
        // The install lock protects the private selector. A competing installer
        // may still replace the public link, so leave that path alone.
        match stage {
            Some(stage) => fs::rename(stage.path().join(SELECTOR), &selector),
            None => fs::remove_file(&selector),
        }
        .map_err(io_error)?;
        state::sync_dir(&self.root).map_err(io_error)?;
        self.require_launcher(launcher)
    }

    pub(super) fn version(&self) -> Result<Version> {
        self.owner()?;
        self.selected_directory()?
            .map(|active| dir_version(&active).expect("validated managed target"))
            .ok_or_else(|| {
                "the Sai launcher has no selected binary; run the installer again".into()
            })
    }

    fn settle_launcher_switch(&self, pending: &mut Pending) -> Result<bool> {
        let Some(switch) = &pending.launcher_switch else {
            return Ok(false);
        };
        if switch.staged != LAUNCHER_STAGE {
            return Err("the pending Sai launcher switch names an invalid staging path".into());
        }
        let displaced = self.root.join(LAUNCHER_STAGE);
        let stage = displaced.parent().expect("staged launcher has a parent");
        let stage_exists = match stage.symlink_metadata() {
            Ok(info)
                if info.is_dir()
                    && info.uid() == rustix::process::geteuid().as_raw()
                    && info.mode() & 0o077 == 0 =>
            {
                true
            }
            Ok(_) => {
                return Err("the pending Sai launcher staging directory is not private".into());
            }
            Err(err) if err.kind() == io::ErrorKind::NotFound => false,
            Err(err) => return Err(io_error(err)),
        };
        let entry = if stage_exists {
            launcher_entry(&displaced).map_err(io_error)?
        } else {
            None
        };
        let restored = if let Some(entry) = entry
            .as_ref()
            .filter(|entry| *entry != &switch.expected && Some(*entry) != switch.published.as_ref())
        {
            self.restore_displaced(pending, &displaced, stage, entry)
                .map_err(|error| format!(
                    "The Sai launcher changed during the switch. The displaced launcher is at {}: {error}",
                    displaced.display()
                ))?;
            true
        } else if let Some(competitor) = &switch.competitor {
            if entry.is_some() {
                return Err("the staged launcher changed during recovery".into());
            }
            let active = launcher_entry(&self.launcher).map_err(io_error)?;
            if active.as_ref().map(competitor_identity).as_ref() != Some(competitor) {
                return Err("the public launcher no longer matches the recorded competitor".into());
            }
            true
        } else {
            false
        };
        if stage_exists || restored {
            state::sync_dir(self.launcher.parent().expect("launcher has a parent"))
                .map_err(io_error)?;
        }
        if stage_exists {
            fs::remove_dir_all(stage).map_err(io_error)?;
            state::sync_dir(&self.root).map_err(io_error)?;
        }
        Ok(restored)
    }

    fn restore_displaced(
        &self,
        pending: &mut Pending,
        displaced: &Path,
        stage: &Path,
        competitor: &LauncherEntry,
    ) -> io::Result<()> {
        let switch = pending
            .launcher_switch
            .as_ref()
            .expect("pending launcher switch");
        let identity = competitor_identity(competitor);
        if switch
            .competitor
            .as_ref()
            .is_some_and(|entry| entry != &identity)
        {
            return Err(io::Error::other(
                "the staged launcher changed during recovery",
            ));
        }
        let published = switch.published.clone();
        let withdrawn = stage.join("public");
        let active = launcher_entry(&self.launcher)?;
        let mut recorded_before_withdrawal = false;
        if active.as_ref() != Some(competitor)
            && let Some(ours) = published.as_ref()
        {
            if launcher_entry(&withdrawn)?.is_none() && active.as_ref() == Some(ours) {
                self.record_competitor(pending, &identity)?;
                recorded_before_withdrawal = true;
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
                Some(entry) if entry == ours => {}
                Some(_) => {
                    publish_displaced(&withdrawn, &self.launcher)?;
                    return Err(io::Error::other("a newer launcher owns the public path"));
                }
                None => return Err(io::Error::other("the public launcher is no longer ours")),
            }
        }
        if launcher_entry(&self.launcher)?
            .as_ref()
            .is_some_and(|entry| entry != competitor)
        {
            return Err(io::ErrorKind::AlreadyExists.into());
        }
        if !recorded_before_withdrawal {
            self.record_competitor(pending, &identity)?;
        }
        publish_displaced(displaced, &self.launcher)
    }

    fn record_competitor(
        &self,
        pending: &mut Pending,
        identity: &CompetitorIdentity,
    ) -> io::Result<()> {
        // An exclusive rename consumes the staged entry. Keep its identity
        // durable so a retry can recognize publication before clearing the record.
        let switch = pending
            .launcher_switch
            .as_mut()
            .expect("pending launcher switch");
        if switch.competitor.is_some() {
            // A prior write may have stopped after publishing the record.
            return state::sync_dir(&self.root);
        }
        switch.competitor = Some(identity.clone());
        state::atomic_json(&self.root.join(RECORD), pending)
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
        if let Some(mut pending) = self.pending()?
            && self.settle_launcher_switch(&mut pending)?
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
        let launcher = self.launcher_identity()?;
        let expected = pending
            .launcher
            .ok_or_else(|| "the pending Sai install record has no launcher identity".to_owned())?;
        if (launcher.0, launcher.1) != expected {
            return Err("the Sai launcher changed during the interrupted install".into());
        }
        let active = self.selected_directory()?;
        let invalid =
            || "the pending Sai install record names a missing or invalid binary".to_owned();
        self.target(&pending.new).ok_or_else(invalid)?;
        let previous = pending
            .old
            .as_deref()
            .map(|old| {
                dir_version(old)
                    .filter(|_| self.target(old).is_some())
                    .ok_or_else(invalid)
            })
            .transpose()?;
        if active.as_deref() == Some(pending.new.as_str()) && active != pending.old {
            self.switch_selector(&launcher, &pending.new, pending.old.as_deref())?;
        } else if active != pending.old {
            return Err("the Sai selector changed during an interrupted install".into());
        }
        if self.selected_directory()? != pending.old {
            return Err("the Sai selector changed during an interrupted install".into());
        }
        self.require_launcher(&launcher)?;
        remove_record(&record)?;
        previous.ok_or_else(|| {
            "Interrupted first Sai install was rolled back. Run the installer again.".into()
        })
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
            let launcher = self.launcher_identity()?;
            let old = self.selected_directory()?.ok_or_else(|| {
                "the Sai launcher has no selected binary; run the installer again".to_owned()
            })?;
            if dir_version(&old).as_ref() != Some(installed) {
                return Err("the Sai launcher changed during the update".into());
            }
            let (stage, new) = self.stage(releases, target, home.path()).await?;
            if !allowed() {
                return Ok(Change::Available);
            }
            self.require_launcher(&launcher)?;
            self.publish(stage, &new)?;
            let binary = self
                .target(&new)
                .ok_or_else(|| "the verified replacement binary is missing".to_owned())?;
            let record = self.root.join(RECORD);
            let pending = Pending {
                old: Some(old.clone()),
                new,
                launcher: Some((launcher.0, launcher.1)),
                launcher_switch: None,
            };
            state::atomic_json(&record, &pending).map_err(io_error)?;
            let switched = async {
                self.switch_selector(&launcher, &old, Some(&pending.new))?;
                smoke::verify_as(&binary, &self.launcher, home.path(), target).await?;
                self.require_launcher(&launcher)?;
                if self.selected_directory()?.as_deref() != Some(pending.new.as_str()) {
                    return Err("the Sai selector changed during the update".into());
                }
                remove_record(&record)
            }
            .await;

            match switched {
                Ok(()) => Ok(Change::Updated),
                Err(reason) => {
                    let rollback = self.require_launcher(&launcher).and_then(|()| {
                        let active = self.selected_directory()?;
                        if active.as_deref() == Some(pending.new.as_str()) {
                            self.switch_selector(&launcher, &pending.new, Some(&old))?;
                            let _ = remove_record(&record);
                            Ok(true)
                        } else if active.as_deref() == Some(old.as_str()) {
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
