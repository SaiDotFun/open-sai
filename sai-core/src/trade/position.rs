//! Verified Keeper and Perp reads for position previews and submissions.

mod lcd;

use std::time::Duration;

use chrono::{SecondsFormat, Utc};
use serde::Serialize;
use serde_json::{Map, Value, json};
use tokio::time::{Instant, sleep, timeout_at};

use crate::{
    errors::{Code, SaiError},
    keeper::{MARKET, POSITION, ROUTE, TIMEOUT, canonical_account, truthy},
};

use super::{
    Trading,
    actions::{incomplete, invalid},
    positive_decimal,
};
use lcd::height_error;
pub(super) use lcd::{Lcd, Read};

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct PositionRoute {
    pub(super) chain_id: String,
    pub(super) contract_address: String,
}

pub(super) struct Snapshot {
    pub(super) raw: Value,
    pub(super) market: Value,
    pub(super) route: PositionRoute,
    pub(super) observed_at: String,
}

pub(super) enum SnapshotError {
    Owner(&'static str),
    Unavailable,
}

const KEEPER_OWNER: &str = "Keeper position belongs to a different account";
const CONTRACT_OWNER: &str = "Contract position belongs to a different account";

impl From<SaiError> for SnapshotError {
    fn from(_: SaiError) -> Self {
        Self::Unavailable
    }
}

pub(super) fn contract_owner_error() -> SaiError {
    invalid(CONTRACT_OWNER)
}

impl Trading<'_> {
    pub(super) async fn position_route(
        &self,
        lcd: &Lcd<'_>,
        confirming: bool,
    ) -> Result<PositionRoute, SaiError> {
        let route = self
            .keeper
            .request(ROUTE, Map::new(), None, TIMEOUT)
            .await?;
        let route = route
            .get("oneClickTrading")
            .ok_or_else(|| incomplete("KeyError"))?;
        let network = lcd.network().await?;
        let network = network.as_str().filter(|id| !id.trim().is_empty());
        let chain = text_field(route, "chainId")?;
        let mismatch = "Keeper trading route and LCD network do not match";
        if !truthy(field(route, "enabled")?) || chain.trim().is_empty() || network.is_none() {
            return Err(invalid(mismatch));
        }
        let contract = text_field(route, "contractAddress")?;
        if contract.trim().is_empty() {
            return Err(invalid("Keeper trading route has no Perp contract address"));
        }
        if network != Some(chain) {
            return Err(if confirming {
                SaiError::new(
                    Code::PreviewMismatch,
                    format!("{mismatch}. Use an LCD on Keeper's chain and request a new preview."),
                )
            } else {
                invalid(mismatch)
            });
        }
        Ok(PositionRoute {
            chain_id: chain.into(),
            contract_address: contract.into(),
        })
    }

    pub(super) async fn indexed_position(
        &self,
        addr: &str,
        id: &Value,
        timeout: Duration,
    ) -> Result<Value, SaiError> {
        self.read_indexed_position(addr, id, timeout)
            .await
            .map_err(|error| match error {
                IndexedPositionError::Owner => invalid(KEEPER_OWNER),
                IndexedPositionError::Read(error) => error,
            })
    }

    async fn read_indexed_position(
        &self,
        addr: &str,
        id: &Value,
        timeout: Duration,
    ) -> Result<Value, IndexedPositionError> {
        let data = self
            .keeper
            .request(
                POSITION,
                variables([("trader", json!(addr)), ("id", id.clone())]),
                None,
                timeout,
            )
            .await?;
        let position = field(
            data.get("perp").ok_or_else(|| incomplete("KeyError"))?,
            "trade",
        )?;
        if !truthy(position) {
            return Err(invalid("Position not found for this account").into());
        }
        if !same_account(addr, position.get("trader")) {
            return Err(IndexedPositionError::Owner);
        }
        if position.get("id") != Some(id) {
            return Err(invalid("Keeper returned a different position index").into());
        }
        Ok(position.clone())
    }

    pub(super) async fn position_snapshot(
        &self,
        lcd: &Lcd<'_>,
        addr: &str,
        id: &Value,
        route: PositionRoute,
        mut indexed: Value,
    ) -> Result<Snapshot, SnapshotError> {
        require_open(&indexed)?;
        let borrowing = field(&indexed, "perpBorrowing")?.clone();
        let collateral = field(&borrowing, "collateralToken")?;
        if field(collateral, "symbol")? != "USDC" {
            return Err(SnapshotError::Unavailable);
        }
        let trader = text_field(&indexed, "trader")?.to_owned();
        let market_id = field(&borrowing, "marketId")?;
        let token_id = field(collateral, "id")?;
        let token = format!("TokenIndex({token_id})");
        let response = self
            .keeper
            .request(
                MARKET,
                variables([
                    ("market", market_id.clone()),
                    ("collateral", token_id.clone()),
                ]),
                None,
                TIMEOUT,
            )
            .await?;
        let market = field(response.get("perp").ok_or_else(malformed)?, "borrowing")?;
        if !truthy(&market["isOpen"])
            || market["marketId"] != *market_id
            || market["collateralToken"]["id"] != *token_id
        {
            return Err(SnapshotError::Unavailable);
        }
        let read_raw = || async {
            let (raw, _) = lcd
                .position(&route.contract_address, &trader, id, Read::Snapshot)
                .await?;
            if !raw.is_object() {
                return Err(SnapshotError::Unavailable);
            }
            if !same_account(addr, raw.get("user")) {
                return Err(SnapshotError::Owner(CONTRACT_OWNER));
            }
            if raw["user_trade_index"] != format!("UserTradeIndex({id})")
                || raw["market_index"] != format!("MarketIndex({market_id})")
                || raw["collateral_index"] != token
            {
                return Err(SnapshotError::Unavailable);
            }
            require_contract_open(&raw)?;
            Ok(raw)
        };

        let mut raw = read_raw().await?;
        if !same_margin(&raw, &indexed)? {
            let deadline = Instant::now() + Duration::from_secs(2);
            for delay in [250, 500].map(Duration::from_millis) {
                if deadline.saturating_duration_since(Instant::now()) <= delay {
                    break;
                }
                sleep(delay).await;
                indexed = timeout_at(deadline, self.read_indexed_position(addr, id, TIMEOUT))
                    .await
                    .map_err(|_| SnapshotError::Unavailable)?
                    .map_err(|error| match error {
                        IndexedPositionError::Owner => SnapshotError::Owner(KEEPER_OWNER),
                        IndexedPositionError::Read(_) => SnapshotError::Unavailable,
                    })?;
                require_open(&indexed)?;
                if indexed["perpBorrowing"] != borrowing {
                    return Err(SnapshotError::Unavailable);
                }
                raw = timeout_at(deadline, read_raw())
                    .await
                    .map_err(|_| SnapshotError::Unavailable)??;
                if same_margin(&raw, &indexed)? {
                    break;
                }
            }
            if Instant::now() >= deadline || !same_margin(&raw, &indexed)? {
                return Err(SnapshotError::Unavailable);
            }
        }

        let observed_at = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let (funds, _) = lcd
            .smart(
                &route.contract_address,
                json!({"get_exchange_balance": {"user": trader, "collateral_index": token}}),
                Read::Snapshot,
            )
            .await?;
        if funds["collateral_index"] != token {
            return Err(SnapshotError::Unavailable);
        }
        Ok(Snapshot {
            raw,
            market: market.clone(),
            route,
            observed_at,
        })
    }
}

impl Snapshot {
    pub(super) async fn verified_height(
        &self,
        lcd: &Lcd<'_>,
        id: &Value,
    ) -> Result<Option<String>, SnapshotError> {
        let trader = text_field(&self.raw, "user")?;
        let (latest, height) = lcd
            .position(&self.route.contract_address, trader, id, Read::Fresh)
            .await?;
        if !latest.is_object() {
            return Err(SnapshotError::Unavailable);
        }
        if !same_account(trader, latest.get("user")) {
            return Err(SnapshotError::Owner(CONTRACT_OWNER));
        }
        if [
            "user_trade_index",
            "market_index",
            "collateral_index",
            "collateral_amount",
            "leverage",
            "is_open",
            "trade_type",
            "long",
            "open_price",
        ]
        .iter()
        .any(|&key| latest.get(key) != self.raw.get(key))
        {
            return Ok(None);
        }
        if height.as_deref().is_some_and(|height| {
            !height.bytes().all(|byte| byte.is_ascii_digit())
                || height.trim_start_matches('0').is_empty()
        }) {
            return Err(height_error("LCD returned an invalid contract query height.").into());
        }
        Ok(height)
    }

    pub(super) async fn effective_leverage(
        &self,
        lcd: &Lcd<'_>,
        trade_id: &Value,
        height: &str,
    ) -> Result<u128, SaiError> {
        let stored = contract_decimal(field(&self.raw, "leverage")?)?;
        let query = |message| self.smart(lcd, message, height);
        let market_index = &self.raw["market_index"];
        let enabled = query(json!({"is_after_hours_market": {"market_index": market_index}}))
            .await?
            .as_bool()
            .ok_or_else(malformed)?;
        if !enabled {
            return Ok(stored);
        }
        let market = query(json!({"get_market": {"index": market_index}})).await?;
        let opened = optional_block(field(&market, "block_last_opened")?)?;
        let closed = optional_block(field(&market, "block_last_closed")?)?;
        let (Some(opened), Some(closed)) = (opened, closed) else {
            return Ok(stored);
        };
        if opened > closed {
            let infos = query(json!({"get_trade_infos": {
                "trader": self.raw["user"], "start_idx": trade_id, "scan_limit": 1,
            }}))
            .await?;
            let [info] = infos.as_array().map(Vec::as_slice).ok_or_else(malformed)? else {
                return Err(malformed());
            };
            let risk = optional_block(field(info, "created_block")?)?.ok_or_else(malformed)?;
            if risk >= opened {
                return Ok(stored);
            }
        }
        let cap = query(json!({"get_after_hours_leverage": {}})).await?;
        Ok(stored.min(contract_decimal(&cap)?))
    }

    pub(super) async fn smart(
        &self,
        lcd: &Lcd<'_>,
        message: Value,
        height: &str,
    ) -> Result<Value, SaiError> {
        lcd.smart(&self.route.contract_address, message, Read::At(height))
            .await
            .map(|(value, _)| value)
    }
}

enum IndexedPositionError {
    Owner,
    Read(SaiError),
}

impl From<SaiError> for IndexedPositionError {
    fn from(error: SaiError) -> Self {
        Self::Read(error)
    }
}

pub(super) fn same_account(addr: &str, value: Option<&Value>) -> bool {
    value
        .and_then(Value::as_str)
        .and_then(canonical_account)
        .zip(canonical_account(addr))
        .is_some_and(|(owner, account)| owner == account)
}

fn require_open(position: &Value) -> Result<(), SaiError> {
    if truthy(field(position, "isOpen")?) && field(position, "tradeType")? == "trade" {
        Ok(())
    } else {
        Err(invalid("Open position not found"))
    }
}

fn require_contract_open(raw: &Value) -> Result<(), SaiError> {
    if raw["is_open"].as_bool() == Some(true) && raw["trade_type"] == "trade" {
        Ok(())
    } else {
        Err(invalid("Open position not found"))
    }
}

fn same_margin(raw: &Value, indexed: &Value) -> Result<bool, SaiError> {
    Ok(
        number_text(&raw["collateral_amount"])? == number_text(&indexed["collateralAmount"])?
            && float(&raw["leverage"])? == float(&indexed["leverage"])?,
    )
}

pub(super) fn contract_decimal(value: &Value) -> Result<u128, SaiError> {
    match value.as_str().ok_or_else(malformed)? {
        "0" => Ok(0),
        raw => positive_decimal(raw, "Contract quote input"),
    }
}

fn variables<const N: usize>(data: [(&str, Value); N]) -> Map<String, Value> {
    data.into_iter()
        .map(|(key, value)| (key.into(), value))
        .collect()
}

fn optional_block(value: &Value) -> Result<Option<i64>, SaiError> {
    if value.is_null() {
        Ok(None)
    } else {
        value.as_i64().map(Some).ok_or_else(malformed)
    }
}

pub(super) fn field<'a>(value: &'a Value, key: &str) -> Result<&'a Value, SaiError> {
    if !value.is_object() {
        return Err(malformed());
    }
    value.get(key).ok_or_else(|| incomplete("KeyError"))
}

fn text_field<'a>(value: &'a Value, key: &str) -> Result<&'a str, SaiError> {
    field(value, key)?.as_str().ok_or_else(malformed)
}

pub(super) fn number_text(value: &Value) -> Result<String, SaiError> {
    if let Some(text) = value.as_str() {
        Ok(text.to_owned())
    } else if let Some(number) = value.as_number() {
        Ok(number.to_string())
    } else {
        Err(malformed())
    }
}

fn float(value: &Value) -> Result<f64, SaiError> {
    number_text(value)?
        .parse()
        .map_err(|_| invalid("Invalid leverage"))
}

pub(super) fn malformed() -> SaiError {
    incomplete("TypeError")
}
