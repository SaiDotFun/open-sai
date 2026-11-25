//! Trade previews and one reservation and submission path for CLI and MCP.

mod actions;
mod cancel;
mod confirmation;
mod estimates;
mod position;
mod position_change;
mod update;

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
pub use estimates::PreviewMode;
pub use position_change::{
    MarginDirection, PositionAction, PositionMode, parse_partial_close_percent,
};
pub use update::{leverage_preview, tpsl_preview};

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
    position_route: Option<position::PositionRoute>,
    position_owner_verified: Option<bool>,
    estimates: Option<Value>,
    margin_change: bool,
}

impl TradePreview {
    pub fn new(intent: TradeIntent) -> Self {
        Self {
            intent,
            reference_price: None,
            quote: None,
            warning: None,
            position_route: None,
            position_owner_verified: None,
            estimates: None,
            margin_change: false,
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
        if self.position_owner_verified == Some(false) {
            output["positionOwnerVerified"] = json!(false);
        }
        if let Some(estimates) = &self.estimates {
            output["estimates"] = estimates.clone();
        } else if self.intent.kind == "CANCEL_PENDING_TRADE" {
            output["estimates"] = cancel::estimates();
        }
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
        if self.intent.kind == "CLOSE_POSITION" && self.position_owner_verified != Some(true) {
            return output;
        }
        if let Some(route) = &self.position_route {
            output["positionRoute"] = json!(route);
        }
        output["previewDigest"] = json!(preview_digest(self, account));
        output
    }

    /// Include only confirmations that the CLI can use to submit.
    pub fn cli_preview_output(&self, account: Option<&str>) -> Value {
        let mut output = self.output(account);
        if let Some(route) = &self.position_route {
            output["positionRoute"] = json!(route);
            let execution = &output["estimates"]["execution"];
            let rejected = execution["status"] == "would_be_rejected";
            let missing_oracle = execution["code"] == "OraclePublicationTimeUnknown";
            if rejected && let Some(fields) = output.as_object_mut() {
                fields.shift_remove("quote");
            }
            if self.position_owner_verified != Some(false)
                && (!self.requires_market_price_refresh() || self.reference_price.is_some())
                && !rejected
                && !missing_oracle
            {
                output["previewDigest"] = json!(preview_digest(self, account));
            }
        }
        output
    }

    pub fn is_market_close(&self) -> bool {
        self.intent.kind == "CLOSE_POSITION"
    }

    fn position_id(&self) -> Result<Value, SaiError> {
        self.intent
            .target
            .as_ref()
            .and_then(|target| serde_json::from_str(&target.trade_id).ok())
            .ok_or_else(SaiError::internal)
    }

    pub fn requires_market_price_refresh(&self) -> bool {
        self.intent.is_market_open()
    }
}

type ReservationCallback<'a> = dyn Fn(&str, &str) + Sync + 'a;

#[derive(Default)]
pub struct SubmitOptions<'a> {
    pub allow_concurrent_open: bool,
    pub confirmation: Option<Confirmation<'a>>,
    /// Receive the pinned account and committed key before dispatch to Keeper.
    pub on_reserved: Option<&'a ReservationCallback<'a>>,
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
    lcd_endpoint: Option<&'a str>,
    storage: &'a Storage<'a>,
    events: EventContext<'a>,
}

impl<'a> Trading<'a> {
    pub fn new(endpoint: &'a str, storage: &'a Storage<'a>, events: EventContext<'a>) -> Self {
        Self {
            keeper: Keeper::new(endpoint, events),
            lcd_endpoint: None,
            storage,
            events,
        }
    }

    /// Select the LCD used for live contract checks and preview estimates.
    pub fn with_lcd_endpoint(mut self, endpoint: &'a str) -> Self {
        self.lcd_endpoint = Some(endpoint);
        self
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

    /// Produce a route-bound preview for callers that require confirmation.
    pub async fn confirmed_trade_preview(
        &self,
        session: &Session,
        mut preview: TradePreview,
    ) -> Result<Value, SaiError> {
        let endpoint = self.keeper.endpoint();
        self.storage.check_active_trade(session, endpoint)?;
        if !matches!(
            preview.intent.kind,
            "OPEN_TRADE" | "CLOSE_POSITION" | "CANCEL_PENDING_TRADE"
        ) {
            return Err(SaiError::internal());
        }
        if !preview.is_market_close() {
            preview.position_route = self
                .basic_trade_route(session, &preview, true, None)
                .await?;
        }
        let market_row = if preview.requires_market_price_refresh() {
            let row = self.refresh_market_quote(&mut preview).await?;
            let price = preview
                .reference_price
                .as_deref()
                .ok_or_else(SaiError::internal)?;
            confirmation::reference_price_units(price, "Current price")?;
            Some(row)
        } else {
            None
        };
        self.estimate_preview(&mut preview, Some(session), PreviewMode::Mcp, market_row)
            .await?;
        self.load_session(Some(session.addr()))?;
        Ok(preview.confirmed_output(Some(session.addr())))
    }

    async fn basic_trade_route(
        &self,
        session: &Session,
        preview: &TradePreview,
        bind_open: bool,
        confirmation: Option<&Confirmation<'_>>,
    ) -> Result<Option<position::PositionRoute>, SaiError> {
        let account = session.addr();
        let no_lcd = match preview.intent.kind {
            "CLOSE_POSITION" => {
                return self
                    .close_submit_route(session, preview, confirmation)
                    .await
                    .map(Some);
            }
            "CANCEL_PENDING_TRADE" => "Cannot verify the pending order without an LCD",
            "OPEN_TRADE" if bind_open => "Cannot verify the trade route without an LCD",
            _ => return Ok(None),
        };
        let lcd = self
            .lcd_endpoint
            .map(position::Lcd)
            .ok_or_else(|| actions::invalid(no_lcd))?;
        let route = self.position_route(&lcd, confirmation.is_some()).await?;
        if let Some(confirmation) = confirmation {
            // A replacement contract may not contain the target or market.
            // Route checks cannot issue a digest before those reads.
            confirmation.verify_route_digest(preview, account, &route)?;
        }
        if preview.intent.kind == "CANCEL_PENDING_TRADE" {
            self.check_pending_target(session, preview, &lcd, &route)
                .await?;
        }
        Ok(Some(route))
    }

    async fn close_submit_route(
        &self,
        session: &Session,
        preview: &TradePreview,
        confirmation: Option<&Confirmation<'_>>,
    ) -> Result<position::PositionRoute, SaiError> {
        let account = session.addr();
        let id = preview.position_id()?;
        let confirmed_route = if let Some(confirmation) = confirmation {
            // Route drift can remove the position from Keeper's index.
            let route = position::PositionRoute::from_keeper(&self.keeper_position_route().await?)?;
            confirmation.verify_route_digest(preview, account, &route)?;
            Some(route)
        } else {
            None
        };
        self.indexed_position(account, &id, keeper::TIMEOUT).await?;
        let lcd = self
            .lcd_endpoint
            .map(position::Lcd)
            .ok_or_else(|| actions::invalid("Cannot verify the position without an LCD"))?;
        let route = if let Some(route) = confirmed_route {
            route.verify_network(&lcd.network().await?, true)?;
            route
        } else {
            self.position_route(&lcd, false).await?
        };
        let (route, _) = Self::verify_contract_owner(&lcd, account, &id, route).await?;
        Ok(route)
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
        let confirming = options.confirmation.is_some();
        let cli_confirmation = options.confirmation.as_ref().filter(|confirmation| {
            confirmation.is_cli()
                && (matches!(
                    preview.intent.kind,
                    "OPEN_TRADE" | "CLOSE_POSITION" | "CANCEL_PENDING_TRADE"
                ) || preview.is_standalone_position_update())
        });
        let cli_preview = if let Some(confirmation) = cli_confirmation {
            Some(
                self.prepare_cli_confirmation(session, preview, confirmation)
                    .await?,
            )
        } else {
            None
        };
        let route = if cli_preview.is_none() {
            self.basic_trade_route(session, preview, confirming, options.confirmation.as_ref())
                .await?
        } else {
            None
        };
        let checked_route = route.is_some() || cli_preview.is_some();
        let open = preview.intent.kind == "OPEN_TRADE";
        let update = if cli_preview.is_none() && preview.is_standalone_position_update() {
            Some(
                self.preflight_position_update(account, preview, options.confirmation.is_some())
                    .await?,
            )
        } else {
            None
        };
        let mut prepared;
        let preview = if let Some(preview) = &cli_preview {
            preview
        } else if open || confirming && checked_route {
            prepared = preview.clone();
            prepared.position_route = route;
            if open {
                self.prepare_open_submission(session, &mut prepared, confirming)
                    .await?;
            }
            &prepared
        } else if let Some(update) = &update {
            &update.preview
        } else {
            preview
        };
        // Live reads can outlast the session. Reload the pinned account
        // before confirmation and reserve with its current token.
        let reloaded;
        let session = if checked_route || open || update.is_some() || confirming {
            reloaded = self.load_session(Some(account))?;
            &reloaded
        } else {
            session
        };
        if let Some(confirmation) = options.confirmation
            && let Err(mut error) = confirmation.verify(preview, account)
        {
            if let Some(update) = update {
                return Err(self.update_refusal(account, update, error).await);
            }
            if let Some(fresh) = error
                .details
                .as_deref_mut()
                .and_then(|details| details.get_mut("preview"))
            {
                let mut replacement = preview.clone();
                if replacement.estimates.is_none() {
                    self.estimate_preview(&mut replacement, Some(session), PreviewMode::Mcp, None)
                        .await?;
                }
                *fresh = replacement.confirmed_output(Some(account));
            }
            return Err(error);
        }
        preview.check_cli_execution(account, confirming)?;

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
        if let Some(on_reserved) = options.on_reserved {
            on_reserved(account, &key);
        }

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
