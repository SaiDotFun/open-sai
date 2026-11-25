use std::{borrow::Cow, sync::LazyLock};

use regex::{Captures, Regex};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use url::Host;

use crate::endpoint::Endpoint;

pub const REDACTED: &str = "[redacted session token]";
const REDACTED_HEX: &str = "[redacted hex]";
const REDACTED_URL: &str = "[redacted url]";
pub(super) const REDACTED_INPUT: &str = "[redacted]";
const CUT: &str = "[cut]";
const MAX_TEXT: usize = 2000;
const MAX_UNTRUSTED: usize = 300;
const MAX_DEPTH: usize = 6;
const MAX_ITEMS: usize = 64;
pub const EVENT_CHARS: usize = 12 * 1024;
const EVENT_NODES: usize = 512;

static JWT: LazyLock<Regex> =
    LazyLock::new(|| regex(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"));
static URL: LazyLock<Regex> = LazyLock::new(|| regex(r"[A-Za-z][A-Za-z0-9+.-]*://\S*"));
static URL_REST: LazyLock<Regex> = LazyLock::new(|| regex(r"(?s)[A-Za-z][A-Za-z0-9+.-]*://.*"));
static HOST: LazyLock<Regex> =
    LazyLock::new(|| regex(r"\A(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])\z"));
static LONG_HEX: LazyLock<Regex> = LazyLock::new(|| regex(r"(?:0x)?[0-9A-Fa-f]{100,}"));
static HEX: LazyLock<Regex> = LazyLock::new(|| regex(r"(?:0x)?[0-9A-Fa-f]{64,}"));

fn regex(pattern: &str) -> Regex {
    Regex::new(pattern).expect("valid log redaction pattern")
}

fn numeric_argument(value: &str) -> bool {
    value.parse::<f64>().is_ok_and(f64::is_finite)
}

fn safe_option_value(flag: &str, value: &str) -> bool {
    match flag {
        "--surface" => matches!(value, "cli" | "mcp" | "update"),
        "--scope" => matches!(value, "user" | "project" | "local"),
        "--client" => matches!(value, "claude" | "codex" | "cursor"),
        "--side" => matches!(value, "long" | "short"),
        "--type" => matches!(value, "market" | "limit" | "stop"),
        "-n" | "--market" | "--amount" | "--leverage" | "--price" | "--slippage"
        | "--take-profit" | "--stop-loss" => numeric_argument(value),
        _ => false,
    }
}

fn value_option(flag: &str) -> bool {
    matches!(
        flag,
        "--endpoint"
            | "--ws-endpoint"
            | "--lcd-endpoint"
            | "--as"
            | "--addr"
            | "--key"
            | "--page"
            | "--dir"
            | "--before"
            | "--since"
            | "--event"
            | "--surface"
            | "-n"
            | "--scope"
            | "--client"
            | "--server"
            | "--side"
            | "--type"
            | "--market"
            | "--amount"
            | "--leverage"
            | "--price"
            | "--slippage"
            | "--take-profit"
            | "--stop-loss"
    )
}

fn switch_option(flag: &str) -> bool {
    matches!(
        flag,
        "--json"
            | "--help"
            | "-h"
            | "--version"
            | "-V"
            | "--skill"
            | "--offline"
            | "--force"
            | "--no-fix"
            | "--dry-run"
            | "--all"
            | "--reset-args"
            | "--cancel-order"
            | "--allow-concurrent-open"
            | "--clear-take-profit"
            | "--clear-stop-loss"
    )
}

fn known_command(group: &str, word: &str) -> bool {
    match group {
        "" => matches!(
            word,
            "account"
                | "auth"
                | "config"
                | "doctor"
                | "intent"
                | "logs"
                | "market"
                | "mcp"
                | "skill"
                | "trade"
                | "watch"
        ),
        "account" => matches!(word, "positions" | "history" | "balances" | "overview"),
        "auth" => matches!(
            word,
            "login" | "import" | "renew" | "status" | "list" | "use" | "logout"
        ),
        "config" => matches!(word, "get" | "set" | "unset" | "list"),
        "intent" => matches!(word, "local" | "status" | "wait"),
        "logs" => matches!(word, "path" | "tail" | "intent" | "run" | "stats"),
        "market" => matches!(word, "list" | "prices"),
        "mcp" => matches!(word, "list" | "install" | "serve"),
        "skill" => matches!(word, "show" | "source" | "install"),
        "trade" => matches!(word, "open" | "close" | "cancel" | "tpsl" | "leverage"),
        "watch" => matches!(word, "positions" | "balances" | "price"),
        _ => false,
    }
}

fn safe_argv(args: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut group = String::new();
    let mut command = String::new();
    let mut position = 0;
    let mut pending = None;
    let mut opaque = false;
    args.into_iter()
        .take(MAX_ITEMS)
        .map(|arg| {
            let (flag, attached) = arg
                .split_once('=')
                .map_or((arg.as_str(), None), |(flag, value)| (flag, Some(value)));
            let value_flag = value_option(flag);
            let previous = pending.take();
            if opaque {
                return REDACTED_INPUT.into();
            }
            if arg == "--" {
                opaque = true;
                return arg;
            }
            if value_flag || switch_option(flag) {
                if let Some(value) = attached {
                    return if safe_option_value(flag, value) {
                        arg
                    } else {
                        format!("{flag}={REDACTED_INPUT}")
                    };
                }
                pending = value_flag.then(|| flag.to_owned());
                return arg;
            }
            if let Some(flag) = previous {
                return if safe_option_value(&flag, &arg) {
                    arg
                } else {
                    REDACTED_INPUT.into()
                };
            }
            if position == 0 && command.is_empty() && known_command(&group, &arg) {
                if group.is_empty() {
                    group.clone_from(&arg);
                } else {
                    command.clone_from(&arg);
                }
                return arg;
            }
            let safe = position == 0
                && match (group.as_str(), command.as_str()) {
                    ("trade", "close" | "cancel" | "tpsl" | "leverage") | ("watch", "price") => {
                        numeric_argument(&arg)
                    }
                    ("config", "get" | "set" | "unset") => {
                        crate::settings::Key::parse(&arg).is_ok()
                    }
                    _ => false,
                };
            position += 1;
            if safe { arg } else { REDACTED_INPUT.into() }
        })
        .collect()
}

fn endpoint_field(key: &str) -> bool {
    matches!(key, "endpoint" | "keeper" | "websocket" | "lcd")
}

fn public_endpoint(value: &str) -> String {
    let site = origin(value);
    if site == REDACTED_URL {
        REDACTED_INPUT.into()
    } else {
        site
    }
}

/// A log field with its text redaction policy.
pub enum Field {
    Text(String),
    Untrusted(String),
    Endpoint(String),
    EndpointIdentity([u8; 32]),
    Array(Vec<Field>),
    Object(Vec<(String, Field)>),
    Scalar(Value),
}

impl From<Value> for Field {
    fn from(value: Value) -> Self {
        match value {
            Value::String(value) => Self::Text(value),
            Value::Array(items) => Self::Array(items.into_iter().map(Self::from).collect()),
            Value::Object(items) => Self::Object(
                items
                    .into_iter()
                    .map(|(key, value)| (key, Self::from(value)))
                    .collect(),
            ),
            value => Self::Scalar(value),
        }
    }
}

impl Field {
    pub fn redacted_argument() -> Self {
        Self::Text(REDACTED_INPUT.into())
    }

    pub fn argv(args: impl IntoIterator<Item = String>) -> Self {
        Self::Array(args.into_iter().take(MAX_ITEMS).map(Self::Text).collect())
    }

    pub fn endpoint_identity(value: &str) -> Self {
        endpoint_digest(value).map_or(Self::Scalar(Value::Null), Self::EndpointIdentity)
    }

    pub(super) fn snapshot(self, budget: &mut Budget, tokens: &[String], depth: usize) -> Self {
        budget.chars = budget.chars.saturating_sub(16);
        budget.nodes = budget.nodes.saturating_sub(1);
        match self {
            Self::Text(text) => Self::Text(copy_text(&text, budget, tokens)),
            Self::Untrusted(text) => Self::Untrusted(copy_text(&text, budget, tokens)),
            Self::Endpoint(text) => Self::Endpoint(copy_text(&text, budget, tokens)),
            Self::EndpointIdentity(_) => self,
            Self::Scalar(Value::Number(number))
                if number.is_i64()
                    || number.is_u64()
                    || number.is_f64()
                    || number
                        .to_string()
                        .strip_prefix('-')
                        .is_some_and(|digits| digits.parse::<u64>().is_ok()) =>
            {
                Self::Scalar(Value::Number(number))
            }
            Self::Scalar(Value::Bool(_) | Value::Null) => self,
            Self::Object(items) if depth < MAX_DEPTH => {
                let count = items.len();
                let mut copied = Vec::new();
                for (index, (key, value)) in items.into_iter().enumerate() {
                    if index >= MAX_ITEMS || budget.full() {
                        copied.push((CUT.into(), Self::Scalar(Value::from(count - index))));
                        break;
                    }
                    budget.chars = budget.chars.saturating_sub(16);
                    budget.nodes = budget.nodes.saturating_sub(1);
                    let key = copy_text(prefix(&key, MAX_UNTRUSTED + 1), budget, tokens);
                    let item = if budget.full() {
                        Self::Text(CUT.into())
                    } else {
                        value
                            .private_field(&key)
                            .snapshot(budget, tokens, depth + 1)
                    };
                    copied.push((key, item));
                }
                Self::Object(copied)
            }
            Self::Array(items) if depth < MAX_DEPTH => {
                let count = items.len();
                let mut copied = Vec::new();
                for (index, value) in items.into_iter().enumerate() {
                    if index >= MAX_ITEMS || budget.full() {
                        copied.push(Self::Text(format!("{CUT} {} more items", count - index)));
                        break;
                    }
                    copied.push(value.snapshot(budget, tokens, depth + 1));
                }
                Self::Array(copied)
            }
            _ => Self::Text(CUT.into()),
        }
    }

    fn private_field(self, key: &str) -> Self {
        match (key, self) {
            ("argv", Self::Array(args)) => Self::Array(
                safe_argv(args.into_iter().map(|field| match field {
                    Self::Text(text) => text,
                    _ => REDACTED_INPUT.into(),
                }))
                .into_iter()
                .map(Self::Text)
                .collect(),
            ),
            ("argv" | "error" | "errorMsg" | "errorMessage", _) => {
                Self::Text(REDACTED_INPUT.into())
            }
            (key, field) if endpoint_field(key) => match field {
                Self::Text(text) | Self::Untrusted(text) | Self::Endpoint(text) => {
                    Self::Text(public_endpoint(&text))
                }
                Self::Object(_) => field,
                _ => Self::Text(REDACTED_INPUT.into()),
            },
            (_, field) => field,
        }
    }

    pub(super) fn clean(self, tokens: &[String]) -> Value {
        match self {
            Self::Text(text) => Value::String(scrub(&text, tokens, &URL, false)),
            Self::Untrusted(text) => Value::String(scrub(&text, tokens, &URL_REST, true)),
            Self::Endpoint(text) => Value::String(origin(&text)),
            Self::EndpointIdentity(_) => Value::Null,
            Self::Array(items) => {
                Value::Array(items.into_iter().map(|item| item.clean(tokens)).collect())
            }
            Self::Object(items) => Value::Object(
                items
                    .into_iter()
                    .map(|(key, value)| (scrub(&key, tokens, &URL, false), value.clean(tokens)))
                    .collect::<Map<_, _>>(),
            ),
            Self::Scalar(value) => value,
        }
    }
}

pub(super) struct Budget {
    chars: usize,
    nodes: usize,
}

impl Budget {
    pub fn new() -> Self {
        Self {
            chars: EVENT_CHARS,
            nodes: EVENT_NODES,
        }
    }

    fn full(&self) -> bool {
        self.chars == 0 || self.nodes == 0
    }

    pub fn used(&self) -> usize {
        EVENT_CHARS - self.chars
    }
}

pub(super) fn prefix(text: &str, limit: usize) -> &str {
    text.char_indices()
        .nth(limit)
        .map_or(text, |(byte, _)| &text[..byte])
}

fn copy_text(text: &str, budget: &mut Budget, tokens: &[String]) -> String {
    let head = prefix(text, MAX_TEXT + 4096);
    let whole = head.len() == text.len();
    let mut copied = replace_tokens(head, tokens);
    let count = copied.chars().count();
    if !whole || count > MAX_TEXT {
        let head = prefix(&copied, (MAX_TEXT - 64).min(count.saturating_sub(1)));
        copied = if let Some(index) = head.rfind([' ', '\n', '\t']) {
            format!(
                "{} {CUT} {} more characters",
                &head[..index],
                copied[index..].chars().count()
            )
        } else if URL.find(head).is_some_and(|found| found.start() == 0) {
            head.into()
        } else {
            format!("{CUT} {count} characters")
        };
    }
    budget.chars = budget.chars.saturating_sub(copied.chars().count());
    copied
}

pub(super) fn replace_tokens(text: &str, tokens: &[String]) -> String {
    let mut text = text.to_owned();
    for token in tokens {
        if text.contains(token.as_str()) {
            text = text.replace(token, REDACTED);
        }
    }
    text
}

pub(super) fn endpoint_digest(value: &str) -> Option<[u8; 32]> {
    let site = origin(value);
    if site == REDACTED_URL {
        return None;
    }
    let endpoint = Endpoint::split(value.trim())?;
    let path = if endpoint.path.is_empty() {
        "/"
    } else {
        &endpoint.path
    };
    Some(Sha256::digest(format!("{site}{path}?{}", endpoint.query)).into())
}

/// Strip credentials, path, query, and fragment for a public endpoint report.
pub fn origin(text: &str) -> String {
    let Some(url) = Endpoint::split(text.trim()) else {
        return REDACTED_URL.into();
    };
    let (mut host, port) = url.host_port();
    if !host.is_ascii() && !host.starts_with('[') {
        let Ok(encoded) = Host::parse(&host) else {
            return REDACTED_URL.into();
        };
        host = encoded.to_string();
    }
    if !HOST.is_match(&host) {
        return REDACTED_URL.into();
    }
    match port {
        Ok(Some(port)) => format!("{}://{host}:{port}", url.scheme),
        Ok(None) => format!("{}://{host}", url.scheme),
        Err(()) => REDACTED_URL.into(),
    }
}

fn redact_urls<'a>(text: &'a str, url: &Regex, untrusted: bool) -> Cow<'a, str> {
    url.replace_all(text, |matched: &Captures<'_>| {
        let origin = origin(&matched[0]);
        if untrusted {
            format!("{origin} {CUT}")
        } else {
            origin
        }
    })
}

fn redact_url_text(text: String, url: &Regex) -> String {
    match redact_urls(&text, url, false) {
        Cow::Borrowed(_) => text,
        Cow::Owned(redacted) => redacted,
    }
}

type McpArgumentSchema = fn(&str) -> Option<&'static Map<String, Value>>;

fn redact_retained_value(value: Value, mcp_argument_schema: McpArgumentSchema) -> Value {
    match value {
        Value::String(text) => Value::String(redact_url_text(text, &URL)),
        Value::Array(items) => Value::Array(
            items
                .into_iter()
                .map(|value| redact_retained_value(value, mcp_argument_schema))
                .collect(),
        ),
        Value::Object(mut row) => {
            redact_retained_urls(&mut row, mcp_argument_schema);
            Value::Object(row)
        }
        value => value,
    }
}

pub(super) fn redact_retained_urls(
    row: &mut Map<String, Value>,
    mcp_argument_schema: McpArgumentSchema,
) {
    let mcp_call = row.get("event").and_then(Value::as_str) == Some("mcp.tool_call");
    let properties = if mcp_call {
        row.get("tool")
            .and_then(Value::as_str)
            .and_then(mcp_argument_schema)
    } else {
        None
    };
    *row = std::mem::take(row)
        .into_iter()
        .map(|(key, value)| {
            let value = match (key.as_str(), value) {
                ("arguments", Value::Object(args)) if mcp_call => Value::Object(
                    args.into_iter()
                        .map(|(key, value)| {
                            if !properties.is_some_and(|known| known.contains_key(&key)) {
                                return (REDACTED_INPUT.into(), REDACTED_INPUT.into());
                            }
                            let value = match value {
                                Value::Null | Value::Bool(_) | Value::Number(_) => value,
                                _ => REDACTED_INPUT.into(),
                            };
                            (key, value)
                        })
                        .collect(),
                ),
                ("arguments", _) if mcp_call => REDACTED_INPUT.into(),
                ("argv", Value::Array(args)) => safe_argv(args.into_iter().map(|arg| match arg {
                    Value::String(text) => text,
                    _ => REDACTED_INPUT.into(),
                }))
                .into(),
                ("argv" | "error" | "errorMsg" | "errorMessage", _) => REDACTED_INPUT.into(),
                (key, Value::String(endpoint)) if endpoint_field(key) => {
                    public_endpoint(&endpoint).into()
                }
                (key, value) if endpoint_field(key) && !value.is_object() => REDACTED_INPUT.into(),
                (_, value) => redact_retained_value(value, mcp_argument_schema),
            };
            (redact_url_text(key, &URL), value)
        })
        .collect();
}

fn scrub(text: &str, tokens: &[String], url: &Regex, untrusted: bool) -> String {
    let text = replace_tokens(text, tokens);
    let text = JWT.replace_all(&text, REDACTED);
    let text = redact_urls(&text, url, untrusted);
    let (hex, limit) = if untrusted {
        (&*HEX, MAX_UNTRUSTED)
    } else {
        (&*LONG_HEX, MAX_TEXT)
    };
    let text = hex.replace_all(&text, REDACTED_HEX);
    let head = prefix(&text, limit);
    if head.len() == text.len() {
        text.into_owned()
    } else {
        format!(
            "{head}... [{} more characters]",
            text[head.len()..].chars().count()
        )
    }
}
