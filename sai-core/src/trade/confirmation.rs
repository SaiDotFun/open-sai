use std::{fmt::Write, sync::LazyLock};

use num_bigint::BigUint;
use regex::Regex;
use serde_json::{Map, json};
use sha2::{Digest, Sha256};

use super::{
    PreviewMode, TradePreview, Trading,
    actions::invalid,
    position::{Lcd, PositionRoute},
};
use crate::{
    errors::{Code, SaiError},
    keeper::TIMEOUT,
    settings::repr,
    storage::Session,
};

const NEEDS_DIGEST: &str = "preview_digest is required: call the matching preview_* tool, show the user the preview, get confirmation, then pass its previewDigest";
const MISMATCH: &str = "The trade no longer matches the preview the user confirmed, so nothing was submitted. Show the user this new preview, get their confirmation again, then retry with its previewDigest.";

/// Preview confirmation supplied by a caller that requires an explicit digest.
pub struct Confirmation<'a> {
    digest: &'a str,
    reference_price: Option<(&'a str, BigUint)>,
    cli: bool,
}

impl<'a> Confirmation<'a> {
    pub fn new(digest: &'a str, reference_price: Option<&'a str>) -> Result<Self, SaiError> {
        if digest.is_empty() {
            return Err(SaiError::new(Code::InvalidRequest, NEEDS_DIGEST));
        }
        let reference_price = reference_price
            .map(|price| {
                reference_price_units(price, "Reference price").map(|units| (price, units))
            })
            .transpose()?;
        Ok(Self {
            digest,
            reference_price,
            cli: false,
        })
    }

    pub fn for_cli(digest: &'a str) -> Self {
        Self {
            digest,
            reference_price: None,
            cli: true,
        }
    }

    pub fn for_market_cli(digest: &'a str, price: Option<&'a str>) -> Result<Self, SaiError> {
        let price = price.ok_or_else(|| {
            invalid("Pass the confirmed market preview's referencePrice via --price")
        })?;
        if decimal_parts(price).is_none() {
            return Err(SaiError::usage(format!(
                "Invalid value for '--price': {} is not a valid decimal.",
                repr(price)
            )));
        }
        let units = reference_price_units(price, "Reference price")?;
        Ok(Self {
            reference_price: Some((price, units)),
            ..Self::for_cli(digest)
        })
    }

    pub(super) fn is_cli(&self) -> bool {
        self.cli
    }

    fn verify_digest(&self, preview: &TradePreview, account: &str) -> Result<(), SaiError> {
        if !self.digest_matches(preview, account)? {
            if self.cli {
                return Err(SaiError::new(
                    Code::PreviewMismatch,
                    "The trade no longer matches the confirmed preview. Run with --dry-run to review a new preview before submitting.",
                ));
            }
            return Err(refusal(
                preview,
                account,
                Code::PreviewMismatch,
                MISMATCH.into(),
            ));
        }
        Ok(())
    }

    pub(super) fn verify_route_digest(
        &self,
        preview: &TradePreview,
        account: &str,
        route: &PositionRoute,
    ) -> Result<(), SaiError> {
        let mut current = preview.clone();
        current.position_route = Some(route.clone());
        if self.digest_matches(&current, account)? {
            return Ok(());
        }
        let message = if self.cli {
            "The trade no longer matches the confirmed preview. Request a new preview before submitting."
        } else {
            "The trade no longer matches the preview the user confirmed. Request a fresh preview before submitting."
        };
        Err(SaiError::new(Code::PreviewMismatch, message))
    }

    fn digest_matches(&self, preview: &TradePreview, account: &str) -> Result<bool, SaiError> {
        match (
            preview.requires_market_price_refresh(),
            self.reference_price.as_ref(),
        ) {
            (true, None) => {
                return Err(SaiError::new(
                    Code::InvalidRequest,
                    "reference_price is required for a market order: pass the referencePrice from the preview_open result the user confirmed",
                ));
            }
            (false, Some(_)) => {
                return Err(SaiError::new(
                    Code::InvalidRequest,
                    "reference_price applies only to market orders",
                ));
            }
            _ => {}
        }
        let mut confirmed = preview.clone();
        if let Some((reference, _)) = &self.reference_price {
            confirmed.reference_price = Some((*reference).into());
        }
        Ok(preview_digest(&confirmed, Some(account)) == self.digest)
    }

    pub(super) fn verify(self, preview: &TradePreview, account: &str) -> Result<(), SaiError> {
        self.verify_digest(preview, account)?;
        if let (Some((_, reference)), Some(current)) =
            (&self.reference_price, &preview.reference_price)
        {
            let current = reference_price_units(current, "Current price")?;
            let slippage = preview.intent.body["maxSlippagePct"]
                .as_str()
                .ok_or_else(SaiError::internal)?;
            let allowed = if slippage == "0" {
                0
            } else {
                positive_decimal(slippage, "Slippage")?
            };
            let distance = if &current >= reference {
                current - reference
            } else {
                reference - current
            };
            let moved = distance * 100u32;
            if &moved * SCALE > reference * allowed {
                let moved = display_movement(moved, reference.clone());
                if self.cli {
                    return Err(SaiError::new(
                        Code::PriceMoved,
                        format!(
                            "The market price moved {moved}% from the confirmed reference, more than the {slippage}% slippage. Run with --dry-run to review a new preview before submitting."
                        ),
                    ));
                }
                return Err(refusal(
                    preview,
                    account,
                    Code::PriceMoved,
                    format!(
                        "The market price moved {moved}% from the confirmed referencePrice, more than the {slippage}% slippage, so nothing was submitted. Show the user this new preview, get their confirmation again, then retry with its previewDigest and referencePrice."
                    ),
                ));
            }
        }
        Ok(())
    }

    fn check_unavailable_target<T>(
        &self,
        preview: &TradePreview,
        account: &str,
        route: &PositionRoute,
        result: Result<T, SaiError>,
    ) -> Result<T, SaiError> {
        if result.is_err() {
            self.verify_route_digest(preview, account, route)?;
        }
        result
    }
}

impl Trading<'_> {
    pub(super) async fn prepare_cli_confirmation(
        &self,
        session: &Session,
        preview: &TradePreview,
        confirmation: &Confirmation<'_>,
    ) -> Result<TradePreview, SaiError> {
        let lcd = self.lcd_endpoint.map(Lcd).ok_or_else(|| {
            let message = match preview.intent.kind {
                "OPEN_TRADE" => "Cannot verify the trade route without an LCD",
                "CANCEL_PENDING_TRADE" => "Cannot verify the pending order without an LCD",
                _ => "Cannot verify the position without an LCD",
            };
            invalid(message)
        })?;
        let route = self.position_route(&lcd, true).await?;
        let mut prepared = preview.clone();
        prepared.position_route = Some(route.clone());
        let addr = session.addr();
        match preview.intent.kind {
            "OPEN_TRADE" => {
                let market_row = if preview.requires_market_price_refresh() {
                    Some(
                        self.refresh_confirmed_cli_price(
                            session,
                            &mut prepared,
                            confirmation,
                            &lcd,
                            &route,
                        )
                        .await?,
                    )
                } else {
                    None
                };
                self.estimate_preview(
                    &mut prepared,
                    Some(session),
                    PreviewMode::CliSubmission,
                    market_row,
                )
                .await?;
                self.recheck_open_route(&lcd, &route).await?;
            }
            "CANCEL_PENDING_TRADE" => {
                let target = self
                    .check_pending_target(session, preview, &lcd, &route)
                    .await;
                confirmation.check_unavailable_target(preview, addr, &route, target)?;
            }
            _ => {
                let id = preview.position_id()?;
                let target = async {
                    let indexed = self.indexed_position(addr, &id, TIMEOUT).await?;
                    let verified =
                        Self::verify_contract_owner(&lcd, addr, &id, route.clone()).await?;
                    Ok((indexed, verified))
                }
                .await;
                let (indexed, verified) =
                    confirmation.check_unavailable_target(preview, addr, &route, target)?;
                if preview.is_market_close() {
                    let (_, estimates) = self
                        .estimate_verified_close(&lcd, addr, &id, indexed, verified)
                        .await?;
                    prepared.estimates = Some(estimates);
                } else if !preview.intent.body["requestedLeverage"].is_null() {
                    self.estimate_position_update(
                        &mut prepared,
                        addr,
                        &id,
                        Some(verified.0),
                        indexed,
                        Some(verified.1),
                    )
                    .await?;
                }
            }
        }
        Ok(prepared)
    }

    async fn refresh_confirmed_cli_price(
        &self,
        session: &Session,
        preview: &mut TradePreview,
        confirmation: &Confirmation<'_>,
        lcd: &Lcd<'_>,
        route: &PositionRoute,
    ) -> Result<serde_json::Value, SaiError> {
        let mismatch = confirmation.verify_digest(preview, session.addr()).is_err();
        let refreshed = async {
            let row = self.refresh_market_quote(preview).await?;
            let price = preview
                .reference_price
                .as_deref()
                .ok_or_else(SaiError::internal)?;
            reference_price_units(price, "Current price")?;
            preview.intent.body["intentPrice"] = json!(price);
            Ok(row)
        }
        .await;
        match refreshed {
            Ok(row) => Ok(row),
            Err(_) if mismatch => Err(SaiError::new(
                Code::PreviewMismatch,
                "The market open no longer matches the confirmed preview. Request a new preview before submitting.",
            )),
            Err(error) => {
                self.recheck_open_route(lcd, route).await?;
                self.estimate_preview(preview, Some(session), PreviewMode::CliSubmission, None)
                    .await?;
                self.recheck_open_route(lcd, route).await?;
                preview.check_cli_execution(session.addr())?;
                Err(error)
            }
        }
    }

    async fn recheck_open_route(
        &self,
        lcd: &Lcd<'_>,
        route: &PositionRoute,
    ) -> Result<(), SaiError> {
        if &self.position_route(lcd, true).await? != route {
            return Err(SaiError::new(
                Code::PreviewMismatch,
                "The trading route changed while checking the open. Request a new preview before submitting.",
            ));
        }
        Ok(())
    }
}

impl TradePreview {
    pub(super) fn check_cli_execution(&self, account: &str) -> Result<(), SaiError> {
        if let Some(estimates) = &self.estimates {
            let execution = &estimates["execution"];
            if (execution["status"] == "would_be_rejected"
                || execution["code"] == "OraclePublicationTimeUnknown")
                && let Some(reason) = execution["reason"].as_str()
            {
                let mut preview = self.cli_preview_output(Some(account));
                if let Some(fields) = preview.as_object_mut() {
                    fields.shift_remove("quote");
                }
                return Err(SaiError {
                    details: Some(Box::new(Map::from_iter([("preview".into(), preview)]))),
                    ..SaiError::new(Code::PreviewMismatch, reason)
                });
            }
        }
        Ok(())
    }
}

fn refusal(preview: &TradePreview, account: &str, code: Code, message: String) -> SaiError {
    SaiError {
        details: Some(Box::new(Map::from_iter([(
            "preview".into(),
            preview.confirmed_output(Some(account)),
        )]))),
        ..SaiError::new(code, message)
    }
}

const SCALE: u128 = 10u128.pow(18);

fn decimal_parts(value: &str) -> Option<(&str, &str)> {
    static PLAIN: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\A(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?\z").expect("valid decimal pattern")
    });
    PLAIN
        .is_match(value)
        .then(|| value.split_once('.').unwrap_or((value, "")))
}

fn invalid_price(name: &str) -> SaiError {
    SaiError::new(
        Code::InvalidRequest,
        format!("{name} must be a positive finite decimal"),
    )
}

pub(super) fn reference_price_units(value: &str, name: &str) -> Result<BigUint, SaiError> {
    // Python checks Decimal(price) * 10**18 with 28 significant digits and
    // round-half-even. This is the last integer that rounds within u128::MAX.
    static PYTHON_REFERENCE_MAX: LazyLock<BigUint> = LazyLock::new(|| {
        BigUint::parse_bytes(b"340282366920938463463374607450000000000", 10)
            .expect("valid reference price limit")
    });
    let (whole, fraction) = decimal_parts(value)
        .filter(|(whole, _)| whole.len() <= 21)
        .ok_or_else(|| invalid_price(name))?;
    let units = format!("{whole}{fraction:0<18}");
    BigUint::parse_bytes(units.as_bytes(), 10)
        .filter(|units| units.bits() != 0 && units <= &*PYTHON_REFERENCE_MAX)
        .ok_or_else(|| invalid_price(name))
}

/// Parse a positive plain decimal into contract units with 18 fractional digits.
pub fn positive_decimal(value: &str, name: &str) -> Result<u128, SaiError> {
    let (whole, fraction) = decimal_parts(value).ok_or_else(|| invalid_price(name))?;
    let whole = whole
        .parse::<u128>()
        .ok()
        .and_then(|whole| whole.checked_mul(SCALE));
    let fraction = fraction.parse::<u128>().unwrap_or(0) * 10u128.pow(18 - fraction.len() as u32);
    whole
        .and_then(|whole| whole.checked_add(fraction))
        .filter(|&units| units != 0)
        .ok_or_else(|| invalid_price(name))
}

fn display_movement(moved: BigUint, reference: BigUint) -> String {
    let scaled = moved * 10_000u32;
    let mut rounded = &scaled / &reference;
    let double_remainder = (scaled % &reference) * 2u32;
    if double_remainder > reference || (double_remainder == reference && rounded.bit(0)) {
        rounded += 1u32;
    }
    let digits = format!("{rounded:0>5}");
    let (whole, fraction) = digits.split_at(digits.len() - 4);
    format!("{whole}.{fraction}")
}

fn canonical_price(price: &str) -> String {
    let (whole, fraction) = price.split_once('.').unwrap_or((price, ""));
    let digits = format!("{whole}{fraction}");
    let significant = digits.trim_start_matches('0');
    let point = significant.len() as i64 - fraction.len() as i64;
    if significant.is_empty() {
        return "0".into();
    }
    if !significant.bytes().all(|byte| byte.is_ascii_digit()) {
        return price.into();
    }
    plain_decimal(significant.trim_end_matches('0'), point)
}

pub(super) fn plain_decimal(significant: &str, point: i64) -> String {
    if point <= 0 {
        return format!("0.{}{significant}", "0".repeat((-point) as usize));
    }
    let point = point as usize;
    if point >= significant.len() {
        return format!("{significant}{}", "0".repeat(point - significant.len()));
    }
    let (whole, fraction) = significant.split_at(point);
    format!("{whole}.{fraction}")
}

/// The exact canonical JSON bytes that the user confirms.
pub fn digest_bytes(preview: &TradePreview, account: Option<&str>) -> Vec<u8> {
    let mut material = json!({
        "addr": account.map(str::to_lowercase),
        "intent": preview.intent,
    });
    if preview.intent.is_market_open() {
        material["intent"]["body"]
            .as_object_mut()
            .expect("market intent body")
            .shift_remove("intentPrice");
        material["referencePrice"] = json!(preview.reference_price.as_deref().map(canonical_price));
    }
    if let Some(quote) = &preview.quote {
        let mut quote = quote.clone();
        if preview.intent.kind == "DECREASE_POSITION_SIZE"
            && let Some(fields) = quote.as_object_mut()
        {
            // The user must confirm again once a USD estimate is available.
            fields.shift_remove("collateralPriceUsd");
            let usd_available = fields
                .shift_remove("closedNotionalUsd")
                .is_some_and(|value| !value.is_null());
            fields.insert("closedNotionalUsdAvailable".into(), json!(usd_available));
        }
        material["quote"] = quote;
    }
    if let Some(route) = &preview.position_route {
        material["positionRoute"] = json!(route);
    }
    material.sort_all_objects();
    let mut canonical = String::new();
    for ch in material.to_string().chars() {
        if ch.is_ascii() && ch != '\u{7f}' {
            canonical.push(ch);
        } else {
            for unit in ch.encode_utf16(&mut [0; 2]) {
                write!(canonical, "\\u{unit:04x}").expect("writing to a string");
            }
        }
    }
    canonical.into_bytes()
}

pub fn preview_digest(preview: &TradePreview, account: Option<&str>) -> String {
    format!("{:x}", Sha256::digest(digest_bytes(preview, account)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::trade::TradeIntent;
    use serde_json::Value;

    #[test]
    fn partial_close_digest_binds_usd_availability_and_estimate_status() {
        let digest = |price: Option<&str>, usd: Option<&str>, estimated| {
            let mut preview = TradePreview::new(TradeIntent::new(
                "DECREASE_POSITION_SIZE",
                Some("7".into()),
                json!({"collateral": {"amountUsdc": "5000000"}, "leverageDelta": "0"}),
            ));
            preview.quote = Some(json!({
                "collateralPriceUsd": price,
                "closedNotionalUsd": usd,
                "closedNotionalIsEstimate": estimated,
            }));
            preview_digest(&preview, None)
        };
        let priced_estimate = digest(Some("1"), Some("10"), true);
        let priced = digest(Some("1"), Some("10"), false);
        assert_ne!(digest(None, None, true), priced_estimate);
        assert_ne!(priced_estimate, priced);
        assert_eq!(priced, digest(Some("1.00119"), Some("10.0119"), false));
    }

    #[test]
    fn canonical_bytes_and_digests_match_the_python_reference() {
        let data: Value =
            serde_json::from_str(include_str!("../../../tests/parity/trade_digest.json")).unwrap();
        for case in data["cases"].as_array().unwrap() {
            let input = &case["preview"];
            let intent = &input["intent"];
            let kind = match intent["kind"].as_str().unwrap() {
                "OPEN_TRADE" => "OPEN_TRADE",
                "CLOSE_POSITION" => "CLOSE_POSITION",
                "CANCEL_PENDING_TRADE" => "CANCEL_PENDING_TRADE",
                other => panic!("Unexpected fixture intent kind {other}"),
            };
            let mut preview = TradePreview::new(TradeIntent::new(
                kind,
                intent["target"]["userTradeId"].as_str().map(str::to_owned),
                intent["body"].clone(),
            ));
            preview.reference_price = input["referencePrice"].as_str().map(str::to_owned);
            preview.quote = input.get("quote").cloned();
            preview.estimates = Some(data["estimates"].clone());
            if kind == "CLOSE_POSITION" {
                preview.position_owner_verified = Some(true);
            }
            if let Some(route) = input.get("positionRoute") {
                preview.position_route = Some(crate::trade::position::PositionRoute {
                    chain_id: route["chainId"].as_str().unwrap().into(),
                    contract_address: route["contractAddress"].as_str().unwrap().into(),
                });
            }
            let account = input["addr"].as_str();
            assert_eq!(
                digest_bytes(&preview, account),
                case["canonical"].as_str().unwrap().as_bytes()
            );
            assert_eq!(
                preview_digest(&preview, account),
                case["digest"].as_str().unwrap()
            );
        }
    }

    #[test]
    fn confirmation_reference_matches_python_at_the_decimal_limit() {
        let accepted = "340282366920938463463.374607450000000000";
        let rejected = "340282366920938463463.374607450000000001";
        assert!(Confirmation::new("fixture", Some(accepted)).is_ok());
        assert!(Confirmation::new("fixture", Some(rejected)).is_err());
        assert!(positive_decimal(accepted, "Price").is_err());
    }

    #[test]
    fn cancel_confirmation_refusal_carries_the_market_close_warning() {
        use crate::trade::close_preview;

        let account = "0xaa00000000000000000000000000000000000001";
        let old = close_preview("8", true).unwrap();
        let current = close_preview("7", true).unwrap();
        let digest = preview_digest(&old, Some(account));
        let refusal = Confirmation::new(&digest, None)
            .unwrap()
            .verify(&current, account)
            .unwrap_err();
        assert_eq!(refusal.code, Code::PreviewMismatch);
        let replacement = &refusal.details.unwrap()["preview"];
        assert_eq!(replacement["intent"]["target"]["userTradeId"], "7");
        assert_eq!(
            replacement["previewDigest"],
            preview_digest(&current, Some(account))
        );
        for name in ["fees", "liquidationPrice", "worstCaseFill"] {
            assert_eq!(
                replacement["estimates"][name],
                json!({
                    "status": "unavailable",
                    "reason": "A pending order can fill before cancellation executes. Perp may close a filled position at market.",
                })
            );
        }
    }

    #[test]
    fn refuses_price_movement_that_decimal_context_rounds_to_the_limit() {
        let account = "0xaa00000000000000000000000000000000000001";
        let mut preview = TradePreview::new(TradeIntent::new(
            "OPEN_TRADE",
            None,
            json!({"orderType": "market", "maxSlippagePct": "10"}),
        ));
        let reference = "100000000000";
        preview.reference_price = Some(reference.into());
        let digest = preview_digest(&preview, Some(account));
        preview.reference_price = Some("110000000000".into());
        Confirmation::new(&digest, Some(reference))
            .unwrap()
            .verify(&preview, account)
            .unwrap();
        preview.reference_price = Some("110000000000.000000000000000001".into());
        let refusal = Confirmation::new(&digest, Some(reference))
            .unwrap()
            .verify(&preview, account)
            .unwrap_err();
        assert_eq!(refusal.code, Code::PriceMoved);
    }

    #[test]
    fn confirmation_binds_the_account_before_checking_market_movement() {
        let account = "0xaa00000000000000000000000000000000000001";
        let mut preview = TradePreview::new(TradeIntent::new(
            "OPEN_TRADE",
            None,
            json!({"orderType": "market", "maxSlippagePct": "1"}),
        ));
        preview.reference_price = Some("100".into());
        let digest = preview_digest(&preview, Some(account));
        preview.reference_price = Some("101".into());
        Confirmation::new(&digest, Some("100.0"))
            .unwrap()
            .verify(&preview, account)
            .unwrap();

        preview.reference_price = Some("101.00005".into());
        let check = || Confirmation::new(&digest, Some("100")).unwrap();
        let refusal = check()
            .verify(&preview, "0xbb00000000000000000000000000000000000002")
            .unwrap_err();
        assert_eq!(refusal.code, Code::PreviewMismatch);
        let refusal = check().verify(&preview, account).unwrap_err();
        assert_eq!(refusal.code, Code::PriceMoved);
        assert!(refusal.message.contains("1.0000%"));
        assert_eq!(
            refusal.details.unwrap()["preview"]["referencePrice"],
            "101.00005"
        );
        assert_eq!(display_movement(3u32.into(), 20_000u32.into()), "0.0002");
    }
}
