use std::{
    fs::{self, File},
    io::{self, Read as _, Write as _},
    path::Path,
    time::SystemTime,
};

use chrono::{DateTime, Datelike, NaiveDate, Utc};

use super::Surface;

const PRUNE_MARKER: &str = ".pruned";
const ENDPOINT_KEY: &str = ".endpoint-key";

#[cfg(unix)]
pub(super) struct LogDir(File);

#[cfg(not(unix))]
pub(super) struct LogDir(std::path::PathBuf);

impl LogDir {
    pub fn open(root: &Path) -> io::Result<Self> {
        Self::open_at(root, true)
    }

    pub fn read_only(root: &Path) -> io::Result<Self> {
        Self::open_at(root, false)
    }

    fn open_at(root: &Path, create: bool) -> io::Result<Self> {
        let mut builder = fs::DirBuilder::new();
        builder.recursive(true);
        #[cfg(unix)]
        {
            use rustix::{
                fs::{Mode, OFlags, open},
                process::geteuid,
            };
            use std::os::unix::fs::{DirBuilderExt, MetadataExt};

            builder.mode(0o700);
            if create {
                builder.create(root)?;
            }
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
            if create {
                restrict(&dir, &info)?;
            }
            Ok(Self(dir))
        }
        #[cfg(not(unix))]
        {
            if create {
                builder.create(root)?;
            }
            let metadata = fs::symlink_metadata(root)?;
            if !metadata.is_dir() || redirected(&metadata) {
                return Err(io::ErrorKind::PermissionDenied.into());
            }
            Ok(Self(root.into()))
        }
    }

    #[cfg(unix)]
    pub fn read_file(&self, name: &str) -> io::Result<File> {
        use rustix::{
            fs::{Mode, OFlags, openat},
            process::geteuid,
        };
        use std::os::unix::fs::MetadataExt;

        let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK;
        let file = File::from(openat(&self.0, name, flags, Mode::empty())?);
        let info = file.metadata()?;
        if !info.is_file() || info.nlink() > 1 || info.uid() != geteuid().as_raw() {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        Ok(file)
    }

    #[cfg(not(unix))]
    pub fn read_file(&self, name: &str) -> io::Result<File> {
        let path = self.0.join(name);
        let info = fs::symlink_metadata(&path)?;
        if !info.is_file() || redirected(&info) {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        File::open(path)
    }

    #[cfg(unix)]
    pub fn names(&self) -> io::Result<Vec<String>> {
        use rustix::fs::{AtFlags, Dir, FileType, statat};

        let mut names = Vec::new();
        for entry in Dir::read_from(&self.0)? {
            let entry = entry?;
            let name = entry.file_name();
            if let Ok(text) = name.to_str()
                && parse_name(text).is_some()
                && statat(&self.0, name, AtFlags::SYMLINK_NOFOLLOW).is_ok_and(|stat| {
                    FileType::from_raw_mode(stat.st_mode) == FileType::RegularFile
                })
            {
                names.push(text.into());
            }
        }
        Ok(names)
    }

    #[cfg(not(unix))]
    pub fn names(&self) -> io::Result<Vec<String>> {
        let mut names = Vec::new();
        for entry in fs::read_dir(&self.0)? {
            let entry = entry?;
            if let Ok(name) = entry.file_name().into_string()
                && parse_name(&name).is_some()
                && !redirected(&fs::symlink_metadata(entry.path())?)
                && entry.file_type()?.is_file()
            {
                names.push(name);
            }
        }
        Ok(names)
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

    fn read_key(&self) -> io::Result<Option<[u8; 32]>> {
        let file = match self.read_file(ENDPOINT_KEY) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if file.metadata()?.permissions().mode() & 0o077 != 0 {
                return Err(io::ErrorKind::PermissionDenied.into());
            }
        }
        let mut key = Vec::new();
        file.take(33).read_to_end(&mut key)?;
        key.try_into()
            .map(Some)
            .map_err(|_| io::ErrorKind::InvalidData.into())
    }

    pub fn key(&self, create: bool) -> Option<[u8; 32]> {
        let key = self.read_key().ok()?;
        if key.is_some() || !create {
            return key;
        }
        let lock = self.private_file(".endpoint-key.lock", false).ok()?;
        lock.lock().ok()?;
        if let Some(key) = self.read_key().ok()? {
            return Some(key);
        }
        let mut key = [0; 32];
        ring::rand::SecureRandom::fill(&ring::rand::SystemRandom::new(), &mut key).ok()?;
        let name = format!(".endpoint-key-{}", uuid::Uuid::new_v4().simple());
        let mut staged = self.private_file(&name, true).ok()?;
        let saved = staged.write_all(&key);
        drop(staged);
        let result = saved.and_then(|()| self.publish_key(&name));
        if result.is_err() {
            self.remove_key_stage(&name);
        }
        result.ok().map(|()| key)
    }

    #[cfg(unix)]
    fn publish_key(&self, name: &str) -> io::Result<()> {
        Ok(rustix::fs::renameat(&self.0, name, &self.0, ENDPOINT_KEY)?)
    }

    #[cfg(not(unix))]
    fn publish_key(&self, name: &str) -> io::Result<()> {
        fs::rename(self.0.join(name), self.0.join(ENDPOINT_KEY))
    }

    #[cfg(unix)]
    fn remove_key_stage(&self, name: &str) {
        let _ = rustix::fs::unlinkat(&self.0, name, rustix::fs::AtFlags::empty());
    }

    #[cfg(not(unix))]
    fn remove_key_stage(&self, name: &str) {
        let _ = fs::remove_file(self.0.join(name));
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

pub(super) fn parse_name(name: &str) -> Option<(NaiveDate, usize)> {
    let (surface, date) = name.strip_suffix(".jsonl")?.split_once('-')?;
    let slot = Surface::ALL
        .iter()
        .position(|valid| valid.as_str() == surface)?;
    if date.len() != 10 || date.as_bytes()[4] != b'-' || date.as_bytes()[7] != b'-' {
        return None;
    }
    let day = NaiveDate::parse_from_str(date, "%Y-%m-%d").ok()?;
    (day.year_ce().0 && day.format("%Y-%m-%d").to_string() == date).then_some((day, slot))
}

fn expired(name: &str, keep_days: u64, today: NaiveDate) -> bool {
    parse_name(name).is_some_and(|(date, _)| {
        (today - date).num_days() >= i64::try_from(keep_days).unwrap_or(i64::MAX)
    })
}
