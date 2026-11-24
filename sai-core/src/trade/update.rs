//! Standalone TP/SL and target-leverage intents.

mod estimates;

use serde_json::{Value, json};

use crate::{
    errors::{Code, SaiError},
    keeper::TIMEOUT,
    storage::Session,
};

use super::{
    TradeIntent, TradePreview, Trading,
    actions::{id, invalid},
    position::{Lcd, PositionRoute, Read, contract_owner_error, same_account},
    positive_decimal,
};

/// Build a patch for the two trigger prices, preserving the caller's decimal strings.
pub fn tpsl_preview(
    trade_id: &str,
    take_profit: Option<&str>,
    stop_loss: Option<&str>,
    clear_take_profit: bool,
    clear_stop_loss: bool,
) -> Result<TradePreview, SaiError> {
    let id = id(trade_id, "TRADE_ID")?;
    let has_tp = take_profit.is_some_and(|price| !price.is_empty());
    let has_sl = stop_loss.is_some_and(|price| !price.is_empty());
    if id.sign() == num_bigint::Sign::Minus
        || !(has_tp || has_sl || clear_take_profit || clear_stop_loss)
        || (has_tp && clear_take_profit)
        || (has_sl && clear_stop_loss)
    {
        return Err(invalid(
            "Specify a trade and at least one non-conflicting TP or SL change",
        ));
    }
    let patch = |price: Option<&str>, clear| -> Result<Value, SaiError> {
        if let Some(price) = price {
            positive_decimal(price, "TP/SL price")?;
            Ok(json!({"operation": "set", "value": price}))
        } else {
            Ok(json!({"operation": if clear { "clear" } else { "keep" }}))
        }
    };
    Ok(TradePreview::new(TradeIntent::new(
        "UPDATE_TRADE",
        Some(id.to_string()),
        json!({
            "takeProfit": patch(take_profit, clear_take_profit)?,
            "stopLoss": patch(stop_loss, clear_stop_loss)?,
        }),
    )))
}

/// Build a target-leverage update without overwriting either trigger.
pub fn leverage_preview(trade_id: &str, leverage: &str) -> Result<TradePreview, SaiError> {
    let id = id(trade_id, "TRADE_ID")?;
    if id.sign() == num_bigint::Sign::Minus {
        return Err(invalid("Trade id must not be negative"));
    }
    positive_decimal(leverage, "Leverage")?;
    Ok(TradePreview::new(TradeIntent::new(
        "UPDATE_TRADE",
        Some(id.to_string()),
        json!({
            "takeProfit": {"operation": "keep"},
            "stopLoss": {"operation": "keep"},
            "requestedLeverage": leverage,
        }),
    )))
}

pub(super) struct PositionUpdate {
    pub(super) preview: TradePreview,
    pub(super) indexed: Value,
}

impl TradePreview {
    pub fn is_standalone_position_update(&self) -> bool {
        // Margin has already quoted and rechecked its own position.
        self.intent.kind == "UPDATE_TRADE" && !self.margin_change
    }

    fn update_id(&self) -> Result<Value, SaiError> {
        if !self.is_standalone_position_update() {
            return Err(SaiError::internal());
        }
        self.intent
            .target
            .as_ref()
            .and_then(|target| serde_json::from_str(&target.trade_id).ok())
            .ok_or_else(SaiError::internal)
    }
}

impl Trading<'_> {
    /// Preview a standalone position update. An unconfirmed CLI dry run can
    /// show the payload without a session. Confirmation always binds a live route.
    pub async fn position_update_preview(
        &self,
        mut preview: TradePreview,
        session: Option<&Session>,
        confirmed: bool,
    ) -> Result<Value, SaiError> {
        let id = preview.update_id()?;
        let Some(session) = session else {
            if confirmed {
                return Err(SaiError::new(Code::SignedOut, "No connected account"));
            }
            preview.estimates = Some(estimates::unknown());
            return Ok(preview.output(None));
        };
        self.storage
            .check_active_trade(session, self.keeper.endpoint())?;
        let addr = session.addr();
        let indexed = self.indexed_position(addr, &id, TIMEOUT).await?;
        let route = if let Some(endpoint) = self.lcd_endpoint {
            let lcd = Lcd(endpoint);
            if confirmed {
                Some(self.verify_position_owner(&lcd, addr, &id, false).await?)
            } else {
                self.position_route(&lcd, false).await.ok()
            }
        } else {
            None
        };
        if confirmed && route.is_none() {
            return Err(invalid("Cannot verify the position without an LCD"));
        }
        self.estimate_position_update(&mut preview, addr, &id, route.clone(), indexed)
            .await?;
        preview.position_route = route;
        Ok(if confirmed {
            preview.confirmed_output(Some(addr))
        } else {
            preview.output(Some(addr))
        })
    }

    pub(super) async fn preflight_position_update(
        &self,
        addr: &str,
        preview: &TradePreview,
        confirming: bool,
    ) -> Result<PositionUpdate, SaiError> {
        let id = preview.update_id()?;
        let indexed = self.indexed_position(addr, &id, TIMEOUT).await?;
        let lcd = self
            .lcd_endpoint
            .map(Lcd)
            .ok_or_else(|| invalid("Cannot verify the position without an LCD"))?;
        let mut preview = preview.clone();
        preview.position_route = Some(
            self.verify_position_owner(&lcd, addr, &id, confirming)
                .await?,
        );
        Ok(PositionUpdate { preview, indexed })
    }

    async fn verify_position_owner(
        &self,
        lcd: &Lcd<'_>,
        addr: &str,
        id: &Value,
        confirming: bool,
    ) -> Result<PositionRoute, SaiError> {
        let route = self.position_route(lcd, confirming).await?;
        let (raw, _) = lcd
            .position(&route.contract_address, addr, id, Read::Fresh)
            .await?;
        if raw["user_trade_index"] != format!("UserTradeIndex({id})") {
            return Err(invalid(&format!("Unable to verify contract position {id}")));
        }
        if !same_account(addr, raw.get("user")) {
            return Err(contract_owner_error());
        }
        Ok(route)
    }

    pub(super) async fn update_refusal(
        &self,
        addr: &str,
        mut update: PositionUpdate,
        mut error: SaiError,
    ) -> SaiError {
        if error.code == Code::PreviewMismatch {
            let Ok(id) = update.preview.update_id() else {
                return error;
            };
            let route = update.preview.position_route.clone();
            if let Err(ownership) = self
                .estimate_position_update(&mut update.preview, addr, &id, route, update.indexed)
                .await
            {
                return ownership;
            }
            if let Some(details) = &mut error.details {
                details.insert(
                    "preview".into(),
                    update.preview.confirmed_output(Some(addr)),
                );
            }
        }
        error
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::trade::{Confirmation, digest_bytes, preview_digest};

    #[test]
    fn position_update_confirmation_matches_python_route_digest() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../tests/parity/position_update_digest.json"
        ))
        .unwrap();
        for case in fixture["cases"].as_array().unwrap() {
            let expected = &case["preview"];
            let addr = expected["addr"].as_str().unwrap();
            let route = &expected["positionRoute"];
            let mut preview = match case["command"].as_str().unwrap() {
                "tpsl" => tpsl_preview("7", Some("110.0100"), None, false, true),
                "leverage" => leverage_preview("7", "10.00"),
                other => panic!("Unexpected fixture command {other}"),
            }
            .unwrap();
            let route = PositionRoute {
                chain_id: route["chainId"].as_str().unwrap().into(),
                contract_address: route["contractAddress"].as_str().unwrap().into(),
            };
            preview.position_route = Some(route.clone());
            preview.estimates = Some(estimates::unknown());
            assert_eq!(
                digest_bytes(&preview, Some(addr)),
                case["canonical"].as_str().unwrap().as_bytes()
            );
            let digest = case["digest"].as_str().unwrap();
            assert_eq!(preview_digest(&preview, Some(addr)), digest);
            Confirmation::new(digest, None)
                .unwrap()
                .verify(&preview, addr)
                .unwrap();

            for changed in [
                PositionRoute {
                    chain_id: "other-chain".into(),
                    ..route.clone()
                },
                PositionRoute {
                    contract_address: "nibi1othercontract".into(),
                    ..route.clone()
                },
            ] {
                preview.position_route = Some(changed.clone());
                let error = Confirmation::new(digest, None)
                    .unwrap()
                    .verify(&preview, addr)
                    .unwrap_err();
                assert_eq!(error.code, Code::PreviewMismatch);
                let replacement = &error.details.unwrap()["preview"];
                assert_eq!(replacement["positionRoute"], json!(changed));
                assert_ne!(replacement["previewDigest"], digest);
            }
        }
    }
}
