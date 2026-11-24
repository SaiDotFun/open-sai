use std::sync::LazyLock;

use regex::{Captures, Regex};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::endpoint::Endpoint;

const REDACTED: &str = "[redacted session token]";
const REDACTED_HEX: &str = "[redacted hex]";
const REDACTED_URL: &str = "[redacted url]";
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

/// A log field with its text redaction policy.
pub enum Field {
    Text(String),
    Untrusted(String),
    Arg(String),
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
    /// Keep only the intermediate digest until the writer keys it.
    pub fn endpoint_identity(endpoint: &str) -> Self {
        let site = origin(endpoint);
        if site != REDACTED_URL
            && let Some(url) = Endpoint::split(endpoint.trim())
        {
            let path = if url.path.is_empty() { "/" } else { &url.path };
            return Self::EndpointIdentity(
                Sha256::digest(format!("{site}{path}?{}", url.query)).into(),
            );
        }
        Self::Scalar(Value::Null)
    }

    pub fn argv(args: impl IntoIterator<Item = String>) -> Self {
        Self::Array(args.into_iter().take(MAX_ITEMS).map(Self::Arg).collect())
    }

    pub fn endpoint_identity(value: &str) -> Self {
        let site = origin(value);
        if site == REDACTED_URL {
            return Self::Scalar(Value::Null);
        }
        let Some(endpoint) = Endpoint::split(value.trim()) else {
            return Self::Scalar(Value::Null);
        };
        let path = if endpoint.path.is_empty() {
            "/"
        } else {
            &endpoint.path
        };
        let digest = Sha256::digest(format!("{site}{path}?{}", endpoint.query));
        Self::EndpointIdentity(digest.into())
    }

    pub(super) fn snapshot(self, budget: &mut Budget, tokens: &[String], depth: usize) -> Self {
        budget.chars = budget.chars.saturating_sub(16);
        budget.nodes = budget.nodes.saturating_sub(1);
        match self {
            Self::Text(text) => Self::Text(copy_text(&text, budget, tokens)),
            Self::Untrusted(text) => Self::Untrusted(copy_text(&text, budget, tokens)),
            Self::Endpoint(text) => Self::Endpoint(copy_text(&text, budget, tokens)),
            Self::EndpointIdentity(_) => self,
            Self::Arg(text) => Self::Arg(copy_text(&text, budget, tokens)),
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
                        value.snapshot(budget, tokens, depth + 1)
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

    pub(super) fn clean(self, tokens: &[String]) -> Value {
        match self {
            Self::Text(text) => Value::String(scrub(&text, tokens, &URL, false)),
            Self::Untrusted(text) => Value::String(scrub(&text, tokens, &URL_REST, true)),
            Self::Arg(text) => Value::String(scrub(&text, tokens, &URL_REST, false)),
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

fn origin(text: &str) -> String {
    let Some(url) = Endpoint::split(text.trim()) else {
        return REDACTED_URL.into();
    };
    let (host, port) = url.host_port();
    if !HOST.is_match(&host) {
        return REDACTED_URL.into();
    }
    match port {
        Ok(Some(port)) => format!("{}://{host}:{port}", url.scheme),
        Ok(None) => format!("{}://{host}", url.scheme),
        Err(()) => REDACTED_URL.into(),
    }
}

fn scrub(text: &str, tokens: &[String], url: &Regex, untrusted: bool) -> String {
    let text = replace_tokens(text, tokens);
    let text = JWT.replace_all(&text, REDACTED);
    let text = url.replace_all(&text, |matched: &Captures<'_>| {
        let origin = origin(&matched[0]);
        if untrusted {
            format!("{origin} {CUT}")
        } else {
            origin
        }
    });
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
