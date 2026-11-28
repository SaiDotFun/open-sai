//! Read USDC base units as dollars.

use serde_json::Value;

/// Convert USDC base units to a dollar decimal. Sai values one USDC at
/// exactly $1, and USDC has six decimals. Returns `None` for anything other
/// than a non-negative integer, as a JSON number or a string of digits.
pub fn base_units_usd(units: &Value) -> Option<String> {
    let digits = match units {
        Value::Number(number) => number.as_u64()?.to_string(),
        Value::String(text) if !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit()) => {
            text.clone()
        }
        _ => return None,
    };
    let padded = format!("{:0>7}", digits.trim_start_matches('0'));
    let (whole, fraction) = padded.split_at(padded.len() - 6);
    let fraction = fraction.trim_end_matches('0');
    Some(if fraction.is_empty() {
        whole.to_owned()
    } else {
        format!("{whole}.{fraction}")
    })
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::base_units_usd;

    #[test]
    fn base_units_read_as_dollars() {
        for (units, usd) in [
            (json!(3_000_000), Some("3")),
            (json!(2_997_450), Some("2.99745")),
            (json!("4000000"), Some("4")),
            (json!("000001"), Some("0.000001")),
            (json!("10999999"), Some("10.999999")),
            (json!(0), Some("0")),
            (json!("0"), Some("0")),
            (Value::Null, None),
            (json!(-1), None),
            (json!(1.5), None),
            (json!("1e6"), None),
            (json!("-1"), None),
            (json!(""), None),
        ] {
            assert_eq!(base_units_usd(&units).as_deref(), usd, "{units}");
        }
    }
}
