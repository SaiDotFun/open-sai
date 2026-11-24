//! Trade previews and one reservation and submission path for CLI and MCP.

mod actions;
mod confirmation;

use std::future::{Future, pending};

use chrono::{SecondsFormat, Utc};
use serde::Serialize;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::{
    errors::{Code, SaiError},
    keeper::{self, Keeper, SUBMIT},
    log::{EventContext, Field, Level},
    storage::{IntentStatus, Session, Storage, StorageError},
};

pub use actions::{MARKET_PRICE_NOTICE, OpenTrade, OrderType, Side, close_preview};
pub use confirmation::{Confirmation, digest_bytes, positive_decimal, preview_digest};

#[derive(Clone, Debug, Serialize)]
pub struct TradeIntent {
    kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    target: Option<TradeTarget>,
    body: Value,
}

#[derive(Clone, Debug, Serialize)]
struct TradeTarget {
    #[serde(rename = "userTradeId")]
    trade_id: String,
}

impl TradeIntent {
    pub fn new(kind: &'static str, trade_id: Option<String>, body: Value) -> Self {
        Self {
            kind,
            target: trade_id.map(|trade_id| TradeTarget { trade_id }),
            body,
        }
    }

    fn is_market_open(&self) -> bool {
        self.kind == "OPEN_TRADE" && self.body["orderType"] == "market"
    }
}

#[derive(Clone, Debug)]
pub struct TradePreview {
    pub intent: TradeIntent,
    pub reference_price: Option<String>,
    pub quote: Option<Value>,
    pub warning: Option<&'static str>,
}

impl TradePreview {
    pub fn new(intent: TradeIntent) -> Self {
        Self {
            intent,
            reference_price: None,
            quote: None,
            warning: None,
        }
    }

    pub fn output(&self, account: Option<&str>) -> Value {
        let mut output = json!({"addr": account, "dryRun": true});
        if self.intent.is_market_open() {
            output["requiresMarketPriceRefresh"] = json!(true);
        }
        if let Some(quote) = &self.quote {
            output["quote"] = quote.clone();
        }
        output["intent"] = json!(self.intent);
        if let Some(price) = &self.reference_price {
            output["referencePrice"] = json!(price);
        }
        if let Some(warning) = self.warning {
            output["warning"] = json!(warning);
        }
        output
    }

    pub fn confirmed_output(&self, account: Option<&str>) -> Value {
        let mut output = self.output(account);
        output["previewDigest"] = json!(preview_digest(self, account));
        output
    }

    pub fn requires_market_price_refresh(&self) -> bool {
        self.intent.is_market_open()
    }
}

#[derive(Default)]
pub struct SubmitOptions<'a> {
    pub allow_concurrent_open: bool,
    pub confirmation: Option<Confirmation<'a>>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AcceptedTrade {
    pub intent_key: String,
    pub status: &'static str,
    pub message: &'static str,
}

/// Use the same Keeper endpoint for session validation, reservation, and submission.
pub struct Trading<'a> {
    keeper: Keeper<'a>,
    storage: &'a Storage<'a>,
    events: EventContext<'a>,
}

impl<'a> Trading<'a> {
    pub fn new(endpoint: &'a str, storage: &'a Storage<'a>, events: EventContext<'a>) -> Self {
        Self {
            keeper: Keeper::new(endpoint, events),
            storage,
            events,
        }
    }

    pub fn load_session(&self, account: Option<&str>) -> Result<Session, SaiError> {
        Ok(self
            .storage
            .load_active_trade_session(self.keeper.endpoint(), account)?)
    }

    pub fn check_open_pending(
        &self,
        session: &Session,
        allow_concurrent_open: bool,
    ) -> Result<(), SaiError> {
        Ok(self.storage.check_open_pending(
            self.keeper.endpoint(),
            session.addr(),
            allow_concurrent_open,
        )?)
    }

    /// Verify a confirmed preview, commit the reservation, then send once.
    /// The reservation stays unknown if Keeper's admission cannot be determined.
    pub async fn submit(
        &self,
        session: &Session,
        preview: &TradePreview,
        options: SubmitOptions<'_>,
    ) -> Result<AcceptedTrade, SaiError> {
        self.submit_with_reference_price(session, preview, options, pending())
            .await
            .map(|(accepted, _)| accepted)
    }

    /// Return the submitted market price and report an interruption during dispatch as unknown.
    pub async fn submit_with_reference_price(
        &self,
        session: &Session,
        preview: &TradePreview,
        options: SubmitOptions<'_>,
        interrupted: impl Future<Output = ()>,
    ) -> Result<(AcceptedTrade, Option<String>), SaiError> {
        let endpoint = self.keeper.endpoint();
        let account = session.addr();
        self.storage.check_active_trade(session, endpoint)?;
        let mut prepared;
        let preview = if preview.requires_market_price_refresh() {
            prepared = preview.clone();
            self.refresh_market_open(&mut prepared).await?;
            self.storage.check_active_trade(session, endpoint)?;
            let price = prepared
                .reference_price
                .as_deref()
                .ok_or_else(SaiError::internal)?;
            positive_decimal(price, "Current price")?;
            prepared.intent.body["intentPrice"] = json!(price);
            &prepared
        } else {
            preview
        };
        if let Some(confirmation) = options.confirmation {
            // A logout or account expiry during a refresh invalidates confirmation.
            self.load_session(Some(account))?;
            confirmation.verify(preview, account)?;
        }

        let (key, stamp) = intent_identity();
        let intent = &preview.intent;
        let kind = intent.kind;
        let entity = intent.target.as_ref().map_or("", |target| &target.trade_id);
        let status_hint = |key: &str| self.storage.intent_status_hint(endpoint, account, key);
        self.storage
            .reserve(
                &key,
                endpoint,
                account,
                kind,
                entity,
                options.allow_concurrent_open,
            )
            .map_err(|error| {
                if let StorageError::UncertainIntent(ref blocked) = error {
                    SaiError {
                        intent_key: Some(blocked.clone()),
                        hint: Some(status_hint(blocked)),
                        ..SaiError::from(error)
                    }
                } else {
                    error.into()
                }
            })?;

        let mut semantic = json!({"intentSchemaVersion": "v0", "tsIntentInit": stamp});
        let fields = json!(intent);
        for (field, value) in fields
            .as_object()
            .expect("TradeIntent serializes to an object")
        {
            semantic[field] = value.clone();
        }
        let variables = Map::from_iter([(
            "input".into(),
            json!({
                "intentKey": key,
                "intent": semantic,
                "lifecycle": {"sequence": 1, "lifecycleStep": "jwt_pass", "tsLifecycleStep": stamp},
            }),
        )]);
        let unknown = |message: String| SaiError {
            intent_key: Some(key.clone()),
            hint: Some(status_hint(&key)),
            ..SaiError::new(Code::OutcomeUnknown, message)
        };
        let unknown_send = || {
            unknown(format!(
                "Outcome unknown for intent {key}; reconcile before another trade"
            ))
        };
        let response = tokio::select! {
            biased;
            () = interrupted => return Err(unknown_send()),
            response = self.keeper.request(SUBMIT, variables, Some(session.token()), keeper::TIMEOUT) => response,
        };
        let ack = match response {
            Ok(ref data) if valid_ack(data.get("recordUserIntent"), &key, account) => {
                &data["recordUserIntent"]
            }
            Err(mut error) if matches!(error.code, Code::AdmissionRejected | Code::NotSent) => {
                let status = if error.code == Code::AdmissionRejected {
                    IntentStatus::Rejected
                } else {
                    IntentStatus::NotSent
                };
                if !self.record_status(&key, account, status) {
                    error.hint = Some(status_hint(&key));
                }
                error.intent_key = Some(key);
                return Err(error);
            }
            _ => return Err(unknown_send()),
        };
        let (hash, invalid_hash) = keeper::request_hash(&ack["txHash"]);
        self.events.event(
            "intent.submitted",
            Level::Info,
            [
                ("intentKey", json!(key).into()),
                ("kind", json!(kind).into()),
                ("addr", json!(account).into()),
                ("endpoint", Field::Endpoint(endpoint.into())),
                ("endpointId", Field::endpoint_identity(endpoint)),
                ("txHash", json!(hash).into()),
                ("txHashInvalid", json!(invalid_hash.then_some(true)).into()),
            ],
        );
        if !self.record_status(&key, account, IntentStatus::Accepted) {
            return Err(unknown(format!(
                "Keeper accepted intent {key}, but the local ledger update failed; reconcile before another trade"
            )));
        }
        Ok((
            AcceptedTrade {
                intent_key: key,
                status: "accepted",
                message: "Accepted by Keeper; execution is not yet confirmed",
            },
            preview
                .requires_market_price_refresh()
                .then(|| preview.reference_price.clone())
                .flatten(),
        ))
    }

    fn record_status(&self, key: &str, account: &str, status: IntentStatus) -> bool {
        let endpoint = self.keeper.endpoint();
        let error_type = match self.storage.set_status(key, endpoint, account, status) {
            Ok(Some(_)) => return true,
            Ok(None) => "MissingIntent",
            Err(StorageError::Sqlite(_)) => "OperationalError",
            Err(StorageError::Io(_)) => "OSError",
            Err(_) => "ValueError",
        };
        self.events.event(
            "intent.status_write_failed",
            Level::Warn,
            [
                ("code", json!(Code::InternalError).into()),
                ("intentKey", json!(key).into()),
                ("addr", json!(account).into()),
                ("endpoint", Field::Endpoint(endpoint.into())),
                ("endpointId", Field::endpoint_identity(endpoint)),
                ("status", json!(status.as_str()).into()),
                ("errorType", json!(error_type).into()),
            ],
        );
        false
    }
}

fn intent_identity() -> (String, String) {
    const ALPHABET: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    let now = Utc::now();
    let random = Uuid::new_v4().as_u128();
    let suffix: String = (0..16)
        .map(|index| char::from(ALPHABET[((random >> (5 * index)) & 31) as usize]))
        .collect();
    (
        format!("{}{suffix}", now.format("%Y%m%dT%H%M%S%3fZ_")),
        now.to_rfc3339_opts(SecondsFormat::Millis, true),
    )
}

fn valid_ack(ack: Option<&Value>, key: &str, account: &str) -> bool {
    ack.is_some_and(|ack| {
        ack["intentKey"] == key
            && ack["lifecycleStep"] == "jwt_pass"
            && ack["execActor"] == "server"
            && ack["evmAddress"]
                .as_str()
                .is_some_and(|address| address.eq_ignore_ascii_case(account))
    })
}
