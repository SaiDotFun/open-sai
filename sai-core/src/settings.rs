//! Resolve settings shared by the CLI and MCP server.

mod edit;
mod home;
mod toml;

use std::{
    env, fmt, fs, io,
    path::{Path, PathBuf},
};

use serde_json::{Value, json};
use toml_edit::{Item, TomlError};

use crate::endpoint::Endpoint;
use toml::ParsedDocument;

pub const FILE_NAME: &str = "config.toml";
const MAINNET_HTTP: &str = "https://sai-keeper.nibiru.fi/query";
const MAINNET_WS: &str = "wss://sai-keeper.nibiru.fi/query";
const MAINNET_LCD: &str = "https://lcd.nibiru.fi";
const DEFAULT_KEEP_DAYS: u64 = 14;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Key {
    KeeperEndpoint,
    KeeperWsEndpoint,
    LcdEndpoint,
    UpdateAuto,
    LogsEnabled,
    LogsKeepDays,
}

pub const KEYS: [Key; 6] = [
    Key::KeeperEndpoint,
    Key::KeeperWsEndpoint,
    Key::LcdEndpoint,
    Key::UpdateAuto,
    Key::LogsEnabled,
    Key::LogsKeepDays,
];

#[derive(Clone, Copy)]
enum Kind {
    Url([&'static str; 2]),
    Bool,
    Days,
}

pub struct Setting {
    pub name: &'static str,
    pub env: &'static str,
    pub flag: Option<&'static str>,
    kind: Kind,
}

const HTTP: Kind = Kind::Url(["https", "http"]);
const WS: Kind = Kind::Url(["wss", "ws"]);
const SETTINGS: [Setting; KEYS.len()] = [
    Setting {
        name: "keeper.endpoint",
        env: "SAI_ENDPOINT",
        flag: Some("--endpoint"),
        kind: HTTP,
    },
    Setting {
        name: "keeper.ws_endpoint",
        env: "SAI_WS_ENDPOINT",
        flag: Some("--ws-endpoint"),
        kind: WS,
    },
    Setting {
        name: "lcd.endpoint",
        env: "SAI_LCD_ENDPOINT",
        flag: Some("--lcd-endpoint"),
        kind: HTTP,
    },
    Setting {
        name: "update.auto",
        env: "SAI_UPDATE_AUTO",
        flag: None,
        kind: Kind::Bool,
    },
    Setting {
        name: "logs.enabled",
        env: "SAI_LOG",
        flag: None,
        kind: Kind::Bool,
    },
    Setting {
        name: "logs.keep_days",
        env: "SAI_LOG_KEEP_DAYS",
        flag: None,
        kind: Kind::Days,
    },
];

impl Key {
    pub const fn spec(self) -> &'static Setting {
        &SETTINGS[self as usize]
    }

    pub fn parse(name: &str) -> Result<Self, SettingsError> {
        find_key(name).ok_or_else(|| {
            SettingsError::invalid(format!(
                "Unknown setting {}. Valid keys: {}",
                repr(name),
                valid_keys()
            ))
        })
    }

    pub fn parse_value(self, raw: &str) -> Result<Value, SettingsError> {
        let text = raw.trim();
        let name = self.spec().name;
        match self.spec().kind {
            Kind::Url(_) => self.check_url(text),
            Kind::Bool => match parse_bool(text) {
                Some(value) => Ok(Value::Bool(value)),
                None => Err(SettingsError::invalid(format!(
                    "Invalid value {} for {name}: expected true or false",
                    repr(raw)
                ))),
            },
            Kind::Days => {
                if text.is_empty() || !text.bytes().all(|c| c.is_ascii_digit()) {
                    return Err(SettingsError::invalid(format!(
                        "Invalid value {} for {name}: expected a whole number, 1 or more",
                        repr(raw)
                    )));
                }
                let digits = text.trim_start_matches('0');
                if digits.is_empty() {
                    return Err(self.expected("a whole number, 1 or more"));
                }
                serde_json::from_str(digits)
                    .map_err(|_| self.expected("a whole number, 1 or more"))
            }
        }
    }

    pub fn default_value(self) -> Value {
        match self {
            Self::KeeperEndpoint => json!(MAINNET_HTTP),
            Self::KeeperWsEndpoint => Value::Null,
            Self::LcdEndpoint => json!(MAINNET_LCD),
            Self::UpdateAuto | Self::LogsEnabled => Value::Bool(true),
            Self::LogsKeepDays => json!(DEFAULT_KEEP_DAYS),
        }
    }

    pub fn public_value(self, value: &Value) -> Value {
        if let (Kind::Url(_), Some(endpoint)) =
            (self.spec().kind, value.as_str())
            && let Some(endpoint) = Endpoint::split(endpoint)
        {
            let (host, port) = endpoint.host_port();
            let authority = match port {
                Ok(Some(port)) => format!("{host}:{port}"),
                _ => host,
            };
            return json!(format!(
                "{}://{authority}{}",
                endpoint.scheme, endpoint.path
            ));
        }
        value.clone()
    }

    fn section_and_name(self) -> (&'static str, &'static str) {
        self.spec()
            .name
            .split_once('.')
            .expect("setting has a section")
    }

    fn is_log(self) -> bool {
        matches!(self, Self::LogsEnabled | Self::LogsKeepDays)
    }

    fn expected(self, expected: &str) -> SettingsError {
        SettingsError::invalid(format!(
            "Invalid value for {}: expected {expected}",
            self.spec().name
        ))
    }

    fn check_url(self, value: &str) -> Result<Value, SettingsError> {
        let Kind::Url(schemes) = self.spec().kind else {
            unreachable!()
        };
        if Endpoint::split(value)
            .is_some_and(|url| schemes.contains(&url.scheme.as_str()))
        {
            return Ok(json!(value));
        }
        Err(self.expected(&format!(
            "a URL starting with {}:// or {}://",
            schemes[0], schemes[1]
        )))
    }

    fn read_value(
        self,
        item: &Item,
        document: &ParsedDocument,
    ) -> Result<Value, SettingsError> {
        match self.spec().kind {
            Kind::Url(_) => self.check_url(
                item.as_str().ok_or_else(|| self.expected("a URL string"))?,
            ),
            Kind::Bool => item
                .as_bool()
                .map(Value::Bool)
                .ok_or_else(|| self.expected("true or false")),
            Kind::Days => {
                let oversized = item
                    .span()
                    .and_then(|span| document.oversized_number(&span));
                oversized
                    .filter(|value| !value.to_string().starts_with('-'))
                    .or_else(|| {
                        item.as_integer()
                            .filter(|&value| value >= 1)
                            .map(|value| json!(value))
                    })
                    .ok_or_else(|| self.expected("a whole number, 1 or more"))
            }
        }
    }
}

fn find_key(name: &str) -> Option<Key> {
    KEYS.into_iter().find(|key| key.spec().name == name)
}

pub fn valid_keys() -> String {
    KEYS.map(|key| key.spec().name).join(", ")
}

pub fn repr(text: &str) -> String {
    use fmt::Write;
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut result = String::from(quote);
    for c in text.chars() {
        match c {
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            '\\' => result.push_str("\\\\"),
            c if c == quote => {
                result.push('\\');
                result.push(c);
            }
            c if c.is_control() || (c != ' ' && c.is_whitespace()) => {
                let _ = match u32::from(c) {
                    code @ 0..=0xff => write!(result, "\\x{code:02x}"),
                    code @ 0x100..=0xffff => write!(result, "\\u{code:04x}"),
                    code => write!(result, "\\U{code:08x}"),
                };
            }
            c => result.push(c),
        }
    }
    result.push(quote);
    result
}

fn parse_bool(raw: &str) -> Option<bool> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => Some(true),
        "false" | "0" | "no" | "off" => Some(false),
        _ => None,
    }
}

#[derive(Debug)]
pub struct SettingsError {
    message: String,
    uneditable: bool,
}

impl SettingsError {
    fn invalid(message: String) -> Self {
        Self {
            message,
            uneditable: false,
        }
    }

    pub fn is_uneditable(&self) -> bool {
        self.uneditable
    }
}

impl fmt::Display for SettingsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for SettingsError {}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum Source {
    Flag,
    Env,
    File,
    Default,
    Derived,
    Invalid,
}

impl Source {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Flag => "flag",
            Self::Env => "env",
            Self::File => "file",
            Self::Default => "default",
            Self::Derived => "derived",
            Self::Invalid => "invalid",
        }
    }
}

/// A snapshot of SAI_HOME and the registry's environment variables.
#[derive(Clone)]
pub struct Environment {
    pub home: PathBuf,
    values: [Option<String>; KEYS.len()],
}

impl Environment {
    pub fn new(home: PathBuf, get: impl Fn(&str) -> Option<String>) -> Self {
        Self {
            home: home::normalize(&home),
            values: KEYS.map(|key| get(key.spec().env)),
        }
    }

    pub fn from_process() -> Result<Self, SettingsError> {
        let home = home::from_process().ok_or_else(|| {
            SettingsError::invalid("Cannot find the home directory".into())
        })?;
        Ok(Self::new(home, |name| env::var(name).ok()))
    }

    pub fn path(&self) -> PathBuf {
        self.home.join(FILE_NAME)
    }

    pub fn value(&self, key: Key) -> Option<&str> {
        self.values[key as usize]
            .as_deref()
            .filter(|value| !value.trim().is_empty())
    }
}

#[derive(Default)]
pub struct Flags {
    pub endpoint: Option<String>,
    pub ws_endpoint: Option<String>,
    pub lcd_endpoint: Option<String>,
}

impl Flags {
    fn value(&self, key: Key) -> Option<&str> {
        match key {
            Key::KeeperEndpoint => self.endpoint.as_deref(),
            Key::KeeperWsEndpoint => self.ws_endpoint.as_deref(),
            Key::LcdEndpoint => self.lcd_endpoint.as_deref(),
            _ => None,
        }
    }
}

#[derive(Debug)]
pub struct Settings {
    entries: [(Value, Source); KEYS.len()],
    pub warnings: Vec<String>,
}

impl Settings {
    pub fn load(
        flags: &Flags,
        env: &Environment,
    ) -> Result<Self, SettingsError> {
        let path = env.path();
        let file = FileSettings::read(&path)?;
        let mut resolved = Self {
            entries: KEYS.map(|key| (key.default_value(), Source::Default)),
            warnings: file.warnings,
        };
        for key in KEYS {
            let index = key as usize;
            let (value, source) =
                pick(key, flags, env, file.entries[index].as_ref());
            resolved.entries[index] = match value {
                Ok(value) => (value, source),
                Err(_) if key.is_log() => {
                    let already_off = resolved.value(Key::LogsEnabled).as_bool()
                        == Some(false);
                    let source =
                        if already_off { source } else { Source::Invalid };
                    if source == Source::Invalid && env.value(key).is_some() {
                        resolved.warnings.push(format!(
                            "{}: invalid value for {}; logging is off until it is fixed",
                            key.spec().env,
                            key.spec().name
                        ));
                    }
                    (key.default_value(), source)
                }
                Err(error) => {
                    let origin = if source == Source::Flag {
                        key.spec().flag.unwrap_or(key.spec().name)
                    } else {
                        key.spec().env
                    };
                    return Err(SettingsError::invalid(format!(
                        "{origin}: {error}"
                    )));
                }
            };
        }
        let http = Key::KeeperEndpoint;
        let ws = Key::KeeperWsEndpoint;
        if resolved.value(ws).is_null()
            || resolved.source(ws) > resolved.source(http)
        {
            let endpoint = Endpoint::split(resolved.endpoint())
                .expect("validated Keeper URL");
            resolved.entries[ws as usize] =
                (json!(endpoint.websocket()), Source::Derived);
        }
        if [Key::LogsEnabled, Key::LogsKeepDays]
            .into_iter()
            .any(|key| resolved.source(key) == Source::Invalid)
        {
            resolved.entries[Key::LogsEnabled as usize].0 = Value::Bool(false);
        }
        Ok(resolved)
    }

    pub fn value(&self, key: Key) -> &Value {
        &self.entries[key as usize].0
    }

    pub fn source(&self, key: Key) -> Source {
        self.entries[key as usize].1
    }

    pub fn endpoint(&self) -> &str {
        self.url(Key::KeeperEndpoint)
    }

    pub fn ws_endpoint(&self) -> &str {
        self.url(Key::KeeperWsEndpoint)
    }

    pub fn lcd_endpoint(&self) -> &str {
        self.url(Key::LcdEndpoint)
    }

    fn url(&self, key: Key) -> &str {
        self.value(key).as_str().expect("resolved URL")
    }
}

type Entry = Result<Value, SettingsError>;

#[derive(Default)]
struct FileSettings {
    entries: [Option<Entry>; KEYS.len()],
    warnings: Vec<String>,
}

impl FileSettings {
    fn read(path: &Path) -> Result<Self, SettingsError> {
        let Some(text) = read_text(path)? else {
            return Ok(Self::default());
        };
        let parsed = ParsedDocument::parse(&text)
            .map_err(|error| parse_error(path, &text, &error, false))?;
        let mut file = Self::default();
        let path = path.display();
        for (section, item) in parsed.document.iter() {
            if let Some(table) = item.as_table_like() {
                for (name, item) in table.iter() {
                    let name = format!("{section}.{name}");
                    let Some(key) = find_key(&name) else {
                        file.warnings.push(format!(
                            "{path}: ignoring unknown setting {}",
                            repr(&name)
                        ));
                        continue;
                    };
                    let entry = key.read_value(item, &parsed);
                    if let Err(ref error) = entry {
                        if !key.is_log() {
                            return Err(SettingsError::invalid(format!(
                                "{path}: {error}"
                            )));
                        }
                        file.warnings.push(format!(
                            "{path}: {error}; logging is off until it is fixed"
                        ));
                    }
                    file.entries[key as usize] = Some(entry);
                }
            } else if section == "logs" {
                let message = table_error(section);
                for key in [Key::LogsEnabled, Key::LogsKeepDays] {
                    file.entries[key as usize] =
                        Some(Err(SettingsError::invalid(message.clone())));
                }
                file.warnings.push(format!(
                    "{path}: {message}; logging is off until it is fixed"
                ));
            } else if KEYS.iter().any(|key| key.section_and_name().0 == section)
            {
                return Err(SettingsError::invalid(format!(
                    "{path}: {}",
                    table_error(section)
                )));
            } else {
                file.warnings.push(format!(
                    "{path}: ignoring unknown setting {}",
                    repr(section)
                ));
            }
        }
        Ok(file)
    }
}

fn table_error(section: &str) -> String {
    format!("{} must be a table, such as [{section}]", repr(section))
}

fn pick(
    key: Key,
    flags: &Flags,
    env: &Environment,
    file: Option<&Entry>,
) -> (Entry, Source) {
    if let Some(raw) = flags.value(key) {
        (key.parse_value(raw), Source::Flag)
    } else if let Some(raw) = env.value(key) {
        (key.parse_value(raw), Source::Env)
    } else if let Some(value) = file {
        let entry = value
            .as_ref()
            .map(Clone::clone)
            .map_err(|error| SettingsError::invalid(error.message.clone()));
        (entry, Source::File)
    } else {
        (Ok(key.default_value()), Source::Default)
    }
}

/// The writer uses file and environment settings independently of CLI flags.
pub(crate) fn log_keep_days(env: &Environment) -> Option<u64> {
    let file = FileSettings::read(&env.path()).ok()?;
    let flags = Flags::default();
    let value = |key: Key| {
        pick(key, &flags, env, file.entries[key as usize].as_ref())
            .0
            .ok()
    };
    if value(Key::LogsEnabled)?.as_bool()? {
        Some(value(Key::LogsKeepDays)?.as_u64().unwrap_or(u64::MAX))
    } else {
        None
    }
}

pub(crate) fn log_disabled_in_env(env: &Environment) -> bool {
    env.value(Key::LogsEnabled)
        .is_some_and(|raw| parse_bool(raw) != Some(true))
}

fn read_text(path: &Path) -> Result<Option<String>, SettingsError> {
    match fs::read_to_string(path) {
        Ok(text) => Ok(Some(if text.contains('\r') {
            text.replace("\r\n", "\n").replace('\r', "\n")
        } else {
            text
        })),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(SettingsError::invalid(format!(
            "Cannot read {}: {}",
            path.display(),
            os_message(&error)
        ))),
    }
}

fn os_message(error: &io::Error) -> String {
    let message = error.to_string();
    message
        .split_once(" (os error ")
        .map_or(message.clone(), |(text, _)| text.to_owned())
}

// Report positions without quoting input lines, which can contain credentials.
fn parse_error(
    path: &Path,
    input: &str,
    error: &TomlError,
    editing: bool,
) -> SettingsError {
    let offset = error.span().map_or(input.len(), |span| span.start);
    let before = input.get(..offset).unwrap_or(input);
    let line = before.bytes().filter(|&c| c == b'\n').count() + 1;
    let column = before.rsplit('\n').next().unwrap_or("").chars().count() + 1;
    let message = error.message();
    let location = if offset >= input.len() {
        "at end of document".into()
    } else {
        format!("at line {line}, column {column}")
    };
    let detail = if editing {
        if offset >= input.len() {
            "Unexpected end of file".into()
        } else {
            let character = input
                .get(offset..)
                .and_then(|tail| tail.chars().next())
                .unwrap_or('\n');
            format!(
                "Unexpected character: {} at line {line} col {}",
                repr(&character.to_string()),
                column - 1
            )
        }
    } else {
        let description =
            if message.contains("table") && message.contains("\x60]\x60") {
                "Expected ']' at the end of a table declaration"
            } else if message.contains("duplicate") {
                "Cannot overwrite a value"
            } else if message.contains("invalid key")
                || message.contains("key/value pair")
            {
                "Expected '=' after a key in a key/value pair"
            } else {
                "Invalid value"
            };
        format!("{description} ({location})")
    };
    SettingsError::invalid(format!("Cannot parse {}: {detail}", path.display()))
}

pub use edit::{set, unset};
