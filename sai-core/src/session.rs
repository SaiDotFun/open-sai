//! Endpoint and expiry checks shared by the CLI and other front ends.

use std::fmt::Write as _;

use chrono::{DateTime, Datelike, NaiveDate, TimeDelta, Utc, Weekday};
use serde::Serialize;

use crate::{
    errors::{Code, SaiError},
    keeper::is_evm_address,
    log,
    settings::MAINNET_HTTP,
    storage::{Session, SessionError, Storage, StorageError, StorageEvent, endpoint_origin},
};

const INVALID_RENEWAL_ADDRESS: &str = "Stored session address is invalid. Sign in again.";

pub fn expired(session: &Session, now: DateTime<Utc>) -> Result<bool, SessionError> {
    Ok(session_expiry(session)? <= now)
}

pub fn session_expiry(session: &Session) -> Result<DateTime<Utc>, SessionError> {
    parse_expiry(session.expires_at()).ok_or_else(|| {
        SessionError::new(
            "Stored session has an invalid expiry; run `sai auth import` or `sai auth login NAME` again",
        )
    })
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ExpiryNotice {
    pub expiry_warning: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub renewal_command: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub renewal_guidance: Option<String>,
}

impl ExpiryNotice {
    pub fn text(&self) -> String {
        let mut notice = self.expiry_warning.clone();
        if let Some(guidance) = &self.renewal_guidance {
            notice.push(' ');
            notice.push_str(guidance);
        }
        if let Some(command) = &self.renewal_command {
            let _ = write!(notice, " Run {command}");
        }
        notice
    }
}

fn expiry_warning_time(left: TimeDelta) -> Option<String> {
    if left <= TimeDelta::zero() || left >= TimeDelta::days(3) {
        return None;
    }
    Some(match left.num_minutes() {
        0 => "under 1 min".into(),
        minutes @ 1..120 => format!("{minutes} min"),
        minutes => format!("{} h", minutes / 60),
    })
}

fn escape_warning_text(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            '\u{2028}' | '\u{2029}' => {
                let _ = write!(escaped, "\\u{:04x}", u32::from(c));
            }
            c if c.is_control() => {
                let _ = write!(escaped, "\\x{:02x}", u32::from(c));
            }
            c => escaped.push(c),
        }
    }
    escaped
}

// Stored Keeper expiries follow Python's datetime.fromisoformat. Convert to
// UTC for comparisons and retain the original text in the session.
pub fn parse_expiry(text: &str) -> Option<DateTime<Utc>> {
    let date_end = date_length(text.as_bytes())?;
    let date = text.get(..date_end)?;
    let tail = text.get(date_end..)?;
    let time_and_offset = tail.get(tail.chars().next()?.len_utf8()..)?;
    let (time, offset) = time_and_offset.split_at(time_and_offset.find(['Z', '+', '-'])?);
    let (hour, minute, second, micros) = time_parts(time)?;
    let date = iso_date(date)?;
    let local = date.and_hms_micro_opt(hour, minute, second, micros)?;
    let offset = match offset.as_bytes() {
        b"Z" => 0,
        [sign @ (b'+' | b'-'), ..] => {
            let (h, m, s, micros) = time_parts(&offset[1..])?;
            let seconds = (h * 60 + m) * 60 + s;
            if seconds >= 86_400 {
                return None;
            }
            // Python treats an offset with zero whole seconds as UTC.
            let unsigned = if seconds == 0 {
                0
            } else {
                i64::from(seconds) * 1_000_000 + i64::from(micros)
            };
            if *sign == b'-' { -unsigned } else { unsigned }
        }
        _ => return None,
    };
    local
        .and_utc()
        .checked_sub_signed(TimeDelta::microseconds(offset))
}

fn date_length(text: &[u8]) -> Option<usize> {
    Some(match text.get(4..)? {
        [b'-', b'W', _, _, b'-', _, next, ..] if !next.is_ascii_digit() => 10,
        [b'-', b'W', ..] => 8,
        [b'-', ..] => 10,
        [b'W', _, _, tail @ ..] => {
            // A digit can be the weekday or separator. The time starts
            // with digit pairs, so parity disambiguates a run of digits.
            let digits = tail.iter().take_while(|c| c.is_ascii_digit()).count();
            match digits {
                0 => 7,
                1 => 8,
                _ if digits.is_multiple_of(2) => 8,
                _ => 7,
            }
        }
        _ => 8,
    })
}

fn iso_date(text: &str) -> Option<NaiveDate> {
    let year = text.get(..4)?;
    if !year.bytes().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let date = match text.len() {
        7 | 8
            if matches!(
                text.as_bytes(),
                [_, _, _, _, b'W', _, _] | [_, _, _, _, b'-', b'W', _, _]
            ) =>
        {
            let (_, week) = text.split_once('W')?;
            NaiveDate::from_isoywd_opt(year.parse().ok()?, week.parse().ok()?, Weekday::Mon)
        }
        8 if text.as_bytes().get(4) == Some(&b'W') => {
            NaiveDate::parse_from_str(text, "%GW%V%u").ok()
        }
        8 => NaiveDate::parse_from_str(text, "%Y%m%d").ok(),
        10 if text.as_bytes().get(5) == Some(&b'W') => {
            NaiveDate::parse_from_str(text, "%G-W%V-%u").ok()
        }
        10 => NaiveDate::parse_from_str(text, "%Y-%m-%d").ok(),
        _ => None,
    }?;
    (1..=9999).contains(&date.year()).then_some(date)
}

fn time_parts(text: &str) -> Option<(u32, u32, u32, u32)> {
    let (clock, fraction) = text.split_once(['.', ',']).unwrap_or((text, "0"));
    if fraction.is_empty() || !fraction.bytes().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let precision = fraction.len().min(6);
    let micros = fraction[..precision].parse::<u32>().ok()? * 10_u32.pow((6 - precision) as u32);
    let pair = |a: u8, b: u8| {
        (a.is_ascii_digit() && b.is_ascii_digit())
            .then(|| u32::from(a - b'0') * 10 + u32::from(b - b'0'))
    };
    let (h, m, s) = match *clock.as_bytes() {
        [a, b] => (pair(a, b)?, 0, 0),
        [a, b, c, d] | [a, b, b':', c, d] => (pair(a, b)?, pair(c, d)?, 0),
        [a, b, c, d, e, f] | [a, b, b':', c, d, b':', e, f] => {
            (pair(a, b)?, pair(c, d)?, pair(e, f)?)
        }
        _ => return None,
    };
    Some((h, m, s, micros))
}

impl Storage<'_> {
    pub fn load_active_session(
        &self,
        endpoint: &str,
        addr: Option<&str>,
    ) -> Result<Session, StorageError> {
        let session = self.load_session(addr).map_err(|mut error| {
            if let StorageError::Session(SessionError(failure)) = &mut error
                && failure.code == Code::SignedOut
            {
                failure.hint = Some(self.sign_in_hint(endpoint, addr));
            }
            error
        })?;
        self.check_active(&session, endpoint)?;
        Ok(session)
    }

    pub fn check_active(&self, session: &Session, endpoint: &str) -> Result<(), SessionError> {
        if session.endpoint() != Some(endpoint) {
            return Err(SessionError::new(
                "Stored session belongs to another Keeper endpoint",
            ));
        }
        if expired(session, Utc::now())? {
            self.emit(StorageEvent::SessionExpired {
                addr: session.addr(),
                expires_at: session.expires_at(),
            });
            if !is_evm_address(session.addr()) {
                let mut error = SaiError::new(Code::SessionExpired, INVALID_RENEWAL_ADDRESS);
                error.hint = None;
                return Err(SessionError(error));
            }
            let mut error = SaiError::new(
                Code::SessionExpired,
                "Trading session expired; sign in again",
            );
            error.hint = if session.key_name().is_some() || session.is_imported() {
                let (command, guidance) = self.renewal_fields(session);
                if let Some(guidance) = guidance {
                    error.message.push_str(&format!(". {guidance}"));
                }
                command
            } else {
                Some(self.sign_in_hint(endpoint, Some(session.addr())))
            };
            return Err(SessionError(error));
        }
        Ok(())
    }

    fn sign_in_hint(&self, endpoint: &str, addr: Option<&str>) -> String {
        let mut args = vec!["auth", "login", "NAME"];
        if let Some(addr) = addr {
            args.extend(["--addr", addr]);
        }
        self.keeper_command_hint(endpoint, &args)
    }

    pub fn keeper_command_hint(&self, endpoint: &str, args: &[&str]) -> String {
        let origin = keeper_origin(endpoint);
        let location = keeper_location(origin.as_deref());
        self.keeper_command(endpoint, origin.as_deref(), &location, args)
    }

    fn keeper_command(
        &self,
        endpoint: &str,
        origin: Option<&str>,
        location: &str,
        args: &[&str],
    ) -> String {
        let flag = if origin == Some(endpoint)
            && self
                .trader
                .parent()
                .is_some_and(|home| log::redact(home, endpoint) == endpoint)
        {
            shell_quote(endpoint)
        } else {
            format!(
                r#""${{SAI_RECONCILE_ENDPOINT:?Set SAI_RECONCILE_ENDPOINT to the original Keeper URL{location}}}""#
            )
        };
        format!("sai --endpoint {flag} {}", shell_join(args))
    }

    pub fn renewal_fields(&self, session: &Session) -> (Option<String>, Option<String>) {
        if !is_evm_address(session.addr()) {
            return (None, Some(INVALID_RENEWAL_ADDRESS.into()));
        }
        let args: &[&str] = if session.key_name().is_some() {
            &["auth", "renew", session.addr()]
        } else {
            &["auth", "import", "--addr", session.addr()]
        };
        let home = self.trader.parent();
        let endpoint = session.endpoint().unwrap_or_default();
        let origin = keeper_origin(endpoint)
            .filter(|origin| home.is_some_and(|home| log::redact(home, origin) == *origin));
        let public_default = endpoint == MAINNET_HTTP
            && home.is_some_and(|home| log::redact(home, endpoint) == endpoint);
        let route = if public_default {
            Some(endpoint)
        } else {
            origin.as_deref()
        };
        let mut guidance = Vec::new();
        if route != Some(endpoint) {
            let location = keeper_location(origin.as_deref());
            guidance.push(format!(
                "Reuse the Keeper endpoint{location} used to sign in."
            ));
        }
        if session.key_name().is_none() && !session.is_imported() {
            guidance.push("You can also sign in again with your Foundry keystore.".into());
        }
        (
            Some(self.keeper_command(endpoint, route, "", args)),
            (!guidance.is_empty()).then(|| guidance.join(" ")),
        )
    }

    pub fn expiry_notice(&self, session: &Session, left: TimeDelta) -> Option<ExpiryNotice> {
        let when = expiry_warning_time(left)?;
        let (renewal_command, renewal_guidance) = self.renewal_fields(session);
        Some(ExpiryNotice {
            expiry_warning: format!(
                "Sai session for {} expires in {when}.",
                escape_warning_text(session.addr())
            ),
            renewal_command,
            renewal_guidance,
        })
    }
}

fn keeper_origin(endpoint: &str) -> Option<String> {
    let origin = endpoint_origin(endpoint);
    (origin != "[redacted url]").then_some(origin)
}

fn keeper_location(origin: Option<&str>) -> String {
    origin.map_or_else(String::new, |origin| format!(" at {origin}"))
}

fn shell_join(args: &[&str]) -> String {
    args.iter()
        .map(|text| shell_quote(text))
        .collect::<Vec<_>>()
        .join(" ")
}

fn shell_quote(text: &str) -> String {
    if !text.is_empty()
        && text
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&byte))
    {
        text.into()
    } else {
        format!("'{}'", text.replace('\'', "'\"'\"'"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expiry_warning_window_and_display_thresholds() {
        for (seconds, expected) in [
            (-1, None),
            (0, None),
            (1, Some("under 1 min")),
            (59, Some("under 1 min")),
            (60, Some("1 min")),
            (120 * 60 - 1, Some("119 min")),
            (120 * 60, Some("2 h")),
            (3 * 24 * 3600 - 1, Some("71 h")),
            (3 * 24 * 3600, None),
            (3 * 24 * 3600 + 1, None),
        ] {
            assert_eq!(
                expiry_warning_time(TimeDelta::seconds(seconds)).as_deref(),
                expected
            );
        }
    }

    #[test]
    fn python_iso_expiry_variants_compare_as_utc() {
        let base = DateTime::parse_from_rfc3339("2099-01-01T00:00:00Z").unwrap();
        for text in [
            "2099-01-01T00:00:00+0000",
            "2099-01-01T00:00:00+00",
            "20990101 053000+0530",
            "2099-01-01T01:30:15.1234567+01:30:15.123456",
            "2099-01-01T00:00:01,5+000001,5",
            "2099-W01-4⭐00Z",
            "2099W014T00Z",
        ] {
            assert_eq!(parse_expiry(text), Some(base.to_utc()), "{text}");
        }
        for text in [
            "2099-01-01T00:00:00",
            "2099-01-01T00:00:60Z",
            "2099-01-01T00:00:00+24",
            "2099-01-01T00:00:00+00:0000",
        ] {
            assert_eq!(parse_expiry(text), None, "{text}");
        }
    }
}
