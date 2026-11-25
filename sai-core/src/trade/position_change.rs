//! Contract quotes and confirmation for changes to an existing position.

mod quote;

use std::future::{Future, pending};

const KEEPER_OWNER_MISMATCH: &str = "Keeper position belongs to a different account";
const CONTRACT_OWNER_MISMATCH: &str = "Contract position belongs to a different account";
const NETWORK_MISMATCH: &str = "Keeper trading route and LCD network do not match";
const CONTRACT_POSITION_MISMATCH: &str = "Position changed. Review the margin change again.";

use serde_json::{Map, Value, json};

use crate::{
    errors::{Code, SaiError},
    keeper::{MARKET, MARKETS, POSITION, ROUTE, TIMEOUT, truthy},
    settings::repr,
};

use super::position::{Lcd, PositionRoute, Read, height_error, same_account};
use super::{Confirmation, SubmitOptions, TradePreview, Trading, actions::id};
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
    Submit(Option<Confirmation<'a>>),
}

pub fn parse_partial_close_percent(input: &str) -> Result<u8, SaiError> {
    let invalid = || {
        SaiError::usage(format!(
            "Invalid value for '--percent': {} is not a valid integer range.",
            repr(input),
        ))
    };
    let text = input.trim();
    let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
    if digits.split('_').any(str::is_empty) {
        return Err(invalid());
    }
    let digits: String = digits
        .chars()
        .filter(|&digit| digit != '_')
        .map(decimal_digit)
        .collect::<Option<_>>()
        .ok_or_else(invalid)?;
    if digits.len() > 4300 {
        return Err(invalid());
    }
    let significant = digits.trim_start_matches('0');
    let normalized = if significant.is_empty() {
        "0"
    } else {
        significant
    };
    let negative = text.starts_with('-') && normalized != "0";
    normalized
        .parse::<u8>()
        .ok()
        .filter(|percent| !negative && (1..=99).contains(percent))
        .ok_or_else(|| {
            let sign = if negative { "-" } else { "" };
            SaiError::usage(format!(
                "Invalid value for '--percent': {sign}{normalized} is not in the range 1<=x<=99."
            ))
        })
}

fn decimal_digit(digit: char) -> Option<char> {
    if digit.is_ascii() {
        return digit.is_ascii_digit().then_some(digit);
    }
    // The zeroes of the Unicode 14 decimal ranges used by the Python 3.11
    // reference. Every range contains ten consecutive digits.
    const ZEROES: &[u32] = &[
        0x0030, 0x0660, 0x06f0, 0x07c0, 0x0966, 0x09e6, 0x0a66, 0x0ae6, 0x0b66, 0x0be6, 0x0c66,
        0x0ce6, 0x0d66, 0x0de6, 0x0e50, 0x0ed0, 0x0f20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946,
        0x19d0, 0x1a80, 0x1a90, 0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0,
        0xa9f0, 0xaa50, 0xabf0, 0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0,
        0x112f0, 0x11450, 0x114d0, 0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50,
        0x11da0, 0x16a60, 0x16ac0, 0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140,
        0x1e2f0, 0x1e950, 0x1fbf0,
    ];
    let point = u32::from(digit);
    let previous = ZEROES.partition_point(|&zero| zero <= point) - 1;
    let offset = point - ZEROES[previous];
    (offset < 10).then(|| char::from(b'0' + offset as u8))
}

struct Snapshot {
    raw: Value,
    funds: Value,
    market: Value,
    route: PositionRoute,
}

struct PositionQuote {
    snapshot: Snapshot,
    inputs: Value,
    preview: TradePreview,
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
        let confirming = matches!(&mode, PositionMode::Submit(Some(_)));
        let first = self
            .quote_for_position(&lcd, addr, &id, action, confirming)
            .await?;
        let PositionMode::Submit(confirmation) = mode else {
            return Ok(match mode {
                PositionMode::Preview => first.preview.confirmed_output(Some(addr)),
                _ => first.preview.output(Some(addr)),
            });
        };

        let cli = confirmation
            .as_ref()
            .is_none_or(Confirmation::is_position_cli);
        let current = self
            .quote_for_position(&lcd, addr, &id, action, confirming)
            .await?;
        let changed = !current.unchanged(&lcd, &id).await?;
        if changed
            || current.snapshot.route != first.snapshot.route
            || current.preview.output(None) != first.preview.output(None)
        {
            let latest = if changed {
                self.quote_for_position(&lcd, addr, &id, action, confirming)
                    .await
            } else {
                Ok(current)
            };
            return Err(position_changed(action, addr, latest, cli));
        }
        let (accepted, _) = self
            .submit_with_reference_price(
                &session,
                &current.preview,
                SubmitOptions {
                    confirmation,
                    ..SubmitOptions::default()
                },
                interrupted,
            )
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
            Ok(fresh) => return Err(replacement_preview(
                "Keeper trading route and LCD network differed during confirmation. Review the replacement preview before submitting.",
                &fresh.preview, addr,
            )),
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
            .read_position_change_snapshot(lcd, addr, trade_id)
            .await?;
        let delta = if let PositionAction::PartialClose { percent } = action {
            let markets = self.position_request(MARKETS, Map::new()).await?;
            let markets = field(field(&markets, "perp")?, "borrowings")?
                .as_array()
                .ok_or_else(malformed)?;
            let mut matches = markets.iter().filter(|market| {
                market["marketId"] == snapshot.market["marketId"]
                    && market["collateralToken"]["symbol"] == "USDC"
            });
            let market = matches
                .next()
                .filter(|market| matches.next().is_none() && truthy(&market["isOpen"]))
                .ok_or_else(|| invalid("Open market data not available"))?;
            let minimum = field(market, "minPositionSizeUSD")?;
            Some(quote::reduction(&snapshot.raw, percent, minimum)?)
        } else {
            None
        };

        let (effective, mut inputs) = snapshot.leverage(lcd, trade_id, None).await?;
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
            PositionAction::PartialClose { percent } => {
                let price = snapshot.price(lcd, None).await.ok();
                inputs["collateralPriceUsd"] = json!(price.as_ref().map(ContractDecimal::display));
                quote::partial(
                    trade_id,
                    &snapshot.raw,
                    percent,
                    &delta.ok_or_else(SaiError::internal)?,
                    effective.as_ref(),
                    price.as_ref(),
                    inputs.clone(),
                )?
            }
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
    ) -> Result<Snapshot, SaiError> {
        let route = self.position_request(ROUTE, Map::new()).await?;
        let route = field(&route, "oneClickTrading")?;
        if route["enabled"] != true {
            return Err(invalid(NETWORK_MISMATCH));
        }
        let position = self
            .position_request(
                POSITION,
                variables([("trader", json!(addr)), ("id", trade_id.clone())]),
            )
            .await?;
        let position = field(field(&position, "perp")?, "trade")?;
        if !truthy(position) {
            return Err(invalid("Position not found for this account"));
        }
        if !same_account(addr, position.get("trader")) {
            return Err(invalid(KEEPER_OWNER_MISMATCH));
        }
        if position.get("id") != Some(trade_id) {
            return Err(invalid("Keeper returned a different position index"));
        }
        if !truthy(field(position, "isOpen")?) || field(position, "tradeType")? != "trade" {
            return Err(invalid("Open position not found"));
        }
        let trader = text_field(position, "trader")?;
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
        let borrowing = field(position, "perpBorrowing")?;
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
            || !truthy(field(market, "isOpen")?)
        {
            return Err(invalid("Open market data is unavailable"));
        }
        let token = format!("TokenIndex({token_id})");
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
            return Err(SaiError::new(
                Code::PreviewMismatch,
                CONTRACT_POSITION_MISMATCH,
            ));
        }
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
            return Err(SaiError::new(
                Code::PreviewMismatch,
                CONTRACT_POSITION_MISMATCH,
            ));
        }
        if number_text(&raw["collateral_amount"])?
            != number_text(field(position, "collateralAmount")?)?
            || float(&raw["leverage"])? != float(field(position, "leverage")?)?
        {
            return Err(invalid(
                "Indexed position differs from contract state; wait for synchronization and retry",
            ));
        }
        Ok(Snapshot {
            raw,
            funds,
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

impl PositionQuote {
    async fn unchanged(&self, lcd: &Lcd<'_>, trade_id: &Value) -> Result<bool, SaiError> {
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
        ] {
            if latest.get(key) != snapshot.raw.get(key) {
                return Ok(false);
            }
        }
        let (_, mut current_inputs) = snapshot.leverage(lcd, trade_id, height.as_deref()).await?;
        let mut quoted = self.inputs.clone();
        let price = quoted
            .as_object_mut()
            .and_then(|fields| fields.shift_remove("collateralPriceUsd"));
        if current_inputs != quoted {
            return Ok(false);
        }
        let height = height.as_deref().ok_or_else(|| invalid(
            "Cannot confirm the position and leverage at the same block. Retry with an LCD that reports the contract query height."
        ))?;
        if price.is_some() {
            current_inputs["collateralPriceUsd"] =
                json!(snapshot.price(lcd, Some(height)).await?.display());
        }
        Ok(current_inputs == self.inputs)
    }
}

impl Snapshot {
    async fn price(
        &self,
        lcd: &Lcd<'_>,
        height: Option<&str>,
    ) -> Result<ContractDecimal, SaiError> {
        let (value, _) = lcd
            .smart(
                &self.route.contract_address,
                json!({"get_collateral_price": {
                    "collateral_index": self.raw["collateral_index"],
                }}),
                height.map_or(Read::Fresh, Read::At),
            )
            .await?;
        ContractDecimal::parse(&value)
    }

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
            preview.confirmed_output(Some(addr)),
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
    fn percentages_use_the_python_311_decimal_digit_repertoire() {
        assert_eq!(
            parse_partial_close_percent("\u{0665}_\u{0660}").unwrap(),
            50
        );
        // Unicode 16 added Garay digits. Python 3.11 rejects them.
        assert!(parse_partial_close_percent("\u{10d45}\u{10d40}").is_err());
    }
}
