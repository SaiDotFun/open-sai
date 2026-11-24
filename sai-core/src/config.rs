use std::{
    env, fs, io,
    path::{Component, Path, PathBuf},
};

const SETTINGS: [(&str, &str, &str); 6] = [
    ("keeper", "endpoint", "keeper.endpoint"),
    ("keeper", "ws_endpoint", "keeper.ws_endpoint"),
    ("lcd", "endpoint", "lcd.endpoint"),
    ("update", "auto", "update.auto"),
    ("logs", "enabled", "logs.enabled"),
    ("logs", "keep_days", "logs.keep_days"),
];

pub struct ConfigListing {
    pub path: PathBuf,
    pub configured_keys: Vec<&'static str>,
}

pub fn config_path() -> io::Result<PathBuf> {
    config_path_from(env::var_os("SAI_HOME").map(PathBuf::from), python_home())
}

fn config_path_from(sai_home: Option<PathBuf>, home: Option<PathBuf>) -> io::Result<PathBuf> {
    // The Python settings reader evaluates Path.home() even with a Sai home override.
    let Some(home) = home.filter(|path| !path.as_os_str().as_encoded_bytes().starts_with(b"~"))
    else {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "Cannot find the home directory",
        ));
    };
    let home = sai_home.unwrap_or_else(|| home.join(".sai"));
    Ok(normalize_path(&home).join("config.toml"))
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
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;

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

fn normalize_path(path: &Path) -> PathBuf {
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

/// List recognized keys present in the file. This stub leaves resolution and
/// value validation to the full settings port.
pub fn list_config() -> io::Result<ConfigListing> {
    let path = config_path()?;
    let text = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) if error.kind() == io::ErrorKind::NotFound => String::new(),
        Err(error) => {
            return Err(io::Error::new(
                error.kind(),
                format!("Cannot read {}: {error}", path.display()),
            ));
        }
    };

    // TOML parse errors can quote the original line, including credentials.
    let configured_keys = listing::configured_keys(&text).map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("Cannot parse {} as TOML", path.display()),
        )
    })?;
    Ok(ConfigListing {
        path,
        configured_keys,
    })
}

mod listing;

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn empty_home_uses_root_and_missing_home_uses_account_directory() {
        let account_home = || Some(PathBuf::from("/home/fixture"));
        let empty = posix_home(Some(PathBuf::new()), account_home);
        let missing = posix_home(None, account_home);

        assert_eq!(
            config_path_from(None, empty).unwrap(),
            Path::new("/.sai/config.toml")
        );
        assert_eq!(
            config_path_from(None, missing).unwrap(),
            Path::new("/home/fixture/.sai/config.toml")
        );
    }
}
