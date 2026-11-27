//! Contract quotes and confirmation for changes to an existing position.

mod quote;

use num_bigint::BigUint;
use std::{
    future::{Future, pending},
    time::Duration,
};

const KEEPER_OWNER_MISMATCH: &str = "Keeper position belongs to a different account";
const CONTRACT_OWNER_MISMATCH: &str = "Contract position belongs to a different account";
const NETWORK_MISMATCH: &str = "Keeper trading route and LCD network do not match";
const INDEX_LAG: &str = "Indexed position differs from contract state. Retry after Keeper synchronizes. No intent was submitted.";
const INDEX_RETRY_BUDGET: Duration = Duration::from_secs(2);
const INDEX_RETRY_DELAYS: [Duration; 2] = [Duration::from_millis(250), Duration::from_millis(500)];

use serde_json::{Map, Value, json};
use tokio::time::{Instant, sleep, timeout_at};

use crate::{
    errors::{Code, SaiError},
    keeper::{MARKET, ROUTE, TIMEOUT, truthy},
};

use super::estimates::{PartialCloseQuote, now};
use super::position::{
    Lcd, PositionRoute, Read, Snapshot as ExecutionSnapshot, height_error, position_mismatch,
    same_account,
};
use super::update::leverage_execution_estimates;
use super::{Confirmation, SubmitOptions, TradePreview, Trading, actions::id, digest_bytes};
use quote::ContractDecimal;

#[derive(Clone, Copy, Eq, PartialEq)]
pub enum MarginDirection {
    Add,
    Remove,
}

#[derive(Clone, Copy)]
pub enum PositionAction<'a> {
    Margin {
        direction: MarginDirection,
        amount: &'a str,
    },
    PartialClose {
        percent: u8,
    },
}

pub enum PositionMode<'a> {
    DryRun,
    Preview,
    McpPreview,
    Submit(SubmitOptions<'a>),
}

pub fn parse_partial_close_percent(input: &str) -> Result<u8, SaiError> {
    let percent = id(input, "--percent")?.to_string();
    percent
        .parse::<u8>()
        .ok()
        .filter(|percent| (1..=99).contains(percent))
        .ok_or_else(|| {
            SaiError::usage(format!(
                "Invalid value for '--percent': {percent} is not in the range 1<=x<=99."
            ))
        })
}

struct Snapshot {
    raw: Value,
    market: Value,
    route: PositionRoute,
}

struct PositionQuote {
    snapshot: Snapshot,
    inputs: Value,
    preview: TradePreview,
}

struct RecheckedPosition {
    height: String,
    effective: u128,
    prices: Option<Value>,
}

impl Trading<'_> {
    /// Quote or submit against one account. A live submission rechecks the
    /// contract inputs at a single LCD block before using the shared submit path.
    pub async fn position_trade(
        &self,
        account: Option<&str>,
        lcd_endpoint: &str,
        trade_id: &str,
        action: PositionAction<'_>,
        mode: PositionMode<'_>,
    ) -> Result<Value, SaiError> {
        self.position_trade_with_interrupt(account, lcd_endpoint, trade_id, action, mode, pending())
            .await
    }

    /// Use the shared submit path to report an interruption during dispatch.
    pub async fn position_trade_with_interrupt(
        &self,
        account: Option<&str>,
        lcd_endpoint: &str,
        trade_id: &str,
        action: PositionAction<'_>,
        mode: PositionMode<'_>,
        interrupted: impl Future<Output = ()>,
    ) -> Result<Value, SaiError> {
        let id = id(trade_id, "TRADE_ID")?;
        let id = serde_json::from_str(&id.to_string()).map_err(|_| SaiError::internal())?;
        let session = self.load_session(account)?;
        let addr = session.addr();
        let lcd = Lcd(lcd_endpoint);
        let confirming =
            matches!(&mode, PositionMode::Submit(options) if options.confirmation.is_some());
        let mut first = self
            .quote_for_position(&lcd, addr, &id, action, confirming)
            .await?;
        let PositionMode::Submit(options) = mode else {
            first.estimate_execution(&lcd, &id, action, None).await?;
            return Ok(match mode {
                PositionMode::Preview | PositionMode::McpPreview => {
                    first.preview.cli_preview_output(Some(addr))
                }
                _ => first.preview.output(Some(addr)),
            });
        };

        let cli = options
            .confirmation
            .as_ref()
            .is_none_or(Confirmation::is_cli);
        let mut current = self
            .quote_for_position(&lcd, addr, &id, action, confirming)
            .await?;
        let rechecked = current.recheck(&lcd, &id).await?;
        let Some(rechecked) = rechecked else {
            let latest = async {
                let mut latest = self.quote_for_position(&lcd, addr, &id, action, confirming).await?;
                let checked = latest.recheck(&lcd, &id).await?.ok_or_else(|| {
                    SaiError::new(Code::PreviewMismatch, "The position quote changed again during verification. Request a new preview.")
                })?;
                latest.estimate_execution(&lcd, &id, action, Some(&checked)).await?;
                Ok(latest)
            }.await;
            return Err(position_changed(action, addr, latest, cli));
        };
        current
            .estimate_execution(&lcd, &id, action, Some(&rechecked))
            .await?;
        if digest_bytes(&current.preview, None) != digest_bytes(&first.preview, None) {
            return Err(position_changed(action, addr, Ok(current), cli));
        }
        let (accepted, _) = self
            .submit_with_reference_price(&session, &current.preview, options, interrupted)
            .await?;
        let mut output = json!({
            "addr": addr,
            "intentKey": accepted.intent_key,
            "status": accepted.status,
            "message": accepted.message,
        });
        if let PositionAction::Margin { .. } = action {
            output["quote"] = current.preview.quote.unwrap_or(Value::Null);
            output["warning"] = json!(current.preview.warning);
        }
        Ok(output)
    }

    async fn quote_for_position(
        &self,
        lcd: &Lcd<'_>,
        addr: &str,
        trade_id: &Value,
        action: PositionAction<'_>,
        confirming: bool,
    ) -> Result<PositionQuote, SaiError> {
        let error = match self.quote_position(lcd, addr, trade_id, action).await {
            Err(error) if is_network_mismatch(&error) => error,
            result => return result,
        };
        if !confirming {
            return Err(invalid(&error.message));
        }
        let message = match self.quote_position(lcd, addr, trade_id, action).await {
            Ok(mut fresh) => {
                let checked = fresh.recheck(lcd, trade_id).await.map_err(|error| {
                    SaiError::new(Code::PreviewMismatch, format!("Could not verify the replacement position quote. {}", error.message))
                })?.ok_or_else(|| SaiError::new(
                    Code::PreviewMismatch,
                    "The position quote changed again during verification. Request a new preview.",
                ))?;
                fresh.estimate_execution(lcd, trade_id, action, Some(&checked)).await?;
                return Err(replacement_preview(
                    "Keeper trading route and LCD network differed during confirmation. Review the replacement preview before submitting.",
                    &fresh.preview, addr,
                ));
            }
            Err(error) if is_network_mismatch(&error) => format!(
                "{NETWORK_MISMATCH}. Use an LCD on Keeper's chain and request a new preview."
            ),
            Err(error) if is_owner_mismatch(&error) => return Err(error),
            Err(_) => "Could not refresh the position preview after an LCD network mismatch. Request a new preview before submitting.".into(),
        };
        Err(SaiError::new(Code::PreviewMismatch, message))
    }

    async fn quote_position(
        &self,
        lcd: &Lcd<'_>,
        addr: &str,
        trade_id: &Value,
        action: PositionAction<'_>,
    ) -> Result<PositionQuote, SaiError> {
        let snapshot = self
            .read_position_change_snapshot(
                lcd,
                addr,
                trade_id,
                matches!(action, PositionAction::PartialClose { .. }),
            )
            .await?;
        let delta = if let PositionAction::PartialClose { percent } = action {
            Some(quote::reduction(&snapshot.raw, percent)?)
        } else {
            None
        };

        let (effective, inputs) = snapshot.leverage(lcd, trade_id, None).await?;
        let mut preview = match action {
            PositionAction::Margin { direction, amount } => {
                let leverage = effective.as_ref().ok_or_else(|| {
                    invalid("Cannot verify effective leverage for this margin change")
                })?;
                quote::margin(
                    trade_id,
                    &snapshot,
                    direction,
                    amount,
                    leverage,
                    inputs.clone(),
                )?
            }
            PositionAction::PartialClose { percent } => quote::partial(
                trade_id,
                &snapshot.raw,
                percent,
                &delta.ok_or_else(SaiError::internal)?,
                effective.as_ref(),
                inputs.clone(),
            )?,
        };
        preview.position_route = Some(snapshot.route.clone());
        Ok(PositionQuote {
            snapshot,
            inputs,
            preview,
        })
    }

    async fn read_position_change_snapshot(
        &self,
        lcd: &Lcd<'_>,
        addr: &str,
        trade_id: &Value,
        require_open_market: bool,
    ) -> Result<Snapshot, SaiError> {
        let route = self.position_request(ROUTE, Map::new()).await?;
        let route = field(&route, "oneClickTrading")?;
        if route["enabled"] != true {
            return Err(invalid(NETWORK_MISMATCH));
        }
        let position = self.indexed_position(addr, trade_id, TIMEOUT).await?;
        require_open_position(&position)?;
        let trader = text_field(&position, "trader")?;
        let chain_id = route["chainId"]
            .as_str()
            .filter(|chain| !chain.is_empty())
            .ok_or_else(|| invalid(NETWORK_MISMATCH))?;
        let network = lcd.network().await?;
        let contract = route["contractAddress"]
            .as_str()
            .filter(|contract| !contract.is_empty())
            .ok_or_else(|| invalid("Keeper trading route has no contract address"))?;
        if network != chain_id {
            return Err(SaiError::new(Code::PreviewMismatch, NETWORK_MISMATCH));
        }
        let borrowing = field(&position, "perpBorrowing")?;
        let collateral = field(borrowing, "collateralToken")?;
        if field(collateral, "symbol")? != "USDC" {
            return Err(invalid("Only USDC positions are supported"));
        }
        let market_id = field(borrowing, "marketId")?;
        let token_id = field(collateral, "id")?;
        let market = self
            .position_request(
                MARKET,
                variables([
                    ("market", market_id.clone()),
                    ("collateral", token_id.clone()),
                ]),
            )
            .await?;
        let market = field(field(&market, "perp")?, "borrowing")?;
        if !truthy(market)
            || field(market, "marketId")? != market_id
            || field(field(market, "collateralToken")?, "id")? != token_id
            || (require_open_market && !truthy(field(market, "isOpen")?))
        {
            return Err(invalid("Open market data is unavailable"));
        }
        let token = format!("TokenIndex({token_id})");
        let read_contract = || async {
            let (raw, _) = lcd
                .position(contract, trader, trade_id, Read::Snapshot)
                .await?;
            require_object(&raw)?;
            if !same_account(addr, raw.get("user")) {
                return Err(invalid(CONTRACT_OWNER_MISMATCH));
            }
            if raw["user_trade_index"] != format!("UserTradeIndex({trade_id})")
                || raw["market_index"] != format!("MarketIndex({market_id})")
                || raw["collateral_index"] != token
                || raw["is_open"] != true
                || raw["trade_type"] != "trade"
            {
                return Err(position_mismatch());
            }
            Ok(raw)
        };
        let raw = read_contract().await?;
        let raw = if same_margin(&raw, &position)? {
            raw
        } else {
            // Retry only amount or leverage drift, with both reads checked again.
            let index_lag = || SaiError {
                retryable_override: true,
                ..invalid(INDEX_LAG)
            };
            let deadline = Instant::now() + INDEX_RETRY_BUDGET;
            timeout_at(deadline, async {
                for delay in INDEX_RETRY_DELAYS {
                    if deadline.saturating_duration_since(Instant::now()) <= delay {
                        break;
                    }
                    sleep(delay).await;
                    let position = self.indexed_position(addr, trade_id, TIMEOUT).await?;
                    require_open_position(&position)?;
                    if field(&position, "perpBorrowing")? != borrowing {
                        return Err(position_mismatch());
                    }
                    let raw = read_contract().await?;
                    if same_margin(&raw, &position)? && Instant::now() < deadline {
                        return Ok(raw);
                    }
                }
                Err(index_lag())
            })
            .await
            .unwrap_or_else(|_| Err(index_lag()))?
        };
        let (funds, _) = lcd
            .smart(
                contract,
                json!({"get_exchange_balance": {
                    "user": trader, "collateral_index": token,
                }}),
                Read::Snapshot,
            )
            .await?;
        require_object(&funds)?;

        if funds["collateral_index"] != token {
            return Err(position_mismatch());
        }
        Ok(Snapshot {
            raw,
            market: market.clone(),
            route: PositionRoute {
                chain_id: chain_id.into(),
                contract_address: contract.into(),
            },
        })
    }

    async fn position_request(
        &self,
        document: &str,
        variables: Map<String, Value>,
    ) -> Result<Value, SaiError> {
        self.keeper
            .request(document, variables, None, TIMEOUT)
            .await
            .map(Value::Object)
    }
}

fn require_open_position(position: &Value) -> Result<(), SaiError> {
    if !truthy(field(position, "isOpen")?) || field(position, "tradeType")? != "trade" {
        return Err(invalid("Open position not found"));
    }
    Ok(())
}

fn same_margin(raw: &Value, position: &Value) -> Result<bool, SaiError> {
    Ok(number_text(&raw["collateral_amount"])?
        == number_text(field(position, "collateralAmount")?)?
        && float(&raw["leverage"])? == float(field(position, "leverage")?)?)
}

impl PositionQuote {
    async fn recheck(
        &self,
        lcd: &Lcd<'_>,
        trade_id: &Value,
    ) -> Result<Option<RecheckedPosition>, SaiError> {
        let snapshot = &self.snapshot;
        let trader = text_field(&snapshot.raw, "user")?;
        let (latest, height) = lcd
            .position(
                &snapshot.route.contract_address,
                trader,
                trade_id,
                Read::Fresh,
            )
            .await?;
        require_object(&latest)?;
        if !same_account(trader, latest.get("user")) {
            return Err(invalid(CONTRACT_OWNER_MISMATCH));
        }
        if height.as_deref().is_some_and(|height| {
            !height.bytes().all(|byte| byte.is_ascii_digit())
                || height.trim_start_matches('0').is_empty()
        }) {
            return Err(height_error(
                "LCD returned an invalid contract query height.",
            ));
        }
        for key in [
            "user_trade_index",
            "market_index",
            "collateral_index",
            "collateral_amount",
            "leverage",
            "is_open",
            "trade_type",
            "long",
            "open_price",
        ] {
            if latest.get(key) != snapshot.raw.get(key) {
                return Ok(None);
            }
        }
        let (effective, current_inputs) =
            snapshot.leverage(lcd, trade_id, height.as_deref()).await?;
        if current_inputs != self.inputs {
            return Ok(None);
        }
        let height = height.as_deref().ok_or_else(|| invalid(
            "Cannot confirm the position and leverage at the same block. Retry with an LCD that reports the contract query height."
        ))?;
        let effective = effective.ok_or_else(|| SaiError::new(
            Code::PreviewMismatch,
            "Cannot verify effective leverage at the position's block. Request a new preview once the leverage inputs are readable.",
        ))?;
        let prices = if self.preview.intent.kind == "DECREASE_POSITION_SIZE" {
            let (prices, _) = lcd
                .smart(
                    &snapshot.route.contract_address,
                    json!({"get_perp_prices": {
                        "market_index": snapshot.raw["market_index"],
                        "collateral_index": snapshot.raw["collateral_index"],
                    }}),
                    Read::At(height),
                )
                .await?;
            Some(prices)
        } else {
            None
        };
        Ok(Some(RecheckedPosition {
            height: height.to_owned(),
            effective: effective.units,
            prices,
        }))
    }

    async fn estimate_execution(
        &mut self,
        lcd: &Lcd<'_>,
        id: &Value,
        action: PositionAction<'_>,
        rechecked: Option<&RecheckedPosition>,
    ) -> Result<(), SaiError> {
        let state = ExecutionSnapshot {
            raw: self.snapshot.raw.clone(),
            market: self.snapshot.market.clone(),
            route: self.snapshot.route.clone(),
            observed_at: now(),
        };
        let actual;
        let height = if let Some(rechecked) = rechecked {
            Ok(Some(rechecked.height.as_str()))
        } else {
            actual = state.verified_height(lcd, id).await;
            actual.as_ref().map(|height| height.as_deref())
        };
        match action {
            PositionAction::Margin { .. } => {
                let Some(target) = self.preview.intent.body["requestedLeverage"].as_str() else {
                    return Ok(());
                };
                self.preview.estimates = Some(if let Ok(Some(height)) = height {
                    let effective = ContractDecimal::parse(&self.inputs["effectiveLeverage"])
                        .ok()
                        .map(|value| value.units);
                    match leverage_execution_estimates(lcd, &state, id, height, target, effective)
                        .await
                    {
                        Ok((estimates, _)) => estimates,
                        Err(_) if rechecked.is_none() => super::update::unknown_execution_inputs(),
                        Err(_) => return Err(position_mismatch()),
                    }
                } else {
                    super::update::unknown_execution_inputs()
                });
            }
            PositionAction::PartialClose { .. } => {
                let Some(amount) = self.preview.intent.body["collateral"]["amountUsdc"]
                    .as_str()
                    .and_then(|amount| amount.parse::<BigUint>().ok())
                else {
                    return Ok(());
                };
                let rechecked = rechecked.and_then(|checked| {
                    checked
                        .prices
                        .as_ref()
                        .map(|prices| (checked.effective, prices))
                });
                self.preview.estimates = Some(
                    Trading::partial_close_estimates(
                        lcd,
                        &state,
                        id,
                        height,
                        PartialCloseQuote {
                            amount: &amount,
                            rechecked,
                        },
                    )
                    .await,
                );
            }
        }
        Ok(())
    }
}

impl Snapshot {
    async fn leverage(
        &self,
        lcd: &Lcd<'_>,
        trade_id: &Value,
        height: Option<&str>,
    ) -> Result<(Option<ContractDecimal>, Value), SaiError> {
        let stored = ContractDecimal::parse(field(&self.raw, "leverage")?)?;
        let mut inputs = json!({
            "marketIndex": self.raw["market_index"],
            "collateralIndex": self.raw["collateral_index"],
        });
        let leverage = match self
            .effective_leverage(lcd, trade_id, height, stored, &mut inputs)
            .await
        {
            Ok(value) => Some(value),
            Err(error) if error.code == Code::PreviewMismatch => {
                return Err(error);
            }
            Err(_) => None,
        };
        inputs["effectiveLeverage"] = json!(leverage.as_ref().map(ContractDecimal::display));
        Ok((leverage, inputs))
    }

    async fn effective_leverage(
        &self,
        lcd: &Lcd<'_>,
        trade_id: &Value,
        height: Option<&str>,
        stored: ContractDecimal,
        inputs: &mut Value,
    ) -> Result<ContractDecimal, SaiError> {
        let query = |message| async move {
            lcd.smart(
                &self.route.contract_address,
                message,
                height.map_or(Read::Snapshot, Read::At),
            )
            .await
            .map(|(value, _)| value)
        };
        let market_index = &self.raw["market_index"];
        let enabled = query(json!({"is_after_hours_market": {"market_index": market_index}}))
            .await?
            .as_bool()
            .ok_or_else(malformed)?;
        inputs["afterHoursMarket"] = json!(enabled);
        if !enabled {
            return Ok(stored);
        }
        let market = query(json!({"get_market": {"index": market_index}})).await?;
        let opened = optional_block(field(&market, "block_last_opened")?)?;
        let closed = optional_block(field(&market, "block_last_closed")?)?;
        inputs["marketLastOpenedBlock"] = json!(opened);
        inputs["marketLastClosedBlock"] = json!(closed);
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
            inputs["positionRiskBlock"] = json!(risk);
            if risk >= opened {
                return Ok(stored);
            }
        }
        let cap = query(json!({"get_after_hours_leverage": {}})).await?;
        let cap = ContractDecimal::parse(&cap)?;
        inputs["afterHoursLeverageCap"] = json!(cap.display());
        Ok(if stored.units <= cap.units {
            stored
        } else {
            cap
        })
    }
}

fn position_changed(
    action: PositionAction<'_>,
    addr: &str,
    latest: Result<PositionQuote, SaiError>,
    cli: bool,
) -> SaiError {
    let message = match action {
        PositionAction::Margin { .. } => "Position changed; review the margin change again",
        PositionAction::PartialClose { .. } => "Position changed; review the reduction again",
    };
    match latest {
        Ok(latest) => {
            let message = if cli {
                message
            } else {
                "The position, quote, or route changed during confirmation, so nothing was submitted. Show the user the latest preview, get their confirmation again, then retry with its previewDigest."
            };
            replacement_preview(message, &latest.preview, addr)
        }
        Err(error) if is_owner_mismatch(&error) || error.code == Code::PreviewMismatch => error,
        Err(error) => SaiError::new(
            Code::PreviewMismatch,
            format!(
                "{message}. Could not quote the latest position. {}",
                error.message
            ),
        ),
    }
}

fn replacement_preview(message: &str, preview: &TradePreview, addr: &str) -> SaiError {
    SaiError {
        details: Some(Box::new(variables([(
            "preview",
            preview.cli_preview_output(Some(addr)),
        )]))),
        ..SaiError::new(Code::PreviewMismatch, message)
    }
}

fn is_network_mismatch(error: &SaiError) -> bool {
    error.code == Code::PreviewMismatch && error.message == NETWORK_MISMATCH
}

fn is_owner_mismatch(error: &SaiError) -> bool {
    error.code == Code::InvalidRequest
        && matches!(
            error.message.as_str(),
            KEEPER_OWNER_MISMATCH | CONTRACT_OWNER_MISMATCH
        )
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

fn field<'a>(value: &'a Value, key: &str) -> Result<&'a Value, SaiError> {
    require_object(value)?;
    value
        .get(key)
        .ok_or_else(|| super::actions::incomplete("KeyError"))
}

fn text_field<'a>(value: &'a Value, key: &str) -> Result<&'a str, SaiError> {
    field(value, key)?.as_str().ok_or_else(malformed)
}

fn require_object(value: &Value) -> Result<(), SaiError> {
    if value.is_object() {
        Ok(())
    } else {
        Err(malformed())
    }
}

fn number_text(value: &Value) -> Result<String, SaiError> {
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

fn malformed() -> SaiError {
    super::actions::incomplete("TypeError")
}

fn invalid(message: &str) -> SaiError {
    SaiError::new(Code::InvalidRequest, message)
}

#[cfg(test)]
mod percent_tests {
    use super::parse_partial_close_percent;

    #[test]
    fn percentages_accept_plain_ascii_digits() {
        assert_eq!(parse_partial_close_percent("0050").unwrap(), 50);
        assert!(parse_partial_close_percent("5_0").is_err());
        assert!(parse_partial_close_percent("\u{0665}_\u{0660}").is_err());
        assert!(parse_partial_close_percent("\u{10d45}\u{10d40}").is_err());
    }
}
