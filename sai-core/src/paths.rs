use std::{
    fs, io,
    path::Path,
    thread,
    time::{Duration, Instant},
};

#[cfg(unix)]
use std::os::{
    fd::AsFd,
    unix::fs::{DirBuilderExt, MetadataExt},
};

#[derive(Clone, Copy)]
enum Existing {
    Preserve,
    Restrict,
    Ignore,
}

pub(crate) fn profile(path: &Path) -> io::Result<()> {
    let path = if path.as_os_str().is_empty() {
        Path::new(".")
    } else {
        path
    };
    private_directory(path, Existing::Preserve)
}

pub(crate) fn managed(path: &Path) -> io::Result<()> {
    private_directory(path, Existing::Restrict)
}

fn private_directory(path: &Path, existing: Existing) -> io::Result<()> {
    let create = match mkdir_private(path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            let parent = path
                .parent()
                .filter(|parent| !parent.as_os_str().is_empty())
                .ok_or(error)?;
            private_directory(parent, Existing::Ignore)?;
            mkdir_private(path)
        }
        result => result,
    };
    let created = match create {
        Ok(()) => true,
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            if matches!(existing, Existing::Ignore) {
                return Ok(());
            }
            false
        }
        Err(error) => return Err(error),
    };
    let info = fs::symlink_metadata(path)?;
    if info.is_symlink() {
        return Err(io::Error::other(format!(
            "{} is a symlink or reparse point",
            path.display()
        )));
    }
    if !info.is_dir() {
        return Err(io::ErrorKind::NotADirectory.into());
    }
    #[cfg(unix)]
    {
        if info.uid() != rustix::process::getuid().as_raw() {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        if (created || matches!(existing, Existing::Restrict)) && info.mode() & 0o7777 != 0o700 {
            use rustix::fs::{AtFlags, CWD, statat};
            let expected = statat(CWD, path, AtFlags::SYMLINK_NOFOLLOW)?;
            // Compare both metadata reads before changing the mode.
            let (device, inode): (u64, u64) = (expected.st_dev as _, expected.st_ino as _);
            if device != info.dev() || inode != info.ino() {
                return Err(io::Error::other("directory changed while setting its mode"));
            }
            set_private_mode(CWD, path, &expected)?;
        }
    }
    Ok(())
}

fn mkdir_private(path: &Path) -> io::Result<()> {
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    builder.mode(0o700);
    let deadline = Instant::now() + Duration::from_millis(500);
    loop {
        match builder.create(path) {
            Err(error)
                if cfg!(unix)
                    && error.kind() == io::ErrorKind::PermissionDenied
                    && Instant::now() < deadline =>
            {
                // Another process may still be restoring a new parent's mode.
                thread::sleep(Duration::from_millis(10));
            }
            result => return result,
        }
    }
}

/// Create a managed child relative to an open parent, preserving existing modes.
#[cfg(unix)]
pub(crate) fn create_managed_at(parent: impl AsFd, name: &Path) -> io::Result<()> {
    use rustix::{
        fs::{AtFlags, FileType, Mode, mkdirat, statat},
        io::Errno,
        process::getuid,
    };

    match mkdirat(&parent, name, Mode::RWXU) {
        Ok(()) => {
            let info = statat(&parent, name, AtFlags::SYMLINK_NOFOLLOW)?;
            if FileType::from_raw_mode(info.st_mode) != FileType::Directory
                || info.st_uid != getuid().as_raw()
            {
                return Err(io::ErrorKind::PermissionDenied.into());
            }
            if info.st_mode & 0o7777 != 0o700 {
                set_private_mode(parent, name, &info)?;
            }
            Ok(())
        }
        Err(Errno::EXIST) => Ok(()),
        Err(error) => Err(error.into()),
    }
}

#[cfg(unix)]
fn set_private_mode(parent: impl AsFd, path: &Path, info: &rustix::fs::Stat) -> io::Result<()> {
    use rustix::{
        fs::{Mode, OFlags, fchmod, openat},
        io::Errno,
    };

    let flags = OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    match openat(&parent, path, flags | OFlags::RDONLY, Mode::empty()) {
        Ok(dir) => {
            check_inode(&dir, info)?;
            Ok(fchmod(dir, Mode::RWXU)?)
        }
        // A restrictive umask can leave a new directory unreadable by its owner.
        Err(Errno::ACCESS) if info.st_mode & 0o077 == 0 => restore_owner_mode(parent, path, info),
        Err(error) => Err(error.into()),
    }
}

#[cfg(unix)]
fn check_inode(dir: impl AsFd, expected: &rustix::fs::Stat) -> io::Result<()> {
    let current = rustix::fs::fstat(dir)?;
    if current.st_dev != expected.st_dev || current.st_ino != expected.st_ino {
        return Err(io::Error::other("directory changed while setting its mode"));
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn restore_owner_mode(parent: impl AsFd, path: &Path, info: &rustix::fs::Stat) -> io::Result<()> {
    use std::os::fd::AsRawFd;

    use rustix::fs::{Mode, OFlags, chmod, openat};

    // O_PATH can open the directory without read permission. Use its procfs
    // link to chmod the opened inode because fchmod rejects O_PATH descriptors.
    let dir = openat(
        parent,
        path,
        OFlags::PATH | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    check_inode(&dir, info)?;
    Ok(chmod(
        format!("/proc/self/fd/{}", dir.as_raw_fd()),
        Mode::RWXU,
    )?)
}

#[cfg(all(unix, not(target_os = "linux")))]
fn restore_owner_mode(parent: impl AsFd, path: &Path, _info: &rustix::fs::Stat) -> io::Result<()> {
    use rustix::fs::{AtFlags, Mode, chmodat};

    Ok(chmodat(
        parent,
        path,
        Mode::RWXU,
        AtFlags::SYMLINK_NOFOLLOW,
    )?)
}
