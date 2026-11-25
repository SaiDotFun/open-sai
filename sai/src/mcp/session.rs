use chrono::Utc;
use sai_core::{
    errors::{Code, SaiError},
    log, session,
    storage::{Session, Storage},
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
                    (true, false) => "not-implemented",
                };
                json!({"available": false, "reason": reason, "addr": session.addr()})
            }
        }
    }

    pub(super) fn summary(&self, storage: &Storage<'_>) -> Value {
        match self {
            Self::Missing => json!({"signedIn": false}),
            Self::Unreadable(error) => {
                json!({"signedIn": false, "error": error.message})
            }
            Self::Present {
                session,
                expired,
                matches,
            } => json!({
                "signedIn": true,
                "connectedAccounts": storage.load_accounts().map_or(1, |accounts| accounts.iter().count()),
                "usable": *matches && !expired,
                "addr": session.addr(),
                "expiresAt": session.expires_at(),
                "endpoint": session.endpoint().map(log::origin),
                "expired": expired,
                "endpointMatches": matches,
            }),
        }
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
