//! Endpoint and expiry checks shared by the CLI and other front ends.

use chrono::{DateTime, Utc};

use crate::storage::{Session, SessionError, Storage, StorageError, StorageEvent};

pub fn expired(session: &Session, now: DateTime<Utc>) -> Result<bool, SessionError> {
    let expires_at = DateTime::parse_from_rfc3339(session.expires_at())
        .map_err(|_| SessionError::new("Stored session has an invalid expiry"))?;
    Ok(expires_at <= now)
}

impl Storage<'_> {
    pub fn load_active_session(
        &self,
        endpoint: &str,
        addr: Option<&str>,
    ) -> Result<Session, StorageError> {
        let session = self.load_session(addr)?;
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
            return Err(SessionError::new("Trading session expired; sign in again"));
        }
        Ok(())
    }
}
