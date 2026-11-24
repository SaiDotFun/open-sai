use num_bigint::BigInt;
use serde_json::{Map, Value, json};

use crate::{
    errors::{Code, SaiError},
    keeper::{MARKETS, TIMEOUT},
};

use super::{TradeIntent, TradePreview, Trading, confirmation::plain_decimal, positive_decimal};

pub const MARKET_PRICE_NOTICE: &str =
    "Market orders use the refreshed Keeper price; --price is a dry-run reference.";

#[derive(Clone, Copy)]
pub enum Side {
    Long,
    Short,
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub enum OrderType {
    Market,
    Limit,
    Stop,
}

pub struct OpenTrade<'a> {
    pub market: &'a str,
    pub side: Side,
    pub order_type: OrderType,
    pub amount: &'a str,
    pub leverage: &'a str,
    pub price: Option<&'a str>,
    pub slippage: &'a str,
    pub take_profit: Option<&'a str>,
    pub stop_loss: Option<&'a str>,
}

impl OpenTrade<'_> {
    pub fn preview(&self) -> Result<TradePreview, SaiError> {
        let market = id(self.market, "--market")?;
        if market.sign() == num_bigint::Sign::Minus {
            return Err(invalid("Market index must not be negative"));
        }
        let amount = positive_decimal(self.amount, "Amount")?;
        positive_decimal(self.leverage, "Leverage")?;
        if let Some(price) = self.price {
            positive_decimal(price, "Price")?;
        } else if self.order_type != OrderType::Market {
            return Err(invalid(
                "A trigger price is required for limit and stop orders",
            ));
        }
        if amount % 10u128.pow(12) != 0 {
            return Err(invalid("Amount must have at most six decimal places"));
        }
        if self.slippage != "0"
            && (positive_decimal(self.slippage, "Slippage")? > 100 * 10u128.pow(18)
                || self
                    .slippage
                    .split_once('.')
                    .is_some_and(|(_, digits)| digits.len() > 16))
        {
            return Err(invalid(
                "Slippage must be between 0 and 100 with at most 16 decimal places",
            ));
        }
        let mut body = json!({
            "market": {"marketIndex": format!("MarketIndex({market})")},
            "side": match self.side { Side::Long => "long", Side::Short => "short" },
            "orderType": match self.order_type {
                OrderType::Market => "market",
                OrderType::Limit => "limit",
                OrderType::Stop => "stop",
            },
            "amountUsd": self.amount,
            "leverage": self.leverage,
            "maxSlippagePct": self.slippage,
        });
        if let Some(price) = self.price {
            body["intentPrice"] = json!(price);
        }
        for (key, value) in [
            ("takeProfit", self.take_profit),
            ("stopLoss", self.stop_loss),
        ] {
            if let Some(value) = value {
                positive_decimal(value, key)?;
                body[key] = json!(value);
            }
        }
        Ok(TradePreview::new(TradeIntent::new(
            "OPEN_TRADE",
            None,
            body,
        )))
    }
}

pub fn close_preview(trade_id: &str, cancel: bool) -> Result<TradePreview, SaiError> {
    let trade_id = id(trade_id, "TRADE_ID")?;
    if trade_id.sign() == num_bigint::Sign::Minus {
        return Err(invalid("Trade id must not be negative"));
    }
    let kind = if cancel {
        "CANCEL_PENDING_TRADE"
    } else {
        "CLOSE_POSITION"
    };
    Ok(TradePreview::new(TradeIntent::new(
        kind,
        Some(trade_id.to_string()),
        json!({}),
    )))
}

impl Trading<'_> {
    /// Fetch the public price used by market previews and market submissions.
    pub async fn refresh_market_open(&self, preview: &mut TradePreview) -> Result<(), SaiError> {
        let market = preview.intent.body["market"]["marketIndex"]
            .as_str()
            .and_then(|text| text.strip_prefix("MarketIndex(")?.strip_suffix(')'))
            .ok_or_else(SaiError::internal)?;
        let market: Value = serde_json::from_str(market).map_err(|_| SaiError::internal())?;
        let data = self
            .keeper
            .request(MARKETS, Map::new(), None, TIMEOUT)
            .await?;
        let borrowings = data
            .get("perp")
            .and_then(|perp| perp.get("borrowings"))
            .and_then(Value::as_array)
            .ok_or_else(|| incomplete("KeyError"))?;
        let mut price = None;
        for row in borrowings {
            let market_id = row.get("marketId").ok_or_else(|| incomplete("KeyError"))?;
            if *market_id != market {
                continue;
            }
            let symbol = row
                .get("collateralToken")
                .and_then(|token| token.get("symbol"))
                .ok_or_else(|| incomplete("KeyError"))?;
            if symbol != "USDC"
                || !crate::keeper::truthy(row.get("isOpen").ok_or_else(|| incomplete("KeyError"))?)
            {
                continue;
            }
            if price.is_some() {
                return Err(invalid("Current open USDC market data is unavailable"));
            }
            price = Some(row.get("price").ok_or_else(|| incomplete("KeyError"))?);
        }
        let price = price.ok_or_else(|| invalid("Current open USDC market data is unavailable"))?;
        preview.reference_price = Some(decimal_price(price)?);
        Ok(())
    }
}

fn id(value: &str, parameter: &str) -> Result<BigInt, SaiError> {
    let invalid = || {
        SaiError::usage(format!(
            "Invalid value for '{parameter}': {} is not a valid integer.",
            crate::settings::repr(value)
        ))
    };
    let text = value.trim();
    let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
    if !digits
        .split('_')
        .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
    {
        return Err(invalid());
    }
    text.replace('_', "").parse().map_err(|_| invalid())
}

fn invalid(message: &str) -> SaiError {
    SaiError::new(Code::InvalidRequest, message)
}

fn incomplete(kind: &str) -> SaiError {
    SaiError::new(
        Code::IncompleteData,
        format!("Keeper or contract data was missing or malformed ({kind})"),
    )
}

/// Keeper prices may be JSON strings or numbers, with an optional exponent.
fn decimal_price(value: &Value) -> Result<String, SaiError> {
    let number;
    let text = if let Some(text) = value.as_str() {
        text
    } else if let Some(value) = value.as_number() {
        let raw = value.to_string();
        // Python's JSON reader parses fractional and exponent tokens as floats.
        number = if raw.contains(['.', 'e', 'E']) {
            let value = value
                .as_f64()
                .ok_or_else(|| invalid("Current price must be a positive finite decimal"))?;
            format!("{value:?}")
        } else {
            raw
        };
        &number
    } else {
        return Err(SaiError::internal());
    };
    let text = text.trim();
    let text = text.strip_prefix('+').unwrap_or(text);
    let (mantissa, exponent) = match text.split_once(['e', 'E']) {
        Some((mantissa, exponent)) => (
            mantissa,
            exponent.parse::<i32>().map_err(|_| SaiError::internal())?,
        ),
        None => (text, 0),
    };
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    if whole.starts_with('-')
        || text.eq_ignore_ascii_case("nan")
        || text.eq_ignore_ascii_case("infinity")
    {
        return Err(invalid("Current price must be a positive finite decimal"));
    }
    let digits = format!("{whole}{fraction}");
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(SaiError::internal());
    }
    let significant = digits.trim_start_matches('0');
    let fractional_places = fraction.len() as i64 - i64::from(exponent);
    if significant.is_empty()
        || fractional_places > 18
        || significant.len() as i64 - fractional_places > 21
    {
        return Err(invalid("Current price must be a positive finite decimal"));
    }
    Ok(plain_decimal(
        significant,
        significant.len() as i64 - fractional_places,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn market_price_formats_numbers_and_decimal_strings_like_python() {
        for (wire, expected) in [
            ("123.000", "123.0"),
            ("1e-7", "0.0000001"),
            ("1e16", "10000000000000000"),
            (r#""+001.00200E-2""#, "0.0100200"),
        ] {
            let value = serde_json::from_str(wire).unwrap();
            assert_eq!(decimal_price(&value).unwrap(), expected);
        }
    }
}
