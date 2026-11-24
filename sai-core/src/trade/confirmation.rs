use std::{fmt::Write, sync::LazyLock};

use num_bigint::BigUint;
use regex::Regex;
use serde_json::{Map, json};
use sha2::{Digest, Sha256};

use super::TradePreview;
use crate::errors::{Code, SaiError};

const NEEDS_DIGEST: &str = "preview_digest is required: call the matching preview_* tool, show the user the preview, get confirmation, then pass its previewDigest";
const MISMATCH: &str = "The trade no longer matches the preview the user confirmed, so nothing was submitted. Show the user this new preview, get their confirmation again, then retry with its previewDigest.";

/// Preview confirmation supplied by a caller that requires an explicit digest.
pub struct Confirmation<'a> {
    digest: &'a str,
    reference_price: Option<(&'a str, u128)>,
    position_cli: bool,
}

impl<'a> Confirmation<'a> {
    pub fn new(digest: &'a str, reference_price: Option<&'a str>) -> Result<Self, SaiError> {
        if digest.is_empty() {
            return Err(SaiError::new(Code::InvalidRequest, NEEDS_DIGEST));
        }
        let reference_price = reference_price
            .map(|price| positive_decimal(price, "Reference price").map(|units| (price, units)))
            .transpose()?;
        Ok(Self {
            digest,
            reference_price,
            position_cli: false,
        })
    }

    pub fn for_position_cli(digest: &'a str) -> Self {
        Self {
            digest,
            reference_price: None,
            position_cli: true,
        }
    }

    pub(super) fn is_position_cli(&self) -> bool {
        self.position_cli
    }

    pub(super) fn verify(self, preview: &TradePreview, account: &str) -> Result<(), SaiError> {
        match (
            preview.requires_market_price_refresh(),
            self.reference_price,
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
        if let Some((reference, _)) = self.reference_price {
            confirmed.reference_price = Some(reference.into());
        }
        let refusal = |code, message: String| SaiError {
            details: Some(Box::new(Map::from_iter([(
                "preview".into(),
                preview.confirmed_output(Some(account)),
            )]))),
            ..SaiError::new(code, message)
        };
        if preview_digest(&confirmed, Some(account)) != self.digest {
            if self.position_cli {
                return Err(SaiError::new(
                    Code::PreviewMismatch,
                    "The trade no longer matches the confirmed preview. Run with --dry-run to review a new preview before submitting.",
                ));
            }
            return Err(refusal(Code::PreviewMismatch, MISMATCH.into()));
        }
        if let (Some((_, reference)), Some(current)) =
            (self.reference_price, &preview.reference_price)
        {
            let current = positive_decimal(current, "Current price")?;
            let slippage = preview.intent.body["maxSlippagePct"]
                .as_str()
                .ok_or_else(SaiError::internal)?;
            let allowed = if slippage == "0" {
                0
            } else {
                positive_decimal(slippage, "Slippage")?
            };
            let moved = BigUint::from(current.abs_diff(reference)) * 100u32;
            let reference = BigUint::from(reference);
            if &moved * SCALE > &reference * allowed {
                let moved = display_movement(moved, reference);
                return Err(refusal(
                    Code::PriceMoved,
                    format!(
                        "The market price moved {moved}% from the confirmed referencePrice, more than the {slippage}% slippage, so nothing was submitted. Show the user this new preview, get their confirmation again, then retry with its previewDigest and referencePrice."
                    ),
                ));
            }
        }
        Ok(())
    }
}

const SCALE: u128 = 10u128.pow(18);

/// Parse a positive plain decimal into contract units with 18 fractional digits.
pub fn positive_decimal(value: &str, name: &str) -> Result<u128, SaiError> {
    static PLAIN: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\A(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?\z").expect("valid decimal pattern")
    });
    let invalid = || {
        SaiError::new(
            Code::InvalidRequest,
            format!("{name} must be a positive finite decimal"),
        )
    };
    if !PLAIN.is_match(value) {
        return Err(invalid());
    }
    let (whole, fraction) = value.split_once('.').unwrap_or((value, ""));
    let whole = whole
        .parse::<u128>()
        .ok()
        .and_then(|whole| whole.checked_mul(SCALE));
    let fraction = fraction.parse::<u128>().unwrap_or(0) * 10u128.pow(18 - fraction.len() as u32);
    whole
        .and_then(|whole| whole.checked_add(fraction))
        .filter(|&units| units != 0)
        .ok_or_else(invalid)
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
    // Python Decimal.normalize() rounds to the default 28 significant digits.
    let rounded;
    let (significant, point) = if significant.len() > 28 {
        let (kept, rest) = significant.split_at(28);
        let mut units = kept.parse::<u128>().expect("28 digits fit in u128");
        let first = rest.as_bytes()[0];
        let above_half =
            first > b'5' || first == b'5' && rest[1..].bytes().any(|digit| digit != b'0');
        if above_half || first == b'5' && units % 2 == 1 {
            units += 1;
        }
        rounded = units.to_string();
        let carry = rounded.len() as i64 - 28;
        (rounded.as_str(), point + carry)
    } else {
        (significant, point)
    };
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
        material["quote"] = quote.clone();
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
