use std::{
    fs, io,
    path::Path,
    thread,
    time::{Duration, Instant},
};

#[cfg(unix)]
use std::{
    fs::File,
    os::unix::fs::{DirBuilderExt, MetadataExt},
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
            set_private_mode(path, &info)?;
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

#[cfg(unix)]
fn set_private_mode(path: &Path, info: &fs::Metadata) -> io::Result<()> {
    use rustix::{
        fs::{Mode, OFlags, fchmod, open},
        io::Errno,
    };

    let flags = OFlags::DIRECTORY | OFlags::NOFOLLOW;
    let mode = Mode::RWXU;
    match open(path, flags | OFlags::RDONLY, Mode::empty()) {
        Ok(fd) => {
            let dir = File::from(fd);
            check_inode(&dir, info)?;
            Ok(fchmod(dir, mode)?)
        }
        // A restrictive umask can leave a new directory unreadable by its owner.
        Err(Errno::ACCESS) if info.mode() & 0o077 == 0 => restore_owner_mode(path, info, mode),
        Err(error) => Err(error.into()),
    }
}

#[cfg(unix)]
fn check_inode(dir: &File, expected: &fs::Metadata) -> io::Result<()> {
    let current = dir.metadata()?;
    if current.dev() != expected.dev() || current.ino() != expected.ino() {
        return Err(io::Error::other("directory changed while setting its mode"));
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn restore_owner_mode(path: &Path, info: &fs::Metadata, mode: rustix::fs::Mode) -> io::Result<()> {
    use std::os::fd::AsRawFd;

    use rustix::fs::{Mode, OFlags, chmod, open};

    // O_PATH can open the directory without read permission. Use its procfs
    // link to chmod the opened inode because fchmod rejects O_PATH descriptors.
    let dir = File::from(open(
        path,
        OFlags::PATH | OFlags::DIRECTORY | OFlags::NOFOLLOW,
        Mode::empty(),
    )?);
    check_inode(&dir, info)?;
    Ok(chmod(format!("/proc/self/fd/{}", dir.as_raw_fd()), mode)?)
}

#[cfg(all(unix, not(target_os = "linux")))]
fn restore_owner_mode(path: &Path, _info: &fs::Metadata, mode: rustix::fs::Mode) -> io::Result<()> {
    use rustix::fs::{AtFlags, CWD, chmodat};

    Ok(chmodat(CWD, path, mode, AtFlags::SYMLINK_NOFOLLOW)?)
}
