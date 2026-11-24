use std::{
    env,
    path::{Component, Path, PathBuf},
};

pub(super) fn from_process() -> Option<PathBuf> {
    sai_home(env::var_os("SAI_HOME").map(PathBuf::from), python_home())
}

fn sai_home(sai: Option<PathBuf>, home: Option<PathBuf>) -> Option<PathBuf> {
    // Path.home() is evaluated in Python even when SAI_HOME is set.
    let home = home.filter(|path| !path.as_os_str().as_encoded_bytes().starts_with(b"~"))?;
    Some(normalize(&sai.unwrap_or_else(|| home.join(".sai"))))
}

#[cfg(unix)]
fn python_home() -> Option<PathBuf> {
    posix_home(env::var_os("HOME").map(PathBuf::from), env::home_dir)
}

#[cfg(unix)]
fn posix_home(
    home: Option<PathBuf>,
    account_home: impl FnOnce() -> Option<PathBuf>,
) -> Option<PathBuf> {
    use std::{ffi::OsStr, os::unix::ffi::OsStrExt};

    let home = home.or_else(account_home)?;
    let bytes = home.as_os_str().as_bytes();
    let end = bytes.iter().rposition(|&byte| byte != b'/');
    let expanded = end.map_or(&b"/"[..], |end| &bytes[..=end]);
    Some(PathBuf::from(OsStr::from_bytes(expanded)))
}

#[cfg(windows)]
fn python_home() -> Option<PathBuf> {
    env::var_os("USERPROFILE").map(PathBuf::from).or_else(|| {
        let path = env::var_os("HOMEPATH")?;
        let drive = env::var_os("HOMEDRIVE").unwrap_or_default();
        Some(PathBuf::from(drive).join(path))
    })
}

#[cfg(not(any(unix, windows)))]
fn python_home() -> Option<PathBuf> {
    env::home_dir()
}

pub(super) fn normalize(path: &Path) -> PathBuf {
    let mut components = path.components();
    let mut normalized = PathBuf::new();
    #[cfg(unix)]
    {
        let bytes = path.as_os_str().as_encoded_bytes();
        if bytes.starts_with(b"//") && !bytes.starts_with(b"///") {
            normalized.push("//");
            components.next();
        }
    }
    normalized.extend(components.filter(|part| *part != Component::CurDir));
    normalized
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn empty_home_uses_root_and_missing_home_uses_account_directory() {
        let account_home = || Some(PathBuf::from("/home/fixture"));
        let empty = posix_home(Some(PathBuf::new()), account_home);
        let missing = posix_home(None, account_home);

        assert_eq!(sai_home(None, empty).unwrap(), Path::new("/.sai"));
        assert_eq!(
            sai_home(None, missing).unwrap(),
            Path::new("/home/fixture/.sai")
        );
    }
}
