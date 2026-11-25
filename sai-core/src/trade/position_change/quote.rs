use num_bigint::{BigInt, BigUint, Sign};
use serde_json::{Value, json};

use super::{MarginDirection, Snapshot, field, invalid, malformed, number_text};
use crate::{
    errors::SaiError,
    trade::{TradeIntent, TradePreview, positive_decimal},
};

const SCALE: u128 = 10u128.pow(18);
const MICROS: u32 = 1_000_000;
const DECIMAL_PRECISION: usize = 28;
const MARGIN_WARNING: &str = "The request authorizes target leverage, not an amount cap; the actual margin change may differ if the position changes before execution.";

pub(super) struct ContractDecimal {
    pub units: u128,
    raw: String,
}

impl ContractDecimal {
    pub(super) fn parse(value: &Value) -> Result<Self, SaiError> {
        let raw = value.as_str().ok_or_else(malformed)?;
        let units = if raw == "0" {
            0
        } else {
            positive_decimal(raw, "Contract quote input")?
        };
        Ok(Self {
            units,
            raw: raw.into(),
        })
    }

    pub(super) fn display(&self) -> String {
        if let Some(fraction) = self.raw.strip_prefix("0.")
            && let Some(first) = fraction.bytes().position(|digit| digit != b'0')
            && first >= 6
        {
            let (leading, trailing) = fraction[first..].split_at(1);
            let mantissa = if trailing.is_empty() {
                leading.to_owned()
            } else {
                format!("{leading}.{trailing}")
            };
            return format!("{mantissa}E-{}", first + 1);
        }
        self.raw.clone()
    }
}

pub(super) fn margin(
    trade_id: &Value,
    snapshot: &Snapshot,
    direction: MarginDirection,
    amount: &str,
    effective: &ContractDecimal,
    mut inputs: Value,
) -> Result<TradePreview, SaiError> {
    let Snapshot {
        raw, funds, market, ..
    } = snapshot;
    let amount = round_decimal(BigUint::from(positive_decimal(amount, "Margin amount")?));
    let amount_scale = SCALE / u128::from(MICROS);
    if &amount % amount_scale != BigUint::ZERO {
        return Err(invalid(
            "Margin amount must have at most six decimal places",
        ));
    }
    let delta = amount / amount_scale;
    let collateral = integer(field(raw, "collateral_amount")?)?;
    let stored = field(raw, "leverage")?.as_str().ok_or_else(malformed)?;
    let stored_units = positive_decimal(stored, "Current leverage")?;
    if effective.units == 0 {
        return Err(invalid(
            "Cannot quote a margin change with zero effective leverage",
        ));
    }
    if collateral.sign() != Sign::Plus {
        return Err(invalid("Invalid margin input"));
    }
    let collateral = collateral.magnitude();
    let adding = direction == MarginDirection::Add;
    if !adding && &delta >= collateral {
        return Err(invalid(
            "Keep some margin in the position; close the position to exit",
        ));
    }
    let target = if adding {
        collateral + &delta
    } else {
        collateral - &delta
    };
    let numerator = collateral * effective.units;
    let leverage = if adding {
        (&numerator + &target - 1u32) / &target
    } else {
        &numerator / &target
    };
    let old = BigUint::from(effective.units);
    if (adding && leverage > old) || (!adding && leverage < old) {
        return Err(invalid(
            "Target leverage would reverse the requested margin direction",
        ));
    }
    let minimum = ratio(field(market, "minLeverage")?)?;
    let maximum = ratio(field(market, "maxLeverage")?)?;
    if leverage == BigUint::ZERO
        || compare_ratio(&leverage, &maximum, SCALE).is_gt()
        || (adding && compare_ratio(&leverage, &minimum, SCALE).is_lt())
    {
        return Err(invalid("Resulting leverage is outside market limits"));
    }
    let after = &numerator / &leverage;
    let actual = match adding {
        true if &after > collateral => &after - collateral,
        false if collateral > &after => collateral - &after,
        _ => BigUint::ZERO,
    };
    if actual == BigUint::ZERO || actual > delta || leverage == old {
        return Err(invalid("Margin change is too small after rounding"));
    }
    if adding && BigInt::from(actual.clone()) > integer(field(funds, "available_to_trade")?)? {
        return Err(invalid("Insufficient balance on Sai"));
    }
    if trade_id.to_string().starts_with('-') {
        return Err(invalid("Trade id must not be negative"));
    }
    let leverage = fixed(&leverage, 18);
    positive_decimal(&leverage, "Leverage")?;
    let mut preview = TradePreview::new(TradeIntent::new(
        "UPDATE_TRADE",
        Some(trade_id.to_string()),
        json!({
            "takeProfit": {"operation": "keep"},
            "stopLoss": {"operation": "keep"},
            "requestedLeverage": leverage,
        }),
    ));
    inputs["requestedLeverage"] = json!(leverage);
    inputs["collateralBefore"] = json!(collateral.to_string());
    inputs["leverageBefore"] = json!(stored);
    inputs["collateralAfter"] = json!(after.to_string());
    inputs["actualDeltaUsdc"] = json!(actual.to_string());
    inputs["marginChangeIsEstimate"] = json!(effective.units < stored_units);
    preview.margin_change = true;
    preview.quote = Some(inputs);
    preview.warning = Some(MARGIN_WARNING);
    Ok(preview)
}

fn fixed(amount: &BigUint, scale: usize) -> String {
    let padded = format!("{amount:0>width$}", width = scale + 1);
    let (whole, fraction) = padded.split_at(padded.len() - scale);
    let fraction = fraction.trim_end_matches('0');
    if fraction.is_empty() {
        whole.into()
    } else {
        format!("{whole}.{fraction}")
    }
}

pub(super) fn reduction(
    raw: &Value,
    percent: u8,
    market_minimum: &Value,
) -> Result<BigUint, SaiError> {
    if !(1..=99).contains(&percent) {
        return Err(invalid("Partial close percentage must be from 1 to 99"));
    }
    let collateral = integer(field(raw, "collateral_amount")?)?;
    let leverage = ratio(field(raw, "leverage")?)?;
    let minimum = ratio(market_minimum)?;
    let fail = || invalid("Reduction is too small or leaves less than the market minimum");
    if collateral.sign() != Sign::Plus {
        return Err(fail());
    }
    let collateral = collateral.magnitude();
    let scaled = round_decimal(collateral * percent);
    let delta = &scaled / 100u32;
    if delta == BigUint::ZERO {
        return Err(fail());
    }
    let remaining = round_decimal(collateral - &delta);
    let notional = round_decimal(remaining * leverage.0.magnitude());
    let notional = BigInt::from_biguint(leverage.0.sign(), notional);
    if notional * BigInt::from(minimum.1) < minimum.0 * BigInt::from(leverage.1) * MICROS {
        return Err(fail());
    }
    // Decimal division preserves the rounded product's exponent when its
    // trailing zeroes permit it. Python's later int() rejects that notation.
    let scaled = scaled.to_str_radix(10);
    let trailing_zeroes = scaled.len() - scaled.trim_end_matches('0').len();
    let exponent = scaled
        .len()
        .saturating_sub(DECIMAL_PRECISION)
        .min(trailing_zeroes.saturating_sub(2));
    if exponent > 0 {
        let digits = delta.to_str_radix(10);
        let (whole, fraction) = digits[..digits.len() - exponent].split_at(1);
        return Err(invalid(&format!(
            "invalid literal for int() with base 10: '{whole}.{fraction}E+{}'",
            digits.len() - 1
        )));
    }
    Ok(delta)
}

pub(super) fn partial(
    trade_id: &Value,
    raw: &Value,
    percent: u8,
    delta: &BigUint,
    effective: Option<&ContractDecimal>,
    price: Option<&ContractDecimal>,
    mut inputs: Value,
) -> Result<TradePreview, SaiError> {
    let stored = field(raw, "leverage")?;
    let leverage = match effective {
        Some(effective) => effective.units,
        None => ContractDecimal::parse(stored)?.units,
    };
    let size = delta * leverage / SCALE;
    inputs["percent"] = json!(percent);
    inputs["collateralBefore"] = json!(number_text(field(raw, "collateral_amount")?)?);
    inputs["leverageBefore"] = json!(number_text(stored)?);
    inputs["closedNotionalUsdc"] = json!(fixed(&size, 6));
    inputs["closedNotionalUsd"] =
        json!(price.map(|price| fixed(&(&size * price.units / SCALE), 6)));
    inputs["closedNotionalIsEstimate"] = json!(effective.is_none() || price.is_none());

    let mut preview = TradePreview::new(TradeIntent::new(
        "DECREASE_POSITION_SIZE",
        Some(trade_id.to_string()),
        json!({
            "collateral": {"amountUsdc": delta.to_string(), "amountUsd": fixed(delta, 6)},
            "leverageDelta": "0",
        }),
    ));
    preview.quote = Some(inputs);
    Ok(preview)
}

fn integer(value: &Value) -> Result<BigInt, SaiError> {
    number_text(value)?
        .trim()
        .parse()
        .map_err(|_| invalid("Invalid contract integer"))
}

// Python Decimal multiplies and subtracts with 28 significant digits before
// applying the explicit floor in the reduction and converting margin to micros.
fn round_decimal(value: BigUint) -> BigUint {
    let digits = value.to_str_radix(10).len();
    if digits <= DECIMAL_PRECISION {
        return value;
    }
    let unit = BigUint::from(10u32).pow((digits - DECIMAL_PRECISION) as u32);
    let mut quotient = &value / &unit;
    let doubled = (value % &unit) * 2u32;
    if doubled > unit || (doubled == unit && quotient.bit(0)) {
        quotient += 1u32;
    }
    quotient * unit
}

// GraphQL decimal fields may be JSON numbers or strings, including exponents.
fn ratio(value: &Value) -> Result<(BigInt, BigUint), SaiError> {
    let text = number_text(value)?;
    let (mantissa, exponent) = match text.split_once(['e', 'E']) {
        Some((mantissa, exponent)) => (
            mantissa,
            exponent
                .parse::<i32>()
                .ok()
                .filter(|exponent| exponent.abs_diff(0) <= 400)
                .ok_or_else(SaiError::internal)?,
        ),
        None => (text.as_str(), 0),
    };
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let coefficient = format!("{whole}{fraction}")
        .parse::<BigInt>()
        .map_err(|_| SaiError::internal())?;
    let scale = i32::try_from(fraction.len()).map_err(|_| SaiError::internal())? - exponent;
    let factor = BigUint::from(10u32).pow(scale.unsigned_abs());
    if scale < 0 {
        Ok((coefficient * BigInt::from(factor), BigUint::from(1u32)))
    } else {
        Ok((coefficient, factor))
    }
}

fn compare_ratio(
    units: &BigUint,
    other: &(BigInt, BigUint),
    multiplier: u128,
) -> std::cmp::Ordering {
    let lhs = BigInt::from(units * &other.1);
    lhs.cmp(&(&other.0 * multiplier))
}

#[cfg(test)]
mod tests {
    use super::*;

    const ACCOUNT: &str = "0x1111111111111111111111111111111111111111";

    #[test]
    fn reduction_refuses_only_when_decimal_would_render_an_exponent() {
        let raw = |collateral| json!({"collateral_amount": collateral, "leverage": "2.0"});
        let failure = reduction(&raw("1000000000000000000000000000"), 50, &json!(1)).unwrap_err();
        assert_eq!(
            failure.message,
            "invalid literal for int() with base 10: '5.0000000000000000000000000E+26'"
        );
        let delta = reduction(&raw("10000000000000000000000000020"), 1, &json!(1)).unwrap();
        assert_eq!(delta.to_string(), "100000000000000000000000000");
    }

    #[test]
    fn margin_digest_matches_python_for_a_carried_position() {
        let snapshot = Snapshot {
            raw: json!({"collateral_amount": "10000003", "leverage": "2.123456789012345678"}),
            funds: json!({"available_to_trade": "10000000"}),
            market: json!({"minLeverage": 1, "maxLeverage": 100}),
            route: crate::trade::position::PositionRoute {
                chain_id: String::new(),
                contract_address: String::new(),
            },
        };
        let cap = "1.234567890123456789";
        let effective = ContractDecimal::parse(&json!(cap)).unwrap();
        let inputs = json!({
            "marketIndex": "MarketIndex(3)",
            "collateralIndex": "TokenIndex(1)",
            "afterHoursMarket": true,
            "marketLastOpenedBlock": 110,
            "marketLastClosedBlock": 100,
            "positionRiskBlock": 90,
            "afterHoursLeverageCap": cap,
            "effectiveLeverage": cap,
        });
        let preview = margin(
            &json!(7),
            &snapshot,
            MarginDirection::Add,
            "1",
            &effective,
            inputs,
        )
        .unwrap();
        assert_eq!(
            preview.confirmed_output(Some(ACCOUNT))["previewDigest"],
            "9802cf3c7f2700d76cee2290b3799fbe054977bbe3fe3adf4c40fe4711db2b8e"
        );
    }

    #[test]
    fn partial_close_digest_binds_leverage_with_the_same_intent() {
        let price = ContractDecimal::parse(&json!("1")).unwrap();
        let confirmation = |leverage| {
            let raw = json!({"collateral_amount": "10000000", "leverage": leverage});
            let effective = ContractDecimal::parse(&raw["leverage"]).unwrap();
            let delta = reduction(&raw, 50, &json!(1)).unwrap();
            let inputs = json!({
                "marketIndex": "MarketIndex(3)",
                "collateralIndex": "TokenIndex(1)",
                "afterHoursMarket": false,
                "effectiveLeverage": leverage,
                "collateralPriceUsd": "1",
            });
            partial(
                &json!(7),
                &raw,
                50,
                &delta,
                Some(&effective),
                Some(&price),
                inputs,
            )
            .unwrap()
            .confirmed_output(Some(ACCOUNT))
        };
        let initial = confirmation("2.0");
        let changed = confirmation("3.0");
        assert_eq!(initial["intent"], changed["intent"]);
        assert_eq!(
            initial["previewDigest"],
            "5c6f75369d893d6e8715bd618b8efbbdd3b4e61b43eeb3f437f67d4f01126797"
        );
        assert_eq!(
            changed["previewDigest"],
            "a7dfedb9c7425f66444d65729a1df28c28aaaa49c68c5822863fd5edde3eb85a"
        );
    }
}
