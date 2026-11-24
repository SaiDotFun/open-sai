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
        })
    }

    pub(super) fn verify(self, preview: &TradePreview, account: &str) -> Result<(), SaiError> {
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

fn canonical_price(price: &str) -> &str {
    if price.contains('.') {
        price.trim_end_matches('0').trim_end_matches('.')
    } else {
        price
    }
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
