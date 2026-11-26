use std::{future::pending, path::Path};

use sai_core::{
    errors::{Code, SaiError},
    log::{self, EventContext, Surface},
    storage::{Storage, StorageEvent, is_trade_reservation_diagnostic},
    trade::{
        Confirmation, MARKET_PRICE_NOTICE, MarginDirection, OpenTrade, OrderType, PositionAction,
        PositionMode, PreviewMode, Side, SubmitOptions, Trading, close_preview, leverage_preview,
        tpsl_preview,
    },
};
use serde_json::{Map, Value, json};

use super::{SaiMcp, session::StoredSession};

pub(super) struct Action<'a> {
    name: &'a str,
    kind: &'a str,
    submit: bool,
}

impl<'a> Action<'a> {
    pub(super) fn named(name: &'a str) -> Option<Self> {
        let (prefix, kind) = name.split_once('_')?;
        matches!(prefix, "preview" | "trade").then_some(Self {
            name,
            kind,
            submit: prefix == "trade",
        })
    }

    async fn run(
        self,
        args: &Map<String, Value>,
        storage: &Storage<'_>,
        endpoint: &str,
        lcd: &str,
        events: EventContext<'_>,
    ) -> Result<Value, SaiError> {
        let text = |key: &str| args.get(key).and_then(Value::as_str);
        let required = |key: &str| text(key).expect("validated trade argument");
        let integer = |key: &str| {
            args[key]
                .as_number()
                .expect("validated trade integer")
                .as_str()
        };
        let flag = |key: &str| args.get(key).and_then(Value::as_bool).unwrap_or(false);
        let addr = text("addr");
        let signed_out_open = !self.submit && self.kind == "open" && addr.is_none_or(str::is_empty);
        let session =
            StoredSession::load(storage, endpoint, addr).into_trade_session(signed_out_open)?;
        let addr = session.as_ref().map(|session| session.addr());
        let trading = Trading::new(endpoint, storage, events).with_lcd_endpoint(lcd);
        let order_type = (self.kind == "open").then(|| match required("order_type") {
            "market" => OrderType::Market,
            "limit" => OrderType::Limit,
            "stop" => OrderType::Stop,
            _ => unreachable!("validated order type"),
        });
        let options = if self.submit {
            let digest = required("preview_digest");
            let confirmation = if let Some(order_type) = order_type {
                Confirmation::for_mcp_open(order_type, digest, text("reference_price"))?
            } else {
                Confirmation::for_mcp(digest, None)?
            };
            Some(SubmitOptions {
                allow_concurrent_open: flag("allow_concurrent_open"),
                confirmation: Some(confirmation),
                ..SubmitOptions::default()
            })
        } else {
            None
        };
        if matches!(self.kind, "margin" | "partial_close") {
            let mode = options.map_or(PositionMode::Preview, PositionMode::Submit);
            let action = if self.kind == "margin" {
                PositionAction::Margin {
                    direction: match required("direction") {
                        "add" => MarginDirection::Add,
                        "remove" => MarginDirection::Remove,
                        _ => unreachable!("validated margin direction"),
                    },
                    amount: required("amount"),
                }
            } else {
                let percent = integer("percent")
                    .parse()
                    .ok()
                    .filter(|percent| (1..=99).contains(percent))
                    .ok_or_else(|| {
                        SaiError::new(
                            Code::InvalidRequest,
                            "Partial close percentage must be from 1 to 99",
                        )
                    })?;
                PositionAction::PartialClose { percent }
            };
            return trading
                .position_trade(addr, lcd, integer("trade_id"), action, mode)
                .await;
        }

        let preview = match self.kind {
            "open" => OpenTrade {
                market: integer("market_id"),
                side: match required("side") {
                    "long" => Side::Long,
                    "short" => Side::Short,
                    _ => unreachable!("validated side"),
                },
                order_type: order_type.expect("validated open order type"),
                amount: required("amount"),
                leverage: required("leverage"),
                price: text("price"),
                slippage: required("slippage"),
                take_profit: text("take_profit"),
                stop_loss: text("stop_loss"),
            }
            .confirmed_cli_preview()?,
            "close" => close_preview(integer("trade_id"), flag("cancel_order"))?,
            "tpsl" => tpsl_preview(
                integer("trade_id"),
                text("take_profit"),
                text("stop_loss"),
                flag("clear_take_profit"),
                flag("clear_stop_loss"),
            )?,
            "leverage" => leverage_preview(integer("trade_id"), required("leverage"))?,
            _ => unreachable!("known trade tool"),
        };
        let Some(options) = options else {
            if preview.is_standalone_position_update() {
                return trading
                    .position_update_preview(preview, session.as_ref(), false)
                    .await;
            }
            let preview = trading
                .prepare_preview(preview, session.as_ref(), PreviewMode::Mcp)
                .await?;
            return Ok(preview.mcp_preview_output(addr));
        };

        let session = session.as_ref().expect("submit requires a session");
        let notice = preview.requires_market_price_refresh() && text("price").is_some();
        let (accepted, price) = trading
            .submit_with_reference_price(session, &preview, options, pending())
            .await?;
        let mut result = json!(accepted);
        if let Some(price) = price {
            result["referencePrice"] = json!(price);
        }
        if notice {
            result["notices"] = json!([MARKET_PRICE_NOTICE]);
        }
        result["addr"] = json!(addr);
        Ok(result)
    }
}

impl SaiMcp {
    pub(super) async fn trade(
        &self,
        action: Action<'_>,
        args: &Map<String, Value>,
        call_id: &str,
    ) -> Result<Value, SaiError> {
        let home = self.env.home.clone();
        let endpoint = self.settings.endpoint().to_owned();
        let lcd = self.settings.lcd_endpoint().to_owned();
        let writer = self.writer.clone();
        let name = action.name.to_owned();
        let args = args.clone();
        let call_id = call_id.to_owned();
        let runtime = tokio::runtime::Handle::current();
        let (_cancel_on_drop, cancel) = tokio::sync::oneshot::channel::<()>();
        // Trades hold their slot across network reads and writes.
        Self::blocking_work(&self.trade_slots, move || {
            let events = EventContext::new(writer.as_deref(), Surface::Mcp, Some("mcp serve"))
                .with_tool(&name, &call_id);
            let log_storage = |event: StorageEvent<'_>| {
                crate::local::storage_event_with_endpoint(events, event, Some(&endpoint))
            };
            let storage = Storage::new(home).with_event_sink(&log_storage);
            let action = Action::named(&name).expect("known trade tool");
            runtime.block_on(async {
                tokio::select! {
                    biased;
                    _ = cancel => Err(SaiError::internal()),
                    result = action.run(&args, &storage, &endpoint, &lcd, events) => result,
                }
            })
        })
        .await
    }
}

pub(super) fn failure(home: &Path, error: SaiError) -> Value {
    if error.code == Code::InternalError && !is_trade_reservation_diagnostic(&error.message) {
        eprintln!("Unexpected error in Sai MCP tool");
        return json!({
            "error": "Unexpected error in the Sai MCP server; see its stderr log",
            "code": error.code,
        });
    }
    let mut details = error.details.map(|details| *details).unwrap_or_default();
    let preview = details.shift_remove("preview");
    let reason = preview.as_ref().and_then(|preview| {
        let execution = &preview["estimates"]["execution"];
        (execution["status"] == "would_be_rejected")
            .then(|| execution["reason"].as_str())
            .flatten()
    });
    let mut result = json!({"error": reason.unwrap_or(&error.message), "code": error.code});
    match error.code {
        Code::OutcomeUnknown => {
            if let Some(key) = error.intent_key {
                result["intentKey"] = json!(key);
                result["next"] = json!(format!(
                    "Call intent_status with key {key} to reconcile. Do not resubmit until it reports a final outcome."
                ));
            }
        }
        Code::RecentOpenPending => {
            if let Some(key) = error.intent_key {
                result["error"] = json!(format!(
                    "Earlier open intent {key} may still execute. Call intent_status with each key in intentKeys before retrying."
                ));
            }
        }
        Code::PreviewMismatch => {
            if let Some(preview) = preview
                .as_ref()
                .filter(|preview| preview["previewDigest"].is_string())
            {
                result["preview"] = preview.clone();
                result["quoteStatus"] = json!("available");
                result["recovery"] = json!({
                    "action": "confirm_preview",
                    "message": "Show the replacement preview and get confirmation before retrying.",
                });
            } else {
                result["quoteStatus"] = json!("unavailable");
                result["recovery"] = json!({
                    "action": "request_preview",
                    "message": "Check the reported cause. If the trade is still possible, call the matching preview tool and get confirmation before retrying."
                });
                if let Some(preview) = preview {
                    result["preview"] = preview;
                }
            }
        }
        _ => {
            if let Some(preview) = preview {
                result["preview"] = preview;
            }
        }
    }
    result
        .as_object_mut()
        .expect("tool error object")
        .extend(details);
    crate::redact_details(&result, &|text| log::redact(home, text))
}
