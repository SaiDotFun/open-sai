use std::{
    fs::{self, File},
    io::{self, Read as _, Write as _},
    path::Path,
    time::SystemTime,
};

use chrono::{DateTime, Datelike, NaiveDate, Utc};

use super::Surface;
use crate::paths;

const PRUNE_MARKER: &str = ".pruned";
const ENDPOINT_KEY: &str = ".endpoint-key";
const ENDPOINT_KEY_LOCK: &str = ".endpoint-key.lock";

#[cfg(unix)]
pub(super) struct LogDir(File);

#[cfg(not(unix))]
pub(super) struct LogDir(std::path::PathBuf);

impl LogDir {
    pub fn open(root: &Path) -> io::Result<Self> {
        if let Some(parent) = root.parent() {
            paths::profile(parent)?;
        }
        // Existing log directories keep their owner bits. The open descriptor
        // below clears group and other access.
        if !root.exists() {
            paths::managed(root)?;
        }
        #[cfg(unix)]
        {
            use rustix::{
                fs::{Mode, OFlags, open},
                process::geteuid,
            };
            use std::os::unix::fs::MetadataExt;

            let fd = open(
                root,
                OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW,
                Mode::empty(),
            )?;
            let dir = File::from(fd);
            let info = dir.metadata()?;
            if info.uid() != geteuid().as_raw() {
                return Err(io::ErrorKind::PermissionDenied.into());
            }
            restrict(&dir, &info)?;
            Ok(Self(dir))
        }
        #[cfg(not(unix))]
        {
            if redirected(&fs::symlink_metadata(root)?) {
                return Err(io::ErrorKind::PermissionDenied.into());
            }
            Ok(Self(root.into()))
        }
    }

    #[cfg(unix)]
    fn private_file(&self, name: &str, exclusive: bool) -> io::Result<File> {
        use rustix::{
            fs::{Mode, OFlags, openat},
            process::geteuid,
        };
        use std::os::unix::fs::MetadataExt;

        let mut flags =
            OFlags::WRONLY | OFlags::APPEND | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::NONBLOCK;
        flags.set(OFlags::EXCL, exclusive);
        let file = File::from(openat(&self.0, name, flags, Mode::RUSR | Mode::WUSR)?);
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.nlink() > 1 || metadata.uid() != geteuid().as_raw() {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        restrict(&file, &metadata)?;
        Ok(file)
    }

    #[cfg(not(unix))]
    fn private_file(&self, name: &str, exclusive: bool) -> io::Result<File> {
        let target = self.0.join(name);
        if let Ok(info) = fs::symlink_metadata(&target)
            && (!info.is_file() || redirected(&info))
        {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        fs::OpenOptions::new()
            .create(true)
            .create_new(exclusive)
            .append(true)
            .open(target)
    }

    pub fn append(&self, name: &str, data: &[u8]) -> io::Result<()> {
        let mut file = self.private_file(name, false)?;
        // One O_APPEND write keeps each JSONL record together across processes.
        let written = file.write(data)?;
        if written == data.len() {
            Ok(())
        } else {
            Err(io::ErrorKind::WriteZero.into())
        }
    }

    pub fn endpoint_key(&self) -> io::Result<[u8; 32]> {
        match self.read_endpoint_key() {
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            result => return result,
        }
        let lock = self.private_file(ENDPOINT_KEY_LOCK, false)?;
        lock.lock()?;
        match self.read_endpoint_key() {
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            result => return result,
        }
        let mut key = [0; 32];
        use ring::rand::SecureRandom;
        ring::rand::SystemRandom::new()
            .fill(&mut key)
            .map_err(|_| io::Error::other("could not generate log identity key"))?;
        let staged = format!(".endpoint-key-{}", uuid::Uuid::new_v4().simple());
        let mut file = self.private_file(&staged, true)?;
        let result = file.write_all(&key);
        drop(file);
        self.publish_endpoint_key(&staged, result)?;
        Ok(key)
    }

    #[cfg(unix)]
    fn publish_endpoint_key(&self, staged: &str, written: io::Result<()>) -> io::Result<()> {
        use rustix::fs::{AtFlags, renameat, unlinkat};
        let result = written
            .and_then(|()| renameat(&self.0, staged, &self.0, ENDPOINT_KEY).map_err(Into::into));
        if result.is_err() {
            let _ = unlinkat(&self.0, staged, AtFlags::empty());
        }
        result
    }

    #[cfg(not(unix))]
    fn publish_endpoint_key(&self, staged: &str, written: io::Result<()>) -> io::Result<()> {
        let staged = self.0.join(staged);
        let result = written.and_then(|()| fs::rename(&staged, self.0.join(ENDPOINT_KEY)));
        if result.is_err() {
            let _ = fs::remove_file(staged);
        }
        result
    }

    fn read_endpoint_key(&self) -> io::Result<[u8; 32]> {
        #[cfg(unix)]
        let file = {
            use rustix::{
                fs::{Mode, OFlags, openat},
                process::geteuid,
            };
            use std::os::unix::fs::MetadataExt;
            let file = File::from(openat(
                &self.0,
                ENDPOINT_KEY,
                OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK,
                Mode::empty(),
            )?);
            let metadata = file.metadata()?;
            if !metadata.is_file()
                || metadata.nlink() > 1
                || metadata.uid() != geteuid().as_raw()
                || metadata.mode() & 0o077 != 0
            {
                return Err(io::ErrorKind::PermissionDenied.into());
            }
            file
        };
        #[cfg(not(unix))]
        let file = {
            let target = self.0.join(ENDPOINT_KEY);
            let metadata = fs::symlink_metadata(&target)?;
            if !metadata.is_file() || redirected(&metadata) {
                return Err(io::ErrorKind::PermissionDenied.into());
            }
            File::open(target)?
        };
        let mut bytes = Vec::with_capacity(33);
        file.take(33).read_to_end(&mut bytes)?;
        bytes
            .try_into()
            .map_err(|_| io::ErrorKind::InvalidData.into())
    }

    pub fn prune(&self, keep_days: u64, today: NaiveDate) -> io::Result<()> {
        let (marker, created) = match self.private_file(PRUNE_MARKER, true) {
            Ok(marker) => (marker, true),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                (self.private_file(PRUNE_MARKER, false)?, false)
            }
            Err(error) => return Err(error),
        };
        let modified = || {
            marker
                .metadata()?
                .modified()
                .map(|time| DateTime::<Utc>::from(time).date_naive())
        };
        if !created && modified()? >= today {
            return Ok(());
        }
        let Ok(()) = marker.try_lock() else {
            return Ok(());
        };
        if !created && modified()? >= today {
            return Ok(());
        }
        self.remove_expired(keep_days, today)?;
        marker.set_modified(SystemTime::now())
    }

    #[cfg(unix)]
    fn remove_expired(&self, keep_days: u64, today: NaiveDate) -> io::Result<()> {
        use rustix::fs::{AtFlags, Dir, FileType, statat, unlinkat};

        for entry in Dir::read_from(&self.0)? {
            let entry = entry?;
            let name = entry.file_name();
            if name
                .to_str()
                .ok()
                .is_some_and(|name| expired(name, keep_days, today))
                && statat(&self.0, name, AtFlags::SYMLINK_NOFOLLOW).is_ok_and(|stat| {
                    FileType::from_raw_mode(stat.st_mode) == FileType::RegularFile
                })
            {
                let _ = unlinkat(&self.0, name, AtFlags::empty());
            }
        }
        Ok(())
    }

    #[cfg(not(unix))]
    fn remove_expired(&self, keep_days: u64, today: NaiveDate) -> io::Result<()> {
        for entry in fs::read_dir(&self.0)? {
            let entry = entry?;
            if entry
                .file_name()
                .to_str()
                .is_some_and(|name| expired(name, keep_days, today))
                && entry.file_type()?.is_file()
            {
                let _ = fs::remove_file(entry.path());
            }
        }
        Ok(())
    }
}

#[cfg(unix)]
fn restrict(file: &File, metadata: &fs::Metadata) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let mode = metadata.permissions().mode();
    if mode & 0o077 != 0 {
        file.set_permissions(fs::Permissions::from_mode(mode & 0o7700))?;
    }
    Ok(())
}

#[cfg(windows)]
fn redirected(metadata: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    metadata.file_attributes() & 0x400 != 0
}

#[cfg(not(any(unix, windows)))]
fn redirected(metadata: &fs::Metadata) -> bool {
    metadata.is_symlink()
}

fn expired(name: &str, keep_days: u64, today: NaiveDate) -> bool {
    let Some((surface, date)) = name
        .strip_suffix(".jsonl")
        .and_then(|name| name.split_once('-'))
    else {
        return false;
    };
    if !Surface::ALL.iter().any(|valid| valid.as_str() == surface)
        || date.len() != 10
        || date.as_bytes()[4] != b'-'
        || date.as_bytes()[7] != b'-'
    {
        return false;
    }
    NaiveDate::parse_from_str(date, "%Y-%m-%d").is_ok_and(|date| {
        date.format("%Y-%m-%d").to_string().len() == 10
            && date.year_ce().0
            && (today - date).num_days() >= i64::try_from(keep_days).unwrap_or(i64::MAX)
    })
}
