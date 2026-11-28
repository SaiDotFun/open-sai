use std::path::Path;

use chrono::{DateTime, Utc};
use sai_core::{
    errors::{Code, SaiError},
    log, session,
    storage::{Accounts, Session, Storage, StorageError},
};
use serde_json::{Map, Value, json};

const NO_ADDR: &str = "No addr given and no stored Sai session. Pass addr, or sign in with `sai auth login NAME` or `sai auth import` in a terminal.";
const OTHER_ENDPOINT: &str = "No addr given, and the stored session belongs to a different Keeper endpoint than this server's keeper.endpoint, so its addr may not be the account meant here. Pass addr explicitly.";
const SIGN_IN: &str = "Ask the user to sign in by running `sai auth login NAME` or `sai auth import` in a terminal, then retry. Reads and most previews work without it.";

pub(super) enum StoredSession {
    Missing,
    Unreadable(SaiError),
    Present {
        session: Session,
        expired: bool,
        matches: bool,
    },
}

impl StoredSession {
    pub(super) fn load(storage: &Storage<'_>, endpoint: &str, addr: Option<&str>) -> Self {
        if !storage.session_exists() {
            return Self::Missing;
        }
        let session = match storage.load_session_for_mcp(addr) {
            Ok(session) => session,
            Err(error) => return Self::Unreadable(error.into()),
        };
        match session::expired(&session, Utc::now()) {
            Ok(expired) => Self::Present {
                matches: session.endpoint() == Some(endpoint),
                session,
                expired,
            },
            Err(error) => Self::Unreadable(error.into()),
        }
    }

    pub(super) fn refusal(&self) -> Option<SaiError> {
        let (code, message) = match self {
            Self::Present { matches: false, .. } => (
                Code::SessionUnavailable,
                "The signed-in Sai session belongs to a different Keeper endpoint than this server's.",
            ),
            Self::Present { expired: true, .. } => {
                (Code::SessionExpired, "The Sai trading session expired.")
            }
            Self::Present { .. } => return None,
            Self::Unreadable(error) => {
                return Some(SaiError::new(
                    Code::SessionUnavailable,
                    format!("{}, in a terminal.", error.message),
                ));
            }
            Self::Missing => (Code::SignedOut, "No Sai account is signed in."),
        };
        Some(SaiError::new(code, format!("{message} {SIGN_IN}")))
    }

    pub(super) fn trading(&self) -> Value {
        match self {
            Self::Missing => {
                json!({"available": false, "reason": "signed-out", "addr": null})
            }
            Self::Unreadable(error) => json!({
                "available": false, "reason": "signed-out", "addr": null, "error": error.message,
            }),
            Self::Present {
                session,
                expired,
                matches,
            } => {
                let reason = match (matches, expired) {
                    (false, _) => "other-endpoint",
                    (true, true) => "expired",
                    (true, false) => "signed-in",
                };
                json!({"available": *matches && !expired, "reason": reason, "addr": session.addr()})
            }
        }
    }

    pub(super) fn into_trade_session(self, optional: bool) -> Result<Option<Session>, SaiError> {
        if let Some(error) = self.refusal() {
            return if optional { Ok(None) } else { Err(error) };
        }
        let Self::Present { session, .. } = self else {
            return Err(SaiError::internal());
        };
        log::remember([session.token().to_owned()]);
        Ok(Some(session))
    }

    pub(super) fn summary(&self, storage: &Storage<'_>, home: &Path) -> Result<Value, SaiError> {
        let report = match self {
            Self::Missing => json!({"signedIn": false}),
            Self::Unreadable(error) => {
                json!({"signedIn": false, "error": error.message})
            }
            Self::Present {
                session,
                expired,
                matches,
            } => {
                log::remember([session.token().to_owned()]);
                let redact = log::session_field_redactor(home);
                let mut report = json!({
                    "signedIn": true,
                    "connectedAccounts": storage.load_accounts().map_or(1, |accounts| accounts.iter().count()),
                    "usable": *matches && !expired,
                });
                report
                    .as_object_mut()
                    .expect("session report")
                    .extend(auth_summary(session, &redact)?);
                report["expired"] = json!(expired);
                report["endpointMatches"] = json!(matches);
                if *expired {
                    let (command, guidance) = storage.renewal_fields(session);
                    add_renewal(&mut report, command, guidance, &redact);
                } else if *matches {
                    add_expiry_notice(
                        &mut report,
                        storage,
                        session,
                        session::session_expiry(session)?,
                        Utc::now(),
                        &redact,
                    );
                }
                report
            }
        };
        Ok(report)
    }
}

pub(super) fn auth_status(
    storage: &Storage<'_>,
    home: &Path,
    endpoint: &str,
    addr: Option<&str>,
) -> Result<Value, SaiError> {
    let stored = StoredSession::load(storage, endpoint, addr);
    if let Some(error) = stored.refusal() {
        return Err(error);
    }
    let StoredSession::Present { session, .. } = stored else {
        return Err(SaiError::internal());
    };
    log::remember([session.token().to_owned()]);
    let redact = log::session_field_redactor(home);
    let mut report = Value::Object(auth_summary(&session, &redact)?);
    let expiry = session::session_expiry(&session)?;
    add_expiry_notice(&mut report, storage, &session, expiry, Utc::now(), &redact);
    Ok(report)
}

pub(super) fn auth_list(
    storage: &Storage<'_>,
    home: &Path,
    endpoint: &str,
) -> Result<Value, SaiError> {
    let accounts = storage
        .load_accounts()
        .map_err(crate::auth::account_error)?;
    account_rows(storage, home, endpoint, &accounts).map_err(crate::auth::account_error)
}

fn account_rows(
    storage: &Storage<'_>,
    home: &Path,
    endpoint: &str,
    accounts: &Accounts,
) -> Result<Value, StorageError> {
    log::remember(
        accounts
            .iter()
            .map(|(session, _)| session.token().to_owned()),
    );
    let redact = log::session_field_redactor(home);
    let now = Utc::now();
    let rows = accounts
        .entries()
        .map(|(key, session, active)| {
            let expiry = session::session_expiry(session)?;
            let expired = expiry <= now;
            session.checked_address(key)?;
            let mut row = Value::Object(auth_summary(session, &redact)?);
            let matches = session.endpoint() == Some(endpoint);
            row["active"] = json!(active);
            row["expired"] = json!(expired);
            row["endpointMatches"] = json!(matches);
            row["usable"] = json!(matches && !expired);
            add_expiry_notice(&mut row, storage, session, expiry, now, &redact);
            Ok(row)
        })
        .collect::<Result<Vec<_>, StorageError>>()?;
    Ok(json!({"accounts": rows}))
}

pub(super) fn auth_use(
    storage: &Storage<'_>,
    home: &Path,
    endpoint: &str,
    addr: &str,
) -> Result<Value, SaiError> {
    storage
        .use_session_prepared(addr, |key, selected, accounts| {
            selected.checked_address(key)?;
            session::session_expiry(selected)?;
            account_rows(storage, home, endpoint, accounts)
        })
        .map_err(crate::auth::account_error)
}

fn auth_summary(
    session: &Session,
    redact: &impl Fn(&str) -> String,
) -> Result<Map<String, Value>, sai_core::storage::SessionError> {
    let endpoint = session.checked_endpoint()?.map(|endpoint| {
        let field = redact(endpoint);
        if field == log::REDACTED {
            field
        } else {
            redact(&log::origin(endpoint))
        }
    });
    Ok(Map::from_iter([
        ("addr".into(), json!(redact(session.addr()))),
        ("expiresAt".into(), json!(redact(session.expires_at()))),
        ("endpoint".into(), json!(endpoint)),
    ]))
}

fn add_expiry_notice(
    report: &mut Value,
    storage: &Storage<'_>,
    session: &Session,
    expiry: DateTime<Utc>,
    now: DateTime<Utc>,
    redact: &impl Fn(&str) -> String,
) {
    if let Some(notice) =
        storage.expiry_notice_for_address(session, expiry - now, &redact(session.addr()))
    {
        report["expiryWarning"] = json!(redact(&notice.expiry_warning));
        add_renewal(
            report,
            notice.renewal_command,
            notice.renewal_guidance,
            redact,
        );
    }
}

fn add_renewal(
    report: &mut Value,
    command: Option<String>,
    guidance: Option<String>,
    redact: &impl Fn(&str) -> String,
) {
    if let Some(command) = command {
        report["renewalCommand"] = json!(redact(&command));
    }
    if let Some(guidance) = guidance {
        report["renewalGuidance"] = json!(redact(&guidance));
    }
}

pub(super) fn resolve_addr(
    storage: &Storage<'_>,
    endpoint: &str,
    addr: Option<&str>,
) -> Result<Map<String, Value>, SaiError> {
    if let Some(addr) = addr {
        return Ok(Map::from_iter([
            ("addr".into(), json!(addr)),
            ("addrSource".into(), json!("argument")),
        ]));
    }
    let message = match StoredSession::load(storage, endpoint, None) {
        StoredSession::Missing => NO_ADDR.to_owned(),
        StoredSession::Unreadable(error) => {
            format!("{}. Or pass addr.", error.message)
        }
        StoredSession::Present { matches: false, .. } => OTHER_ENDPOINT.to_owned(),
        StoredSession::Present {
            session, expired, ..
        } => {
            return Ok(Map::from_iter([
                ("addr".into(), json!(session.addr())),
                ("addrSource".into(), json!("session")),
                ("sessionExpired".into(), json!(expired)),
                ("endpointMatches".into(), json!(true)),
            ]));
        }
    };
    Err(SaiError::new(Code::SessionUnavailable, message))
}
