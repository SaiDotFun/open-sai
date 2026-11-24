//! Create and replace trading sessions using Keeper's account challenge.

mod clipboard;
mod signer;

use std::{io, path::Path, process::Output, time::Duration};

use base64::{
    Engine, alphabet,
    engine::{DecodePaddingMode, GeneralPurpose, GeneralPurposeConfig},
};
use chrono::{DateTime, Datelike, SecondsFormat, Utc};
use serde_json::{Map, Value, json};
use tokio::{process::Command, time::timeout};
use zeroize::Zeroizing;

use crate::{
    errors::{Code, SaiError},
    keeper::{CHALLENGE, Keeper, SESSION, TIMEOUT},
    storage::Session,
};

pub use clipboard::{CLIPBOARD_NAMES, Clipboard};
pub use signer::{LoginError, login};

async fn run(command: &mut Command, seconds: u64) -> io::Result<Output> {
    let child = command.kill_on_drop(true).spawn()?;
    timeout(Duration::from_secs(seconds), child.wait_with_output()).await?
}

async fn run_success(command: &mut Command, seconds: u64) -> io::Result<Output> {
    let output = run(command, seconds).await?;
    if !output.status.success() {
        return Err(io::Error::other("command failed"));
    }
    Ok(output)
}

pub fn import_session(token: Zeroizing<String>, endpoint: &str) -> Result<Session, SaiError> {
    let (addr, expiry) = imported_claims(&token).ok_or_else(|| {
        SaiError::new(Code::InvalidRequest, "Invalid or expired Sai session token")
    })?;
    if expiry <= Utc::now() {
        return Err(SaiError::new(
            Code::SessionExpired,
            "Invalid or expired Sai session token",
        ));
    }
    let precision = if expiry.timestamp_subsec_micros() == 0 {
        SecondsFormat::Secs
    } else {
        SecondsFormat::Micros
    };
    let mut session = Session::new(
        token,
        addr,
        expiry.to_rfc3339_opts(precision, false),
        endpoint.into(),
    );
    session.mark_imported();
    Ok(session)
}

fn imported_claims(token: &str) -> Option<(String, DateTime<Utc>)> {
    // Python's urlsafe decoder accepts both alphabets and ignores non-alphabet
    // bytes. JWT claims emitted by Keeper use unpadded URL-safe base64.
    const BASE64: GeneralPurpose = GeneralPurpose::new(
        &alphabet::STANDARD,
        GeneralPurposeConfig::new()
            .with_decode_allow_trailing_bits(true)
            .with_decode_padding_mode(DecodePaddingMode::Indifferent),
    );
    let payload = token.split('.').nth(1)?;
    let encoded: Vec<_> = payload
        .bytes()
        .map(|byte| match byte {
            b'-' => b'+',
            b'_' => b'/',
            other => other,
        })
        .filter(|byte| byte.is_ascii_alphanumeric() || b"+/=".contains(byte))
        .collect();
    let decoded = Zeroizing::new(BASE64.decode(encoded).ok()?);
    let claims: Value =
        serde_json::from_slice(decoded.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&decoded)).ok()?;
    let addr = claims["sub"].as_str()?;
    if !addr.starts_with("0x") || addr.chars().count() != 42 || claims["purpose"] != "user_intent" {
        return None;
    }
    let exp = &claims["exp"];
    let timestamp = match exp.as_bool() {
        Some(value) => f64::from(u8::from(value)),
        None => exp.as_f64()?,
    };
    let seconds = timestamp.floor();
    let micros = ((timestamp - seconds) * 1_000_000.0).round_ties_even() as i64;
    let expiry = DateTime::from_timestamp_micros(
        (seconds as i64)
            .checked_mul(1_000_000)?
            .checked_add(micros)?,
    )?;
    (1..=9999)
        .contains(&expiry.year())
        .then(|| (addr.into(), expiry))
}

async fn fetch_challenge(keeper: &Keeper<'_>, addr: &str) -> Result<(Value, String), SaiError> {
    let query = variables(json!({"address": addr}));
    let mut data = keeper.request(CHALLENGE, query, None, TIMEOUT).await?;
    let challenge = data
        .remove("createUserIntentAuthChallenge")
        .ok_or_else(missing_data)?;
    let returned = required(&challenge, "evmAddress")?;
    if !returned.eq_ignore_ascii_case(addr) {
        return Err(keeper_error(
            "Challenge addr does not match the requested account",
        ));
    }
    let nonce = challenge.get("nonce").ok_or_else(missing_data)?;
    let mut typed = challenge.get("typedData").ok_or_else(missing_data)?.clone();
    let normalized = nonce_hex(nonce);
    let valid = typed["primaryType"] == "UserTradingSession"
        && typed["domain"]["name"] == "Sai"
        && typed["domain"]["version"] == "1"
        && typed["message"]["account"]
            .as_str()
            .is_some_and(|account| account.eq_ignore_ascii_case(addr))
        && normalized.is_some()
        && nonce_hex(&typed["message"]["challenge"]) == normalized;
    if !valid {
        return Err(keeper_error(
            "Challenge is not a Sai trading session request",
        ));
    }
    let declared = field(field(&typed, "types")?, "EIP712Domain")?
        .as_array()
        .ok_or_else(|| data_error("TypeError"))?
        .iter()
        .map(|field| required(field, "name").map(str::to_owned))
        .collect::<Result<Vec<_>, _>>()?;
    let domain = typed["domain"]
        .as_object_mut()
        .expect("domain was validated");
    let empty = |value: &Value| value.is_null() || value == "";
    domain.retain(|key, value| declared.contains(key) || !empty(value));
    if declared.iter().any(|key| domain.get(key).is_none_or(empty)) {
        return Err(keeper_error(
            "Challenge has an empty declared signing domain field",
        ));
    }
    Ok((typed, nonce.as_str().expect("nonce was validated").into()))
}

fn nonce_hex(value: &Value) -> Option<String> {
    let lower = value.as_str()?.to_ascii_lowercase();
    let digits = lower.strip_prefix("0x").unwrap_or(&lower);
    (digits.len() == 64 && digits.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .then(|| digits.to_owned())
}

async fn exchange(
    keeper: &Keeper<'_>,
    addr: &str,
    nonce: &str,
    signature: &str,
    name: &str,
) -> Result<Session, SaiError> {
    let vars = variables(json!({"signature": signature, "challenge": nonce}));
    let data = keeper.request(SESSION, vars, None, TIMEOUT).await?;
    let session = data
        .get("createUserIntentSession")
        .ok_or_else(missing_data)?;
    let returned = required(session, "evmAddress")?;
    if !returned.eq_ignore_ascii_case(addr) {
        return Err(keeper_error(
            "Session addr does not match the requested account",
        ));
    }
    let expires_at = required(session, "expiresAt")?;
    let token = required(session, "token")?;
    let mut session = Session::new(
        token.to_owned().into(),
        returned.into(),
        expires_at.into(),
        keeper.endpoint().into(),
    );
    session.set_key_name(name);
    Ok(session)
}

fn variables(value: Value) -> Map<String, Value> {
    let Value::Object(vars) = value else {
        unreachable!("GraphQL variables are an object")
    };
    vars
}

fn required<'a>(value: &'a Value, key: &str) -> Result<&'a str, SaiError> {
    field(value, key)?
        .as_str()
        .ok_or_else(|| data_error("TypeError"))
}

fn field<'a>(value: &'a Value, key: &str) -> Result<&'a Value, SaiError> {
    value
        .as_object()
        .ok_or_else(|| data_error("TypeError"))?
        .get(key)
        .ok_or_else(missing_data)
}

fn missing_data() -> SaiError {
    data_error("KeyError")
}

fn data_error(kind: &str) -> SaiError {
    SaiError::new(
        Code::IncompleteData,
        format!("Keeper or contract data was missing or malformed ({kind})"),
    )
}

fn keeper_error(message: &str) -> SaiError {
    SaiError::new(Code::KeeperError, message)
}

// Match executable discovery without mistaking a failed launch for an absent tool.
fn installed(program: &str) -> bool {
    use std::{env, os::unix::fs::PermissionsExt};

    let path = env::var_os("PATH").unwrap_or_else(|| "/bin:/usr/bin".into());
    env::split_paths(&path).any(|dir| {
        let candidate = if dir.as_os_str().is_empty() {
            Path::new(program).to_owned()
        } else {
            dir.join(program)
        };
        candidate
            .metadata()
            .is_ok_and(|info| info.is_file() && info.permissions().mode() & 0o111 != 0)
    })
}
