//! Preview-only estimates. Confirmation binds the inputs in `digest_bytes`.

mod close;
mod lcd;
pub(super) use close::PartialCloseQuote;

use std::time::Duration;

use chrono::{SecondsFormat, Utc};
use num_bigint::BigUint;
use serde_json::{Value, json};

use super::{
    TradePreview, Trading,
    actions::decimal_price,
    cancel,
    confirmation::{plain_decimal, positive_decimal},
    position::{Lcd, Read},
};
use crate::{errors::SaiError, storage::Session};

const SCALE: u128 = 10u128.pow(18);
const USDC_PLACES: u32 = 6;
const QUOTE_TIMEOUT: Duration = Duration::from_secs(3);
const FEE_UNAVAILABLE: &str = "Perp fee schedule or collateral oracle price is unavailable.";
const COLLATERAL_ORACLE: &str = "Sai Perp collateral oracle";
const KEEPER_PRICE: &str = "Sai Keeper market price";

#[derive(Clone, Copy)]
pub enum PreviewMode {
    Cli,
    CliSubmission,
    Mcp,
    McpSubmission,
}

impl Trading<'_> {
    /// Add estimates after validating the intent. The optional session pins the
    /// account for position reads. Cancel previews require a valid session.
    pub async fn prepare_preview(
        &self,
        mut preview: TradePreview,
        session: Option<&Session>,
        mode: PreviewMode,
    ) -> Result<TradePreview, SaiError> {
        let market_row =
            if preview.requires_market_price_refresh() && matches!(mode, PreviewMode::Mcp) {
                let row = self.refresh_market_quote(&mut preview).await?;
                positive_decimal(
                    preview
                        .reference_price
                        .as_deref()
                        .ok_or_else(SaiError::internal)?,
                    "Current price",
                )?;
                Some(row)
            } else {
                None
            };
        if preview.intent.kind == "CANCEL_PENDING_TRADE" {
            let session = session.ok_or_else(SaiError::internal)?;
            self.storage
                .check_active_trade(session, self.keeper.endpoint())?;
            preview.position_route = self
                .basic_trade_route(session, &preview, false, None)
                .await?;
        } else if preview.is_market_close()
            && let Some(session) = session
        {
            self.storage
                .check_active_trade(session, self.keeper.endpoint())?;
        }
        self.estimate_preview(&mut preview, session, mode, market_row)
            .await?;
        Ok(preview)
    }

    pub(super) async fn estimate_preview(
        &self,
        preview: &mut TradePreview,
        session: Option<&Session>,
        mode: PreviewMode,
        market_row: Option<Value>,
    ) -> Result<(), SaiError> {
        preview.estimates = match preview.intent.kind {
            "OPEN_TRADE" => Some(self.open_estimates(preview, mode, market_row).await),
            "CLOSE_POSITION" => Some(self.close_estimates(preview, session).await?),
            "CANCEL_PENDING_TRADE" => Some(cancel::estimates()),
            _ => None,
        };
        Ok(())
    }

    async fn open_estimates(
        &self,
        preview: &mut TradePreview,
        mode: PreviewMode,
        mut market_row: Option<Value>,
    ) -> Value {
        let market = preview.requires_market_price_refresh();
        let mcp_market = market && matches!(mode, PreviewMode::Mcp | PreviewMode::McpSubmission);
        let submitting = matches!(
            mode,
            PreviewMode::CliSubmission | PreviewMode::McpSubmission
        );
        if market_row.is_none() && self.lcd_endpoint.is_some() && !(market && submitting) {
            market_row = self.open_market_row(preview, QUOTE_TIMEOUT).await.ok();
        }
        if preview.position_route.is_none()
            && let Some(endpoint) = self.lcd_endpoint
        {
            preview.position_route = self.position_route(&Lcd(endpoint), false).await.ok();
        }
        let body = &preview.intent.body;
        let supplied = body["intentPrice"].as_str();
        let keeper_price = market_row
            .as_ref()
            .and_then(|row| decimal_price(&row["price"]).ok())
            .filter(|price| positive_decimal(price, "Current price").is_ok());
        let (price, source) = match (market, mode, supplied) {
            (false, _, price) => (price, "Order trigger"),
            (true, PreviewMode::Cli, Some(price)) => (Some(price), "CLI --price"),
            _ => (keeper_price.as_deref(), KEEPER_PRICE),
        };
        if market && matches!(mode, PreviewMode::Cli) {
            preview.reference_price = price.map(str::to_owned);
        }
        let reference =
            price.and_then(|price| Price::new(price, source, (source == KEEPER_PRICE).then(now)));
        let long = body["side"] == "long";
        let slippage = decimal(&body["maxSlippagePct"]).expect("validated slippage");
        let market_bound = market && (mcp_market || preview.position_route.is_some());
        let mut fill = worst_case_fill(reference.as_ref(), long, slippage, market, market_bound);
        if market_bound && !mcp_market && fill["status"] == "bound" {
            fill["assumption"] = json!("Submit with --preview-digest and the quoted --price.");
        }
        let mut result = json!({
            "fees": unavailable(FEE_UNAVAILABLE),
            "liquidationPrice": unavailable("An entry price and opening fee estimate are required."),
            "worstCaseFill": fill,
        });
        if let Some(endpoint) = self.lcd_endpoint {
            self.quote_open(
                endpoint,
                preview,
                market_row.as_ref(),
                reference.as_ref(),
                &mut result,
                if submitting {
                    Read::Fresh
                } else {
                    Read::Snapshot
                },
            )
            .await;
        }
        result
    }

    async fn quote_open(
        &self,
        endpoint: &str,
        preview: &TradePreview,
        row: Option<&Value>,
        reference: Option<&Price>,
        result: &mut Value,
        market_read: Read<'_>,
    ) -> Option<()> {
        let contract = preview.position_route.as_ref()?.contract_address.clone();
        let lcd = lcd::QuoteLcd::new(endpoint, contract);
        let body = &preview.intent.body;
        let market = body["market"].get("marketIndex")?;
        let pair = lcd.market(market, market_read).await?;
        let pending = body["orderType"] != "market";
        if let Some(reason) = market_rejection(&pair, !pending) {
            *result = all_unavailable(reason);
            result["execution"] =
                json!({"status": "would_be_rejected", "code": "MarketClosed", "reason": reason});
            return Some(());
        }
        let row = row?;
        let token = json_index(row.get("collateralToken")?.get("id")?)?;
        let group = lcd.group(&pair, None).await?;
        let minimum = decimal(&group["min_leverage"])?;
        let mut maximum = decimal(&group["max_leverage"])?;
        let custom = lcd.pair_leverage_cap(row.get("marketId")?).await?;
        if custom != 0 {
            maximum = maximum.min(custom);
        }
        let leverage = decimal(&body["leverage"])?;
        let schedule = lcd.fees(&pair, None).await?;
        let quote = lcd
            .smart(
                json!({"get_collateral_price": {"collateral_index": format!("TokenIndex({token})")}}),
                None,
            )
            .await
            .ok()?;
        let price = Price::new(quote.as_str()?, COLLATERAL_ORACLE, Some(now()))?;
        let collateral = BigUint::from(decimal(&body["amountUsd"])? / 10u128.pow(18 - USDC_PLACES));
        let notional = &collateral * leverage / SCALE;
        // Perp floors USD notional before dividing by leverage.
        let collateral_value = (&notional * price.units / SCALE) * SCALE / leverage;
        if leverage < minimum
            || leverage > maximum
            || collateral_value < minimum_fee_usd(&schedule)? * 5u32
        {
            let outside = unavailable(
                "The open request is outside the market's collateral or leverage limits.",
            );
            result["fees"] = outside.clone();
            result["liquidationPrice"] = outside;
            return Some(());
        }
        let (fee, fees) = base_fees(&schedule, &price, notional, true, pending, None)?;
        result["fees"] = fees;
        if let Some(reference) = reference {
            result["liquidationPrice"] = open_liquidation(
                reference,
                body["side"] == "long",
                &collateral,
                &fee,
                leverage,
            );
        }
        Some(())
    }
}

struct Price {
    units: u128,
    output: Value,
}

impl Price {
    fn new(text: &str, source: &str, observed_at: Option<String>) -> Option<Self> {
        Some(Self {
            units: positive_decimal(text, "Quote price").ok()?,
            output: json!({"price": text, "source": source, "observedAt": observed_at}),
        })
    }
}

pub(super) fn now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true)
}

fn decimal(value: &Value) -> Option<u128> {
    match value.as_str()? {
        "0" => Some(0),
        text => positive_decimal(text, "Contract quote input").ok(),
    }
}

fn json_index(value: &Value) -> Option<String> {
    // Keeper represents indices as JSON integers. The wrapped LCD input is a string.
    value.as_u64().map(|index| index.to_string())
}

fn fixed(units: &BigUint, places: u32) -> String {
    if units.bits() == 0 {
        return "0".into();
    }
    let digits = units.to_string();
    plain_decimal(
        digits.trim_end_matches('0'),
        digits.len() as i64 - i64::from(places),
    )
}

fn unavailable(reason: &str) -> Value {
    json!({"status": "unavailable", "reason": reason})
}

fn not_applicable(reason: &str) -> Value {
    json!({"status": "not_applicable", "reason": reason})
}

fn all_unavailable(reason: &str) -> Value {
    let field = unavailable(reason);
    json!({"fees": field, "liquidationPrice": field, "worstCaseFill": field})
}

pub(super) fn market_rejection(pair: &Value, market: bool) -> Option<&'static str> {
    match pair["market_trading_state"].as_str()? {
        "close_only" => Some("Perp rejects this request in a close-only market."),
        "closed" if market => Some("Perp rejects this request in a closed market."),
        _ => None,
    }
}

pub(super) fn oracle_publication_time(oracle: &Value) -> Option<Option<u64>> {
    let oracle = oracle.as_object()?;
    let mut published = None;
    for key in ["base_last_update_time", "quote_last_update_time"] {
        if let Some(value) = oracle.get(key).filter(|value| !value.is_null()) {
            let time = value.as_u64()?;
            published = Some(published.map_or(time, |prior: u64| prior.min(time)));
        }
    }
    Some(published)
}

fn worst_case_fill(
    reference: Option<&Price>,
    long: bool,
    slippage: u128,
    market: bool,
    market_bound: bool,
) -> Value {
    let Some(reference) = reference else {
        return unavailable("Keeper market price is unavailable.");
    };
    let mut price = BigUint::from(reference.units);
    // A confirmed market order allows a move before submission and at execution.
    for _ in 0..if market_bound { 2 } else { 1 } {
        let distance = &price * slippage / (100 * SCALE);
        if long {
            price += distance;
        } else {
            price -= distance;
        }
    }
    json!({
        "status": if market && !market_bound { "estimate" } else { "bound" },
        "price": fixed(&price, 18),
        "side": if long { "maximum" } else { "minimum" },
        "reference": reference.output,
    })
}

fn liquidation_fee_units(collateral: &BigUint) -> BigUint {
    (collateral * 5u32 / 100u32) * 2u32
}

fn liquidation_fee(collateral: &BigUint) -> String {
    let fee = liquidation_fee_units(collateral);
    // Python's Decimal.scaleb uses its default 28-digit, half-even context here.
    let excess = fee.to_string().len().saturating_sub(28) as u32;
    if excess == 0 {
        return fixed(&fee, USDC_PLACES);
    }
    let divisor = BigUint::from(10u32).pow(excess);
    let mut rounded = &fee / &divisor;
    let double_remainder = (fee % &divisor) * 2u32;
    if double_remainder > divisor || (double_remainder == divisor && rounded.bit(0)) {
        rounded += 1u32;
    }
    fixed(&(rounded * divisor), USDC_PLACES)
}

fn open_liquidation(
    entry: &Price,
    long: bool,
    collateral: &BigUint,
    fee: &BigUint,
    leverage: u128,
) -> Value {
    if collateral <= fee || leverage == 0 {
        return unavailable("The estimated remaining collateral or leverage is zero.");
    }
    let remaining = collateral - fee;
    let price = BigUint::from(entry.units);
    // Keep each on-chain floor. Collateral and its liquidation fee are in base
    // units. The final multiplication by the 90% threshold floors at one atom.
    let price_per_unit = (&price / &remaining) * SCALE / leverage;
    let distance =
        price_per_unit * (&remaining * 90u32 - liquidation_fee_units(&remaining) * 100u32) / 100u32;
    let trigger = if !long {
        price + distance
    } else if price > distance {
        price - distance
    } else {
        BigUint::ZERO
    };
    json!({
        "status": "estimate",
        "price": fixed(&trigger, 18),
        "entry": entry.output,
        "excludesBorrowingFees": true,
        "assumption": "Entry at the reference price with only the estimated execution fees deducted.",
    })
}

fn minimum_fee_usd(row: &Value) -> Option<BigUint> {
    let size = integer(row.get("min_position_size_usd")?)?;
    let opening = decimal(&row["open_fee_p"])?;
    let trigger = decimal(&row["trigger_order_fee_p"])?;
    Some(size * (BigUint::from(opening) * 2u32 + trigger) / SCALE)
}

fn base_fees(
    schedule: &Value,
    collateral_price: &Price,
    notional: BigUint,
    opening: bool,
    triggered: bool,
    available_collateral: Option<&BigUint>,
) -> Option<(BigUint, Value)> {
    if notional.bits() == 0 {
        return None;
    }
    let open_rate = decimal(&schedule["open_fee_p"])?;
    let close_rate = decimal(&schedule["close_fee_p"])?;
    let trigger = decimal(&schedule["trigger_order_fee_p"])?;
    let basis = notional.max(minimum_fee_usd(schedule)? * SCALE / collateral_price.units);
    let rate = if opening { open_rate } else { close_rate };
    let mut fee = &basis * rate / SCALE;
    if triggered {
        fee += basis * trigger / SCALE;
    }
    if let Some(collateral) = available_collateral {
        fee = fee.min(collateral.clone());
    }
    let excludes: &[&str] = if opening {
        &["Trader discounts", "absolute exposure fees", "network fees"]
    } else {
        &[
            "Trader discounts",
            "borrowing fees",
            "absolute exposure fees",
            "profit fees",
            "short-hold penalties and fee clawbacks",
            "network fees",
        ]
    };
    let output = json!({
        "status": "estimate",
        "amountUsdc": fixed(&fee, USDC_PLACES),
        "basis": "Undiscounted execution fees using Perp's contract fee schedule.",
        "collateralPriceUsd": collateral_price.output,
        "excludes": excludes,
    });
    Some((fee, output))
}

fn integer(value: &Value) -> Option<BigUint> {
    match value {
        Value::String(text) => text.parse().ok(),
        Value::Number(number) => number.to_string().parse().ok(),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mcp_market_fill_bound_floors_each_slippage_distance() {
        let reference = Price::new("0.000000000000000199", KEEPER_PRICE, None).unwrap();
        let slippage = positive_decimal("33.25", "Slippage").unwrap();
        for (long, expected) in [
            (true, "0.000000000000000353"),
            (false, "0.000000000000000089"),
        ] {
            let bound = worst_case_fill(Some(&reference), long, slippage, true, true);
            assert_eq!(bound["status"], "bound");
            assert_eq!(bound["price"], expected);
        }
    }
}
