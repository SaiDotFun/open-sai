use std::{env, fs, io, path::PathBuf};

use toml_edit::Document;

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
    let home = env::var_os("SAI_HOME")
        .map(PathBuf::from)
        .or_else(|| env::home_dir().map(|path| path.join(".sai")))
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "Cannot find the home directory",
            )
        })?;
    Ok(home.join("config.toml"))
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
    let document = Document::parse(&text).map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("Cannot parse {} as TOML", path.display()),
        )
    })?;
    let configured_keys = SETTINGS
        .iter()
        .filter_map(|&(section, key, name)| {
            document
                .get(section)?
                .as_table_like()?
                .get(key)
                .map(|_| name)
        })
        .collect();

    Ok(ConfigListing {
        path,
        configured_keys,
    })
}
