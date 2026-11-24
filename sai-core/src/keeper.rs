//! Keeper GraphQL requests and their transport verdicts.

mod ca_dir;
mod diagnostics;
mod proxy;
mod public;
mod queries;
pub mod stream;
mod transport;

use std::time::{Duration, Instant};

use bech32::{Bech32, primitives::decode::CheckedHrpstring};
use serde_json::{Map, Value, json};

use crate::{
    endpoint::Endpoint,
    errors::{Code, SaiError},
    log::{self, EventContext, Field, Level},
};

pub use diagnostics::{lcd_network, websocket_handshake};
pub(crate) use public::{
    intent_history, intent_log_fields, intent_record, known_intent_field, log_intent_record,
    request_hash, wait_error_code,
};
pub use public::{public_account_history, public_intent_status};
pub use queries::*;
pub(crate) use transport::get as lcd_get;

pub const TIMEOUT: Duration = Duration::from_secs(20);
pub const ABSENT_AFTER_SECONDS: f64 = 300.0;

pub struct Keeper<'a> {
    endpoint: &'a str,
    events: EventContext<'a>,
}

#[derive(Default)]
struct ResponseLog {
    http_status: Option<u16>,
    errors: Option<(Value, Value)>,
}

impl<'a> Keeper<'a> {
    pub fn new(endpoint: &'a str, events: EventContext<'a>) -> Self {
        Self { endpoint, events }
    }

    pub fn endpoint(&self) -> &str {
        self.endpoint
    }

    /// Send one GraphQL POST. The caller decides if a subsequent read is warranted.
    pub async fn request(
        &self,
        query: &str,
        variables: Map<String, Value>,
        token: Option<&str>,
        timeout: Duration,
    ) -> Result<Map<String, Value>, SaiError> {
        self.request_with(
            query,
            variables,
            token,
            timeout,
            std::future::pending(),
            false,
        )
        .await
    }

    /// Make an interruptible public read.
    pub async fn read_until(
        &self,
        query: &str,
        variables: Map<String, Value>,
        timeout: Duration,
        interrupt: impl Future<Output = SaiError>,
    ) -> Result<Map<String, Value>, SaiError> {
        self.request_with(query, variables, None, timeout, interrupt, false)
            .await
    }

    /// Bound the entire request for an intent wait. Status reads use socket inactivity timeouts.
    pub(crate) async fn wait_request(
        &self,
        query: &str,
        variables: Map<String, Value>,
        timeout: Duration,
    ) -> Result<Map<String, Value>, SaiError> {
        self.request_with(
            query,
            variables,
            None,
            timeout,
            std::future::pending(),
            true,
        )
        .await
    }

    async fn request_with(
        &self,
        query: &str,
        variables: Map<String, Value>,
        token: Option<&str>,
        timeout: Duration,
        interrupt: impl Future<Output = SaiError>,
        bounded: bool,
    ) -> Result<Map<String, Value>, SaiError> {
        let started = Instant::now();
        let token = token.filter(|token| !token.is_empty());
        if let Some(token) = token {
            log::remember([token.to_owned()]);
        }
        let addr = ["trader", "user", "address"]
            .into_iter()
            .filter_map(|key| variables.get(key).and_then(Value::as_str))
            .find(|value| is_evm_address(value));
        let intent = if matches!(query, INTENTS | WAIT_INTENTS) {
            variables
                .get("key")
                .and_then(Value::as_str)
                .filter(|key| log::valid_identifier(key))
        } else {
            None
        };
        let mut response = ResponseLog::default();
        let post = async {
            let post = self.post(query, &variables, token, timeout, &mut response);
            if bounded {
                tokio::time::timeout(timeout, post)
                    .await
                    .unwrap_or_else(|_| Err(keeper_error("Keeper read deadline expired")))
            } else {
                post.await
            }
        };
        let (result, interrupted) = tokio::select! {
            biased;
            error = interrupt => (Err(error), true),
            result = post => (result, false),
        };
        let duration = (started.elapsed().as_secs_f64() * 1_000.0).round_ties_even() as u64;
        let (status, level, code, error) = match &result {
            Err(_) if interrupted => ("error", Level::Warn, Some(Code::InternalError), None),
            Ok(_) => ("ok", Level::Info, None, None),
            Err(error) => match error.code {
                Code::NotSent => ("not_sent", Level::Warn, Some(error.code), None),
                Code::AdmissionRejected => ("rejected", Level::Warn, Some(error.code), None),
                code => (
                    "error",
                    Level::Warn,
                    Some(if query == SUBMIT {
                        Code::OutcomeUnknown
                    } else {
                        code
                    }),
                    Some(error.message.clone()),
                ),
            },
        };
        let (errors, count) = response.errors.unwrap_or_default();
        self.events.event(
            "keeper.request",
            level,
            [
                ("operation", json!(queries::operation(query)).into()),
                ("endpoint", Field::Endpoint(self.endpoint.into())),
                ("addr", json!(addr).into()),
                ("intentKey", json!(intent).into()),
                ("authenticated", json!(token.is_some()).into()),
                ("durationMs", json!(duration).into()),
                ("status", json!(status).into()),
                ("code", json!(code).into()),
                ("error", json!(error).into()),
                ("httpStatus", json!(response.http_status).into()),
                ("keeperErrors", errors.into()),
                ("keeperErrorCount", count.into()),
            ],
        );
        result
    }

    async fn post(
        &self,
        query: &str,
        variables: &Map<String, Value>,
        token: Option<&str>,
        timeout: Duration,
        log: &mut ResponseLog,
    ) -> Result<Map<String, Value>, SaiError> {
        check_url(self.endpoint)?;
        let body = serde_json::to_vec(&json!({"query": query, "variables": variables}))
            .map_err(|_| request_failed())?;
        let (status, bytes) = transport::post(self.endpoint, body, token, timeout).await?;
        log.http_status = Some(status.as_u16());
        if !status.is_success() {
            return Err(request_failed());
        }
        let result: Value = serde_json::from_slice(&bytes).map_err(|_| request_failed())?;
        let Value::Object(mut result) = result else {
            return Err(keeper_error("Keeper returned an invalid response"));
        };
        if let Some(errors) = result.get("errors").filter(|value| truthy(value)) {
            log.errors = Some(graphql_error_fields(errors, false, None));
            if query == SUBMIT
                && !result.get("data").is_some_and(truthy)
                && errors.as_array().is_some_and(|items| {
                    items
                        .iter()
                        .all(|item| item["extensions"]["code"] == "INTENT_NOT_ACCEPTED")
                })
            {
                return Err(SaiError::new(
                    Code::AdmissionRejected,
                    "Keeper rejected admission; check inputs and session",
                ));
            }
            return Err(keeper_error(
                "Keeper rejected the request; check inputs and session",
            ));
        }
        match result.remove("data") {
            Some(Value::Object(data)) => Ok(data),
            _ => Err(keeper_error("Keeper returned no data")),
        }
    }
}

fn check_url(endpoint: &str) -> Result<(), SaiError> {
    check_transport(endpoint, ProbeEndpoint::Keeper).map_err(keeper_error)
}

enum ProbeEndpoint {
    Keeper,
    Lcd,
    WebSocket,
}

fn check_transport(endpoint: &str, service: ProbeEndpoint) -> Result<(), &'static str> {
    let (secure, plain, insecure, credentials) = match service {
        ProbeEndpoint::Keeper => (
            "https",
            "http",
            "Keeper endpoint must use HTTPS, except on loopback",
            "Credentials are not allowed in the Keeper URL",
        ),
        ProbeEndpoint::Lcd => (
            "https",
            "http",
            "LCD endpoint must use HTTPS, except on loopback",
            "Credentials are not allowed in the LCD URL",
        ),
        ProbeEndpoint::WebSocket => (
            "wss",
            "ws",
            "WebSocket endpoint must use WSS, except on loopback",
            "Credentials are not allowed in the WebSocket URL",
        ),
    };
    let Some(url) = Endpoint::split(endpoint).filter(|url| {
        url.scheme == secure
            || (url.scheme == plain
                && matches!(
                    url.host_port().0.as_str(),
                    "localhost" | "127.0.0.1" | "[::1]"
                ))
    }) else {
        return Err(insecure);
    };
    if let Some((userinfo, _)) = url.authority.rsplit_once('@')
        && !matches!(userinfo, "" | ":")
    {
        return Err(credentials);
    }
    Ok(())
}

fn keeper_error(message: &str) -> SaiError {
    SaiError::new(Code::KeeperError, message)
}

fn request_failed() -> SaiError {
    keeper_error("Keeper request failed or returned invalid JSON")
}

fn not_sent() -> SaiError {
    SaiError::new(
        Code::NotSent,
        "Could not connect to Keeper; the request was not sent",
    )
}

pub(crate) fn stringify(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        _ => value.to_string(),
    }
}

pub(crate) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

pub(crate) fn graphql_error_fields(
    errors: &Value,
    retained: bool,
    total: Option<&Value>,
) -> (Value, Value) {
    let codes: Vec<_> = errors
        .as_array()
        .into_iter()
        .flatten()
        .take(3)
        .filter(|item| item.is_object())
        .map(|item| {
            let field = if retained { item } else { &item["extensions"] };
            let code = field["code"]
                .as_str()
                .filter(|code| {
                    matches!(
                        *code,
                        "GRAPHQL_PARSE_FAILED"
                            | "GRAPHQL_VALIDATION_FAILED"
                            | "INTENT_NOT_ACCEPTED"
                            | "BAD_USER_INPUT"
                            | "FORBIDDEN"
                            | "INTERNAL_SERVER_ERROR"
                            | "UNAUTHENTICATED"
                    )
                })
                .unwrap_or("KEEPER_ERROR");
            json!({"code": code})
        })
        .collect();
    let count = errors.as_array().map_or(1, Vec::len) as u64;
    let total = total.and_then(Value::as_u64).unwrap_or(count).max(count);
    (json!(codes), json!(total))
}

pub(crate) fn is_evm_address(value: &str) -> bool {
    value
        .strip_prefix("0x")
        .is_some_and(|hex| hex.len() == 40 && hex.bytes().all(|byte| byte.is_ascii_hexdigit()))
}

/// Canonicalize a 20-byte EVM or checksummed Nibiru account for ledger matching.
pub fn canonical_account(account: &str) -> Option<String> {
    if !account.is_ascii() {
        return None;
    }
    let lower = account.to_ascii_lowercase();
    let hex = lower.strip_prefix("0x").unwrap_or(&lower);
    if hex.len() == 40 && hex.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Some(format!("0x{hex}"));
    }
    if lower.len() != 43 || !lower.starts_with("nibi1") {
        return None;
    }
    let checked = CheckedHrpstring::new::<Bech32>(&lower).ok()?;
    use std::fmt::Write as _;
    let mut evm = String::from("0x");
    for byte in checked.byte_iter() {
        write!(evm, "{byte:02x}").expect("writing to a string");
    }
    Some(evm)
}
