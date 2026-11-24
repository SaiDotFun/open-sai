//! Keeper GraphQL requests and their transport verdicts.

mod queries;

use std::{
    path::Path,
    pin::pin,
    sync::Arc,
    time::{Duration, Instant},
};

use bech32::{Bech32, primitives::decode::CheckedHrpstring};
use reqwest::{Client, header};
use serde_json::{Map, Value, json};
use tokio::{sync::Notify, time};
use tower::util::MapResponseLayer;

use crate::{
    endpoint::Endpoint,
    errors::{Code, SaiError},
    log::{self, EventContext, Field, Level},
};

pub use queries::*;

pub const TIMEOUT: Duration = Duration::from_secs(20);
pub const ABSENT_AFTER_SECONDS: f64 = 300.0;

pub struct Keeper<'a> {
    endpoint: &'a str,
    events: EventContext<'a>,
}

#[derive(Default)]
struct ResponseLog {
    http_status: Option<u16>,
    errors: Option<Field>,
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
        let started = Instant::now();
        let token = token.filter(|token| !token.is_empty());
        if let Some(token) = token {
            log::remember([token.to_owned()]);
        }
        let addr = ["trader", "user", "address"]
            .into_iter()
            .filter_map(|key| variables.get(key).and_then(Value::as_str))
            .find(|value| is_evm_address(value));
        let mut response = ResponseLog::default();
        let result = self
            .post(query, &variables, token, timeout, &mut response)
            .await;
        let duration = (started.elapsed().as_secs_f64() * 1_000.0).round_ties_even() as u64;
        let (status, level, code, error) = match &result {
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
        self.events.event(
            "keeper.request",
            level,
            [
                ("operation", json!(queries::operation(query)).into()),
                ("endpoint", Field::Endpoint(self.endpoint.into())),
                ("addr", json!(addr).into()),
                ("authenticated", json!(token.is_some()).into()),
                ("durationMs", json!(duration).into()),
                ("status", json!(status).into()),
                ("code", json!(code).into()),
                ("error", json!(error).into()),
                ("httpStatus", json!(response.http_status).into()),
                (
                    "keeperErrors",
                    response.errors.unwrap_or(Field::Scalar(Value::Null)),
                ),
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
        let connected = Arc::new(Notify::new());
        let on_connect = Arc::clone(&connected);
        let client = Client::builder()
            .http1_only()
            .http1_title_case_headers()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .connect_timeout(timeout)
            .connector_layer(MapResponseLayer::new(move |connection| {
                on_connect.notify_one();
                connection
            }))
            .user_agent("python-httpx/0.28.1")
            .build()
            .map_err(transport_error)?;
        let mut request = client
            .post(self.endpoint)
            .header(header::ACCEPT, "*/*")
            .header(header::ACCEPT_ENCODING, "gzip, deflate")
            .header(header::CONNECTION, "keep-alive")
            .json(&json!({"query": query, "variables": variables}));
        if let Some(token) = token {
            request = request.bearer_auth(token);
        }
        // reqwest's read timer starts before the connector runs. Start waiting
        // for a response once TCP and TLS have finished so a stalled connect
        // goes through the connector's NotSent classification.
        let mut send = pin!(request.send());
        let mut response = tokio::select! {
            biased;
            result = &mut send => result,
            () = connected.notified() => time::timeout(timeout, send).await.map_err(|_| request_failed())?,
        }.map_err(transport_error)?;
        let status = response.status();
        let mut bytes = Vec::new();
        while let Some(chunk) = time::timeout(timeout, response.chunk())
            .await
            .map_err(|_| request_failed())?
            .map_err(transport_error)?
        {
            bytes.extend_from_slice(&chunk);
        }
        log.http_status = Some(status.as_u16());
        if !status.is_success() {
            return Err(request_failed());
        }
        let result: Value = serde_json::from_slice(&bytes).map_err(|_| request_failed())?;
        let Value::Object(mut result) = result else {
            return Err(keeper_error("Keeper returned an invalid response"));
        };
        if let Some(errors) = result.get("errors").filter(|value| truthy(value)) {
            log.errors = Some(graphql_errors(errors));
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
    let Some(url) = Endpoint::split(endpoint).filter(|url| {
        url.scheme == "https"
            || (url.scheme == "http"
                && matches!(
                    url.host_port().0.as_str(),
                    "localhost" | "127.0.0.1" | "[::1]"
                ))
    }) else {
        return Err(keeper_error(
            "Keeper endpoint must use HTTPS, except on loopback",
        ));
    };
    if let Some((userinfo, _)) = url.authority.rsplit_once('@')
        && !matches!(userinfo, "" | ":")
    {
        return Err(keeper_error(
            "Credentials are not allowed in the Keeper URL",
        ));
    }
    Ok(())
}

fn keeper_error(message: &str) -> SaiError {
    SaiError::new(Code::KeeperError, message)
}

fn request_failed() -> SaiError {
    keeper_error("Keeper request failed or returned invalid JSON")
}

fn transport_error(error: reqwest::Error) -> SaiError {
    if error.is_connect() {
        SaiError::new(
            Code::NotSent,
            "Could not connect to Keeper; the request was not sent",
        )
    } else {
        request_failed()
    }
}

fn graphql_errors(errors: &Value) -> Field {
    let entries = errors.as_array().into_iter().flatten().take(3);
    Field::Array(
        entries
            .filter_map(Value::as_object)
            .map(|item| {
                let code = item.get("extensions").and_then(|value| value.get("code"));
                let code = code.filter(|value| !value.is_null()).map(stringify);
                let message = item.get("message").map_or_else(String::new, stringify);
                Field::Object(vec![
                    (
                        "code".into(),
                        code.map_or(Field::Scalar(Value::Null), Field::Untrusted),
                    ),
                    ("message".into(), Field::Untrusted(message)),
                ])
            })
            .collect(),
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

/// Render the error fields using PR 69's public intent record policy.
/// Ledger reconciliation must read the original observation first.
pub fn scrub_intent_errors(home: &Path, data: &mut Map<String, Value>) {
    let Some(rows) = data.get_mut("userIntents").and_then(Value::as_array_mut) else {
        return;
    };
    let scrub = log::scrubber(home);
    for row in rows.iter_mut().filter_map(Value::as_object_mut) {
        let code = row.get("errorCode");
        if !code.is_some_and(truthy)
            && !row.get("errorMsg").is_some_and(truthy)
            && !matches!(
                row.get("lifecycleStep").and_then(Value::as_str),
                Some("jwt_failed" | "sign_reject" | "sign_failed" | "exec_req_fail" | "exec_fail")
            )
        {
            continue;
        }
        let code = match code.and_then(Value::as_str) {
            Some(
                code @ ("SLIPPAGE"
                | "simulation_failed"
                | "admission_expired"
                | "broadcast_rejected"
                | "chain_execution_failed"
                | "market_order_expired"
                | "market_order_fill_failed"
                | "market_order_trading_paused"
                | "market_order_market_closed"
                | "market_order_trade_not_open"
                | "market_order_outcome_not_indexed"
                | "intent_already_used"
                | "below_minimum"
                | "insufficient_idle"),
            ) => json!(code),
            _ => json!(Code::KeeperError),
        };
        row.insert("errorCode".into(), scrub(code.into()));
        row.insert(
            "errorMsg".into(),
            scrub(Field::Untrusted("Keeper reported an intent error.".into())),
        );
    }
}

fn is_evm_address(value: &str) -> bool {
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
