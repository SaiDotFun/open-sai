use std::sync::LazyLock;

use base64::{Engine as _, engine::general_purpose::STANDARD};
use hyper::HeaderMap;
use regex::Regex;
use serde_json::{Value, json};
use url::{Url, form_urlencoded::byte_serialize};
use uuid::Uuid;

use crate::{
    endpoint::Endpoint,
    errors::SaiError,
    keeper::{self, TIMEOUT},
    storage::Session,
};

use super::{
    TradePreview, Trading,
    actions::invalid,
    position::{Lcd, PositionRoute, same_account},
};

impl Trading<'_> {
    /// Verify the live pending order before displaying a cancellation preview.
    pub async fn cancel_preview(
        &self,
        session: &Session,
        preview: &TradePreview,
    ) -> Result<Value, SaiError> {
        self.storage
            .check_active_trade(session, self.keeper.endpoint())?;
        self.check_pending_order(session, preview).await?;
        Ok(preview.output(Some(session.addr())))
    }

    pub(super) async fn check_pending_order(
        &self,
        session: &Session,
        preview: &TradePreview,
    ) -> Result<PositionRoute, SaiError> {
        let lcd = self
            .lcd_endpoint
            .ok_or_else(|| invalid("Cannot verify the pending order without an LCD"))?;
        let id = preview
            .intent
            .target
            .as_ref()
            .filter(|_| preview.intent.kind == "CANCEL_PENDING_TRADE")
            .map(|target| target.trade_id.as_str())
            .ok_or_else(SaiError::internal)?;
        let route = self.position_route(&Lcd(lcd), false).await?;
        let trade = read_cancel_target(
            lcd.trim_end_matches('/'),
            &route.contract_address,
            session.addr(),
            id,
        )
        .await?;
        if trade["user_trade_index"] != format!("UserTradeIndex({id})")
            || trade["is_open"].as_bool() != Some(true)
            || !matches!(trade["trade_type"].as_str(), Some("limit" | "stop"))
        {
            return Err(invalid(
                "Target is not an open pending order. Preview a market close for a filled position.",
            ));
        }
        if !same_account(session.addr(), trade.get("user")) {
            return Err(invalid(
                "Contract pending order belongs to a different account",
            ));
        }
        Ok(route)
    }
}

pub(super) fn estimates() -> Value {
    let unavailable = json!({
        "status": "unavailable",
        "reason": "A pending order can fill before cancellation executes. Perp may close a filled position at market.",
    });
    json!({
        "fees": unavailable,
        "liquidationPrice": unavailable,
        "worstCaseFill": unavailable,
    })
}

async fn read_cancel_target(
    lcd: &str,
    contract: &str,
    trader: &str,
    id: &str,
) -> Result<Value, SaiError> {
    static MISSING: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"Trade not found for trader \S+ with index ([0-9]+)\b")
            .expect("valid missing-order pattern")
    });
    let unreadable = || invalid(&format!("Unable to read pending order {id} from Perp."));
    let index: Value = serde_json::from_str(id).map_err(|_| SaiError::internal())?;
    let query = json!({"get_trade_raw": {"trader": trader, "index": index}});
    let encoded = STANDARD.encode(query.to_string());
    let encoded: String = byte_serialize(encoded.as_bytes()).collect();
    let contract: String = byte_serialize(contract.as_bytes()).collect();
    let url = format!("{lcd}/cosmwasm/wasm/v1/contract/{contract}/smart/{encoded}");
    let mut url = lcd_url(&url)?;
    url.query_pairs_mut()
        .append_pair("_sai_query", &Uuid::new_v4().simple().to_string());
    let (status, _, bytes) = keeper::lcd_get(url.as_str(), HeaderMap::new(), TIMEOUT)
        .await
        .map_err(|_| unreadable())?;
    if !status.is_success()
        && MISSING
            .captures_iter(&String::from_utf8_lossy(&bytes))
            .any(|hit| &hit[1] == id)
    {
        return Err(invalid(&format!(
            "Trade {id} was not found for this account."
        )));
    }
    read_object(status.is_success(), &bytes)
        .map(|mut response| response["data"].take())
        .ok_or_else(unreadable)
}

fn read_object(success: bool, bytes: &[u8]) -> Option<Value> {
    if !success {
        return None;
    }
    serde_json::from_slice::<Value>(bytes)
        .ok()
        .filter(Value::is_object)
}

pub(super) fn lcd_url(raw: &str) -> Result<Url, SaiError> {
    let Some(endpoint) = Endpoint::split(raw).filter(|endpoint| {
        endpoint.scheme == "https"
            || (endpoint.scheme == "http"
                && matches!(
                    endpoint.host_port().0.as_str(),
                    "localhost" | "127.0.0.1" | "[::1]"
                ))
    }) else {
        return Err(invalid("LCD endpoint must use HTTPS, except on loopback"));
    };
    if let Some((userinfo, _)) = endpoint.authority.rsplit_once('@')
        && !matches!(userinfo, "" | ":")
    {
        return Err(invalid("Credentials are not allowed in the LCD URL"));
    }
    Url::parse(raw).map_err(|_| invalid("Unable to read contract state"))
}
