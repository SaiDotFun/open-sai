use std::{fmt::Write as _, io, path::Path};

use clap::Subcommand;
use sai_core::{
    errors::SaiError,
    settings::{self, Environment, Flags, KEYS, Key},
};
use serde::Serialize;
use serde_json::{Map, Value, json, ser::Formatter};

use crate::{load_settings, usage};

#[derive(Subcommand)]
pub enum ConfigCommand {
    /// Show every setting with its value and source.
    List,
    /// Print the exact value of KEY as resolved now.
    Get { key: String },
    /// Set KEY to VALUE in the config file, keeping its comments.
    Set { key: String, value: String },
    /// Remove KEY from the config file so the default applies again.
    Unset { key: String },
}

impl ConfigCommand {
    pub fn run(self, flags: &Flags, env: &Environment, json: bool) -> Result<(), SaiError> {
        let path = env.path();
        let path = path.as_path();
        let json_path = json.then(|| format_path_json(path));
        match self {
            Self::List => {
                let resolved = load_settings(flags, env)?;
                if let Some(path) = json_path {
                    let entries: Map<String, Value> = KEYS
                        .into_iter()
                        .map(|key| {
                            let spec = key.spec();
                            (
                                spec.name.into(),
                                json!({
                                    "value": key.public_value(resolved.value(key)),
                                    "source": resolved.source(key).as_str(),
                                    "env": spec.env,
                                    "default": key.public_value(&key.default_value()),
                                }),
                            )
                        })
                        .collect();
                    println!(
                        "{{\"path\": {path}, \"settings\": {}}}",
                        format_json(&Value::Object(entries))
                    );
                } else {
                    println!("# {}", path.display());
                    for key in KEYS {
                        let spec = key.spec();
                        let value = format_json(&key.public_value(resolved.value(key)));
                        let source = resolved.source(key).as_str();
                        println!("{} = {value}  # {source}, {}", spec.name, spec.env);
                    }
                }
            }
            Self::Get { key } => {
                let key = parse_key(&key)?;
                let resolved = load_settings(flags, env)?;
                let value = resolved.value(key);
                if json {
                    println!(
                        "{}",
                        format_json(&json!({
                            "key": key.spec().name,
                            "value": value,
                            "source": resolved.source(key).as_str(),
                        }))
                    );
                } else if let Some(raw) = value.as_str() {
                    println!("{raw}");
                } else {
                    println!("{value}");
                }
            }
            Self::Set { key, value } => {
                let key = parse_key(&key)?;
                let value = key.parse_value(&value).map_err(|error| {
                    SaiError::usage(format!("{error}. Valid keys: {}", settings::valid_keys()))
                })?;
                settings::set(path, key, &value)?;
                if let Some(path) = json_path {
                    println!(
                        "{{\"key\": {}, \"value\": {}, \"path\": {path}}}",
                        format_json(&json!(key.spec().name)),
                        format_json(&value)
                    );
                } else {
                    println!("Set {} in {}", key.spec().name, path.display());
                }
                warn_override(env, key);
            }
            Self::Unset { key } => {
                let key = parse_key(&key)?;
                let removed = settings::unset(path, key)?;
                if let Some(path) = json_path {
                    println!(
                        "{{\"key\": {}, \"removed\": {removed}, \"path\": {path}}}",
                        format_json(&json!(key.spec().name))
                    );
                } else {
                    let verb = if removed { "Unset" } else { "Already unset:" };
                    println!("{verb} {} in {}", key.spec().name, path.display());
                }
                warn_override(env, key);
            }
        }
        Ok(())
    }
}

fn parse_key(name: &str) -> Result<Key, SaiError> {
    Key::parse(name).map_err(|error| SaiError::usage(error.to_string()))
}

fn warn_override(env: &Environment, key: Key) {
    if env.is_set(key) {
        let spec = key.spec();
        eprintln!(
            "Warning: {} is set and overrides {} in the file",
            spec.env, spec.name
        );
    }
}

pub fn help(command: &str) -> Option<String> {
    let description = match command {
        "config" => {
            "Show and change settings in $SAI_HOME/config.toml.\n\n  Keys: keeper.endpoint, keeper.ws_endpoint, lcd.endpoint, update.auto. A flag\n  or SAI_* environment variable still overrides the file."
        }
        "config get" => "Print the exact value of KEY as resolved now.",
        "config list" => {
            "Show every setting with its value and source.\n\n  The source is flag, env, file, default, or derived. URLs are shown without\n  credentials; `sai config get` prints the exact value."
        }
        "config set" => "Set KEY to VALUE in the config file, keeping its comments.",
        "config unset" => "Remove KEY from the config file so the default applies again.",
        _ => return None,
    };
    let commands = if command == "config" {
        "\nCommands:\n  get    Print the exact value of KEY as resolved now.\n  list   Show every setting with its value and source.\n  set    Set KEY to VALUE in the config file, keeping its comments.\n  unset  Remove KEY from the config file so the default applies again.\n"
    } else {
        ""
    };
    Some(
        format!(
            "{}\n\n  {description}\n\nOptions:\n  --help  Show this message and exit.\n{commands}",
            usage(command)
        )
        .trim_end()
        .to_owned(),
    )
}

struct Spaced;

impl Formatter for Spaced {
    fn begin_object_key<W: ?Sized + io::Write>(
        &mut self,
        output: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if first {
            Ok(())
        } else {
            output.write_all(b", ")
        }
    }

    fn begin_object_value<W: ?Sized + io::Write>(&mut self, output: &mut W) -> io::Result<()> {
        output.write_all(b": ")
    }

    fn begin_array_value<W: ?Sized + io::Write>(
        &mut self,
        output: &mut W,
        first: bool,
    ) -> io::Result<()> {
        self.begin_object_key(output, first)
    }
}

fn format_json(value: &Value) -> String {
    let mut bytes = Vec::new();
    value
        .serialize(&mut serde_json::Serializer::with_formatter(
            &mut bytes, Spaced,
        ))
        .expect("serializing JSON to a buffer");
    let text = String::from_utf8(bytes).expect("JSON is UTF-8");
    let mut escaped = String::with_capacity(text.len());
    for c in text.chars() {
        if c >= '\u{7f}' {
            for unit in c.encode_utf16(&mut [0; 2]) {
                let _ = write!(escaped, "\\u{unit:04x}");
            }
        } else {
            escaped.push(c);
        }
    }
    escaped
}

#[cfg(unix)]
fn format_path_json(path: &Path) -> String {
    use std::os::unix::ffi::OsStrExt;

    // Python decodes filesystem bytes with surrogateescape. U+DC80 through
    // U+DCFF must go straight to JSON escapes because Rust strings cannot
    // represent them.
    let mut escaped = String::from("\"");
    for chunk in path.as_os_str().as_bytes().utf8_chunks() {
        let valid = format_json(&json!(chunk.valid()));
        escaped.push_str(&valid[1..valid.len() - 1]);
        for &byte in chunk.invalid() {
            let _ = write!(escaped, "\\udc{byte:02x}");
        }
    }
    escaped.push('"');
    escaped
}

#[cfg(not(unix))]
fn format_path_json(path: &Path) -> String {
    format_json(&json!(path.to_string_lossy()))
}
