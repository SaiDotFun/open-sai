//! Endpoint and expiry checks shared by the CLI and other front ends.

use std::sync::LazyLock;

use chrono::{DateTime, Datelike, NaiveDate, TimeDelta, Utc, Weekday};
use regex::Regex;

use crate::{
    errors::{Code, SaiError},
    storage::{Session, SessionError, Storage, StorageError, StorageEvent},
};

pub(crate) const ORIGINAL_KEEPER_HINT: &str =
    r#""${SAI_RECONCILE_ENDPOINT:?Set SAI_RECONCILE_ENDPOINT to the original Keeper URL}""#;

pub fn expired(session: &Session, now: DateTime<Utc>) -> Result<bool, SessionError> {
    let expires_at = parse_expiry(session.expires_at())
        .ok_or_else(|| SessionError::new(
            "Stored session has an invalid expiry; run `sai auth import` or `sai auth login NAME` again",
        ))?;
    Ok(expires_at <= now)
}

// Stored Keeper expiries follow Python's datetime.fromisoformat. Convert to
// UTC for comparisons and retain the original text in the session.
pub(crate) fn parse_expiry(text: &str) -> Option<DateTime<Utc>> {
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
        self.load_active_session_with_hint(endpoint, addr, |addr| {
            self.sign_in_hint(endpoint, addr, false)
        })
    }

    pub(crate) fn load_active_trade_session(
        &self,
        endpoint: &str,
        addr: Option<&str>,
    ) -> Result<Session, StorageError> {
        self.load_active_session_with_hint(endpoint, addr, |addr| {
            self.sign_in_hint(endpoint, addr, true)
        })
    }

    fn load_active_session_with_hint(
        &self,
        endpoint: &str,
        addr: Option<&str>,
        hint: impl Fn(Option<&str>) -> String,
    ) -> Result<Session, StorageError> {
        let session = self.load_session(addr).map_err(|mut error| {
            if let StorageError::Session(SessionError(failure)) = &mut error
                && failure.code == Code::SignedOut
            {
                failure.hint = Some(hint(addr));
            }
            error
        })?;
        self.check_active_with_hint(&session, endpoint, hint)?;
        Ok(session)
    }

    pub fn check_active(&self, session: &Session, endpoint: &str) -> Result<(), SessionError> {
        self.check_active_with_hint(session, endpoint, |addr| {
            self.sign_in_hint(endpoint, addr, false)
        })
    }

    pub(crate) fn check_active_trade(
        &self,
        session: &Session,
        endpoint: &str,
    ) -> Result<(), SessionError> {
        self.check_active_with_hint(session, endpoint, |addr| {
            self.sign_in_hint(endpoint, addr, true)
        })
    }

    fn check_active_with_hint(
        &self,
        session: &Session,
        endpoint: &str,
        hint: impl FnOnce(Option<&str>) -> String,
    ) -> Result<(), SessionError> {
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
            let mut error = SaiError::new(
                Code::SessionExpired,
                "Trading session expired; sign in again",
            );
            error.hint = Some(hint(Some(session.addr())));
            return Err(SessionError(error));
        }
        Ok(())
    }

    fn sign_in_hint(&self, endpoint: &str, addr: Option<&str>, origin_only: bool) -> String {
        static PLAIN_KEEPER_URL: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(r"\Ahttps?://(?:[a-zA-Z0-9.-]+|\[[0-9a-fA-F:.]+\])(?::[0-9]+)?/query\z")
                .expect("valid Keeper URL pattern")
        });
        let flag = if origin_only {
            self.keeper_hint_flag(endpoint)
        } else if PLAIN_KEEPER_URL.is_match(endpoint)
            && self
                .trader
                .parent()
                .is_some_and(|home| crate::log::redact(home, endpoint) == endpoint)
        {
            shell_quote(endpoint)
        } else {
            ORIGINAL_KEEPER_HINT.into()
        };
        let mut hint = format!("sai --endpoint {flag} auth login NAME");
        if let Some(addr) = addr {
            hint.push_str(" --addr ");
            hint.push_str(&shell_quote(addr));
        }
        hint
    }
}

pub(crate) fn shell_quote(text: &str) -> String {
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
