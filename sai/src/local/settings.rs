//! Local command settings adapter. Replace it with the shared C2 resolver.

use std::{borrow::Cow, env, fs, io, net::Ipv6Addr, num::IntErrorKind, path::Path};

use toml_edit::{Document, TomlError};
use toml_parser::{
    Source,
    decoder::ScalarKind,
    parser::{Event, EventKind, RecursionGuard, parse_document},
};
use unicode_normalization::UnicodeNormalization;

const MAINNET_HTTP: &str = "https://sai-keeper.nibiru.fi/query";

pub struct SettingsFlags<'a> {
    pub endpoint: Option<&'a str>,
    pub ws_endpoint: Option<&'a str>,
    pub lcd_endpoint: Option<&'a str>,
}

struct UrlSetting {
    key: &'static str,
    flag: &'static str,
    env: &'static str,
    schemes: [&'static str; 2],
}

const URL_SETTINGS: [UrlSetting; 3] = [
    UrlSetting {
        key: "keeper.endpoint",
        flag: "--endpoint",
        env: "SAI_ENDPOINT",
        schemes: ["https", "http"],
    },
    UrlSetting {
        key: "keeper.ws_endpoint",
        flag: "--ws-endpoint",
        env: "SAI_WS_ENDPOINT",
        schemes: ["wss", "ws"],
    },
    UrlSetting {
        key: "lcd.endpoint",
        flag: "--lcd-endpoint",
        env: "SAI_LCD_ENDPOINT",
        schemes: ["https", "http"],
    },
];

impl UrlSetting {
    fn validate(&self, url: &str) -> Result<(), String> {
        let normalized = url
            .trim_start_matches(|c| c <= ' ')
            .replace(['\t', '\n', '\r'], "");
        if let Some((scheme, rest)) = normalized.split_once("://")
            && self
                .schemes
                .iter()
                .any(|candidate| scheme.eq_ignore_ascii_case(candidate))
            && let Some(authority) = rest.split(['/', '?', '#']).next()
            && valid_authority(authority)
        {
            return Ok(());
        }
        let [secure, plain] = self.schemes;
        Err(format!(
            "Invalid value for {}: expected a URL starting with {secure}:// or {plain}://",
            self.key
        ))
    }
}

fn valid_authority(authority: &str) -> bool {
    if authority.is_empty()
        || (!authority.is_ascii()
            && authority
                .chars()
                .filter(|c| !matches!(c, '@' | ':' | '#' | '?'))
                .nfkc()
                .any(|c| "/?#@:".contains(c)))
    {
        return false;
    }
    if !authority.contains(['[', ']']) {
        return true;
    }
    let host_port = authority.rsplit('@').next().unwrap_or_default();
    let Some(bracketed) = host_port.strip_prefix('[') else {
        return false;
    };
    let Some((host, suffix)) = bracketed.split_once(']') else {
        return false;
    };
    if !suffix.is_empty() && !suffix.starts_with(':') {
        return false;
    }
    let address = match host.split_once('%') {
        Some((_, "")) => return false,
        Some((address, _)) => address,
        None => host,
    };
    address.parse::<Ipv6Addr>().is_ok()
        || host
            .strip_prefix('v')
            .and_then(|host| host.split_once('.'))
            .is_some_and(|(version, rest)| {
                !version.is_empty()
                    && version.bytes().all(|c| c.is_ascii_hexdigit())
                    && !rest.is_empty()
            })
}

fn parse_for_validation(text: &str) -> Result<Document<Cow<'_, str>>, TomlError> {
    let original_error = match Document::parse(Cow::Borrowed(text)) {
        Ok(document) => return Ok(document),
        Err(error) => error,
    };
    let source = Source::new(text);
    let mut replacements = Vec::new();
    let mut collect = |event: Event| {
        if event.kind() != EventKind::Scalar || event.encoding().is_some() {
            return;
        }
        let Some(raw) = source.get(event) else { return };
        let mut decoded = Cow::Borrowed("");
        let mut error = None;
        if let ScalarKind::Integer(radix) = raw.decode_scalar(&mut decoded, &mut error)
            && error.is_none()
            && let Err(error) = i64::from_str_radix(&decoded, radix.value())
        {
            // The validator only needs the integer's type and sign.
            let replacement = match error.kind() {
                IntErrorKind::PosOverflow => "1",
                IntErrorKind::NegOverflow => "-1",
                _ => return,
            };
            replacements.push((event.span(), replacement));
        }
    };
    let mut error = None;
    let mut receiver = RecursionGuard::new(&mut collect, 80);
    parse_document(&source.lex().into_vec(), &mut receiver, &mut error);
    if error.is_some() || replacements.is_empty() {
        return Err(original_error);
    }
    let mut masked = text.to_owned();
    for (span, replacement) in replacements.into_iter().rev() {
        masked.replace_range(span.start()..span.end(), replacement);
    }
    Document::parse(Cow::Owned(masked))
}

fn env_value(name: &str) -> Option<String> {
    env::var(name).ok().filter(|raw| !raw.trim().is_empty())
}

fn boolean(raw: &str) -> Option<bool> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => Some(true),
        "false" | "0" | "no" | "off" => Some(false),
        _ => None,
    }
}

fn positive_integer(raw: &str) -> bool {
    let raw = raw.trim();
    raw.bytes().all(|c| c.is_ascii_digit()) && raw.bytes().any(|c| c != b'0')
}

pub(super) fn keeper_endpoint(path: &Path, flags: SettingsFlags<'_>) -> Result<String, String> {
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == io::ErrorKind::NotFound => String::new(),
        Err(error) => return Err(format!("Cannot read {}: {error}", path.display())),
    };
    let document = parse_for_validation(&text)
        .map_err(|_| format!("Cannot parse {} as TOML", path.display()))?;

    let mut configured_endpoint = None;
    let mut logs_enabled = None;
    let mut warnings = Vec::new();
    let path = path.display();
    for (section, item) in document.iter() {
        let Some(table) = item.as_table_like() else {
            let message = format!("{path}: '{section}' must be a table, such as [{section}]");
            match section {
                "keeper" | "lcd" | "update" => return Err(message),
                "logs" => warnings.push(format!("{message}; logging is off until it is fixed")),
                _ => warnings.push(format!("{path}: ignoring unknown setting '{section}'")),
            }
            continue;
        };
        for (name, value) in table.iter() {
            let key = format!("{section}.{name}");
            if let Some(spec) = URL_SETTINGS.iter().find(|spec| spec.key == key) {
                let url = value.as_str().ok_or_else(|| {
                    format!("{path}: Invalid value for {key}: expected a URL string")
                })?;
                spec.validate(url)
                    .map_err(|error| format!("{path}: {error}"))?;
                if key == "keeper.endpoint" {
                    configured_endpoint = Some(url);
                }
                continue;
            }
            match key.as_str() {
                "update.auto" if value.as_bool().is_none() => {
                    return Err(format!(
                        "{path}: Invalid value for {key}: expected true or false"
                    ));
                }
                "update.auto" => {}
                "logs.enabled" => {
                    logs_enabled = value.as_bool();
                    if logs_enabled.is_none() {
                        warnings.push(format!(
                            "{path}: Invalid value for {key}: expected true or false; logging is off until it is fixed"
                        ));
                    }
                }
                "logs.keep_days" => {
                    if value.as_integer().is_none_or(|days| days < 1) {
                        warnings.push(format!(
                            "{path}: Invalid value for {key}: expected a whole number, 1 or more; logging is off until it is fixed"
                        ));
                    }
                }
                _ => warnings.push(format!("{path}: ignoring unknown setting '{key}'")),
            }
        }
    }

    let mut endpoint = configured_endpoint.unwrap_or(MAINNET_HTTP).to_owned();
    let passed = [flags.endpoint, flags.ws_endpoint, flags.lcd_endpoint];
    for (spec, flag) in URL_SETTINGS.iter().zip(passed) {
        let from_env = env_value(spec.env);
        if let Some((source, value)) = flag
            .map(|raw| (spec.flag, raw))
            .or_else(|| from_env.as_deref().map(|raw| (spec.env, raw)))
        {
            let value = value.trim();
            spec.validate(value)
                .map_err(|error| format!("{source}: {error}"))?;
            if spec.key == "keeper.endpoint" {
                endpoint = value.to_owned();
            }
        }
    }
    if env_value("SAI_UPDATE_AUTO").is_some_and(|raw| boolean(&raw).is_none()) {
        return Err(
            "SAI_UPDATE_AUTO: Invalid value for update.auto: expected true, false, yes, no, on, off, 1, or 0"
                .into(),
        );
    }

    if let Some(raw) = env_value("SAI_LOG") {
        logs_enabled = boolean(&raw);
        if logs_enabled.is_none() {
            warnings.push(
                "SAI_LOG: invalid value for logs.enabled; logging is off until it is fixed".into(),
            );
        }
    }
    if logs_enabled != Some(false)
        && env_value("SAI_LOG_KEEP_DAYS").is_some_and(|raw| !positive_integer(&raw))
    {
        warnings.push(
            "SAI_LOG_KEEP_DAYS: invalid value for logs.keep_days; logging is off until it is fixed"
                .into(),
        );
    }
    for warning in warnings {
        eprintln!("{warning}");
    }
    Ok(endpoint)
}
