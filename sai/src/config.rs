use std::{fmt::Write as _, io};

use clap::Subcommand;
use sai_core::settings::{
    self, Environment, Flags, KEYS, Key, Settings, SettingsError,
};
use serde::Serialize;
use serde_json::{Map, Value, json, ser::Formatter};

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
    pub fn run(
        self,
        flags: &Flags,
        env: &Environment,
        json: bool,
    ) -> Result<(), Error> {
        let path = env.path();
        let path = path.as_path();
        match self {
            Self::List => {
                let resolved = load(flags, env)?;
                if json {
                    let entries: Map<String, Value> = KEYS.into_iter().map(|key| {
                        let spec = key.spec();
                        (spec.name.into(), json!({
                            "value": key.public_value(resolved.value(key)),
                            "source": resolved.source(key).as_str(),
                            "env": spec.env,
                            "default": key.public_value(&key.default_value()),
                        }))
                    }).collect();
                    println!(
                        "{}",
                        format_json(&json!({"path": path, "settings": entries}))
                    );
                } else {
                    println!("# {}", path.display());
                    for key in KEYS {
                        let spec = key.spec();
                        let value =
                            format_json(&key.public_value(resolved.value(key)));
                        let source = resolved.source(key).as_str();
                        println!(
                            "{} = {value}  # {source}, {}",
                            spec.name, spec.env
                        );
                    }
                }
            }
            Self::Get { key } => {
                let key = parse_key(&key)?;
                let resolved = load(flags, env)?;
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
                    Error::usage(format!(
                        "{error}. Valid keys: {}",
                        settings::valid_keys()
                    ))
                })?;
                settings::set(path, key, &value).map_err(Error::settings)?;
                if json {
                    println!(
                        "{}",
                        format_json(
                            &json!({"key": key.spec().name, "value": value, "path": path})
                        )
                    );
                } else {
                    println!("Set {} in {}", key.spec().name, path.display());
                }
                warn_override(env, key);
            }
            Self::Unset { key } => {
                let key = parse_key(&key)?;
                let removed =
                    settings::unset(path, key).map_err(Error::settings)?;
                if json {
                    println!(
                        "{}",
                        format_json(
                            &json!({"key": key.spec().name, "removed": removed, "path": path})
                        )
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

fn parse_key(name: &str) -> Result<Key, Error> {
    Key::parse(name).map_err(|error| Error::usage(error.to_string()))
}

fn load(flags: &Flags, env: &Environment) -> Result<Settings, Error> {
    let settings = Settings::load(flags, env).map_err(Error::settings)?;
    for warning in &settings.warnings {
        eprintln!("{warning}");
    }
    Ok(settings)
}

fn warn_override(env: &Environment, key: Key) {
    if env.value(key).is_some() {
        let spec = key.spec();
        eprintln!(
            "Warning: {} is set and overrides {} in the file",
            spec.env, spec.name
        );
    }
}

pub struct Error {
    pub code: u8,
    pub message: String,
}

impl Error {
    pub fn settings(error: SettingsError) -> Self {
        if error.is_uneditable() {
            Self::usage(error.to_string())
        } else {
            Self {
                code: 1,
                message: error.to_string(),
            }
        }
    }

    pub fn usage(message: String) -> Self {
        Self { code: 2, message }
    }

    pub fn print(&self, command: &str) {
        if self.code == 2 {
            eprintln!(
                "{}\nTry '{}' for help.\n",
                usage(command),
                help_command(command)
            );
        }
        eprintln!("Error: {}", self.message);
    }
}

fn usage(command: &str) -> String {
    let args = match command {
        "config get" | "config unset" => " KEY",
        "config set" => " KEY VALUE",
        "config list" => "",
        _ => " COMMAND [ARGS]...",
    };
    format!("Usage: {} [OPTIONS]{args}", command_line(command))
}

fn command_line(command: &str) -> String {
    if command.is_empty() {
        "sai".into()
    } else {
        format!("sai {command}")
    }
}

fn help_command(command: &str) -> String {
    format!("{} --help", command_line(command))
}

pub fn print_help(command: &str, stderr: bool) -> bool {
    let description = match command {
        "config" => {
            "Show and change settings in $SAI_HOME/config.toml.\n\n  Keys: keeper.endpoint, keeper.ws_endpoint, lcd.endpoint, update.auto. A flag\n  or SAI_* environment variable still overrides the file."
        }
        "config get" => "Print the exact value of KEY as resolved now.",
        "config list" => {
            "Show every setting with its value and source.\n\n  The source is flag, env, file, default, or derived. URLs are shown without\n  credentials; `sai config get` prints the exact value."
        }
        "config set" => {
            "Set KEY to VALUE in the config file, keeping its comments."
        }
        "config unset" => {
            "Remove KEY from the config file so the default applies again."
        }
        _ => return false,
    };
    let commands = if command == "config" {
        "\nCommands:\n  get    Print the exact value of KEY as resolved now.\n  list   Show every setting with its value and source.\n  set    Set KEY to VALUE in the config file, keeping its comments.\n  unset  Remove KEY from the config file so the default applies again.\n"
    } else {
        ""
    };
    let text = format!(
        "{}\n\n  {description}\n\nOptions:\n  --help  Show this message and exit.\n{commands}",
        usage(command)
    );
    if stderr {
        eprint!("{text}");
    } else {
        print!("{text}");
    }
    true
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

    fn begin_object_value<W: ?Sized + io::Write>(
        &mut self,
        output: &mut W,
    ) -> io::Result<()> {
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
