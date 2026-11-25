use std::{collections::BTreeMap, fs, io, path::Path, time::Duration};

use aes::Aes256;
use aes_gcm::{
    AesGcm, KeyInit, Nonce,
    aead::{AeadInPlace, OsRng, consts::U12, rand_core::RngCore},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tempfile::Builder;
use zeroize::Zeroizing;

use super::{
    SessionError, Storage, StorageError, StorageEvent, endpoint_origin, private_file,
    remove_if_exists,
};
use crate::errors::{Code, SaiError};

const SESSIONS: &str = "sessions.json";
const LEGACY_SESSION: &str = "session.json";
const SESSION_KEY_SEED: &[u8] = b"sai-cli keyring-backend test; not a secret";
const VERSION: u64 = 3;
const AAD: &[u8] = b"sai-sessions-v3";
const V2_AAD: &[u8] = b"sai-session-v2-test";
const SIGN_IN_AGAIN: &str = "run `sai auth import` or `sai auth login NAME` again";
const REQUESTED_ACCOUNT: &str = "Requested account";
const NO_SESSION: &str = "No Sai session; run `sai auth import` or `sai auth login NAME`";

/// A stored account session. Call `summary` to display it without the token.
#[derive(Deserialize, Serialize)]
#[serde(remote = "Session")]
pub struct Session {
    #[serde(
        serialize_with = "serialize_token",
        deserialize_with = "deserialize_token"
    )]
    token: Zeroizing<String>,
    #[serde(rename = "evmAddress")]
    addr: String,
    #[serde(rename = "expiresAt", default, deserialize_with = "deserialize_expiry")]
    expires_at: Option<String>,
    #[serde(flatten)]
    extra: BTreeMap<String, Value>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionSummary<'a> {
    pub addr: &'a str,
    pub expires_at: &'a str,
    pub endpoint: Option<String>,
}

impl Session {
    pub fn new(
        token: Zeroizing<String>,
        addr: String,
        expires_at: String,
        endpoint: String,
    ) -> Self {
        Self {
            token,
            addr,
            expires_at: Some(expires_at),
            extra: BTreeMap::from([("endpoint".to_owned(), Value::String(endpoint))]),
        }
    }

    pub fn token(&self) -> &str {
        &self.token
    }

    pub fn addr(&self) -> &str {
        &self.addr
    }

    pub fn expires_at(&self) -> &str {
        self.expires_at
            .as_deref()
            .expect("stored expiry is checked on read")
    }

    pub fn endpoint(&self) -> Option<&str> {
        self.extra.get("endpoint").and_then(Value::as_str)
    }

    pub fn checked_endpoint(&self) -> Result<Option<&str>, SessionError> {
        match self.extra.get("endpoint") {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(endpoint)) => Ok(Some(endpoint)),
            _ => Err(session_error("Stored session has an invalid endpoint")),
        }
    }

    pub fn checked_address(&self, key: &str) -> Result<&str, SessionError> {
        if !crate::keeper::is_evm_address(self.addr()) || self.addr().to_ascii_lowercase() != key {
            return Err(session_error("Stored session has an invalid address"));
        }
        Ok(self.addr())
    }

    pub fn key_name(&self) -> Option<&str> {
        self.extra
            .get("keyName")
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
    }

    pub fn has_named_signer(&self) -> bool {
        self.key_name().is_some()
    }

    pub fn is_imported(&self) -> bool {
        self.extra
            .get("source")
            .is_some_and(|source| source == "imported")
    }

    pub(crate) fn set_key_name(&mut self, name: &str) {
        self.extra
            .insert("keyName".into(), Value::String(name.into()));
    }

    pub(crate) fn mark_imported(&mut self) {
        self.extra
            .insert("source".into(), Value::String("imported".into()));
    }

    pub fn summary(&self) -> SessionSummary<'_> {
        SessionSummary {
            addr: self.addr(),
            expires_at: self.expires_at(),
            endpoint: self.endpoint().map(endpoint_origin),
        }
    }

    fn same_as(&self, previous: &Self) -> bool {
        self.token() == previous.token()
            && self.addr == previous.addr
            && self.expires_at == previous.expires_at
            && self.extra == previous.extra
    }
}

fn serialize_token<S: Serializer>(token: &str, serializer: S) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(token)
}

fn deserialize_token<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Zeroizing<String>, D::Error> {
    String::deserialize(deserializer).map(Zeroizing::new)
}

// Decode the other fields before classifying a missing or non-string expiry.
fn deserialize_expiry<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<String>, D::Error> {
    match Value::deserialize(deserializer)? {
        Value::String(expiry) => Ok(Some(expiry)),
        _ => Ok(None),
    }
}

#[derive(Deserialize, Serialize)]
struct StoredSession(#[serde(with = "Session")] Session);

#[derive(Default, Deserialize, Serialize)]
#[serde(remote = "Accounts")]
pub struct Accounts {
    active: Option<String>,
    sessions: BTreeMap<String, StoredSession>,
}

#[derive(Deserialize)]
struct StoredAccounts(#[serde(with = "Accounts")] Accounts);

impl Accounts {
    pub(crate) fn selected(&self, addr: Option<&str>) -> Option<&Session> {
        let key = addr.map(str::to_lowercase);
        self.sessions
            .get(key.as_ref().or(self.active.as_ref())?)
            .map(|session| &session.0)
    }

    pub fn iter(&self) -> impl Iterator<Item = (&Session, bool)> {
        self.entries().map(|(_, session, active)| (session, active))
    }

    pub fn entries(&self) -> impl Iterator<Item = (&str, &Session, bool)> {
        self.sessions.iter().map(|(addr, session)| {
            (
                addr.as_str(),
                &session.0,
                Some(addr) == self.active.as_ref(),
            )
        })
    }

    fn find_with_label(&self, addr: &str, label: &str) -> Result<String, SessionError> {
        let key = addr.to_lowercase();
        if self.sessions.contains_key(&key) {
            Ok(key)
        } else {
            Err(SessionError(SaiError::new(
                Code::InvalidRequest,
                format!(
                    "{label} is not connected; run `sai auth list` to see connected \
                 accounts, or sign in with `sai auth login NAME` or `sai auth import`"
                ),
            )))
        }
    }
}

pub enum RemovedSession {
    Account(String),
    Unreadable,
}

#[derive(Serialize)]
struct Envelope {
    version: u64,
    nonce: String,
    ciphertext: String,
}

fn session_error(message: &str) -> SessionError {
    SessionError::new(format!("{message}; {SIGN_IN_AGAIN}"))
}

fn read_envelope(path: &Path) -> Result<Option<Value>, SessionError> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map(Some)
            .map_err(|_| session_error("Stored session is unreadable")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(session_error("Stored session is unreadable")),
    }
}

fn cipher() -> AesGcm<Aes256, U12> {
    // This built-in compatibility key keeps tokens out of plaintext files.
    // The private file modes control access to the store.
    let key: Zeroizing<[u8; 32]> = Zeroizing::new(Sha256::digest(SESSION_KEY_SEED).into());
    AesGcm::new((&*key).into())
}

fn decrypt<T: for<'de> Deserialize<'de>>(fields: &Value, aad: &[u8]) -> Result<T, SessionError> {
    let cannot_unlock = || session_error("Cannot unlock session");
    let decode = |key: &str| {
        let field = fields[key].as_str().ok_or_else(cannot_unlock)?;
        STANDARD.decode(field).map_err(|_| cannot_unlock())
    };
    let nonce: [u8; 12] = decode("nonce")?.try_into().map_err(|_| cannot_unlock())?;
    let mut plaintext = Zeroizing::new(decode("ciphertext")?);
    cipher()
        .decrypt_in_place(&Nonce::from(nonce), aad, &mut *plaintext)
        .map_err(|_| cannot_unlock())?;
    serde_json::from_slice(&plaintext).map_err(|_| cannot_unlock())
}

fn load_v2(envelope: &Value) -> Result<Accounts, SessionError> {
    let version = envelope["version"].as_u64();
    let backend = envelope["backend"].as_str();
    if version == Some(1) || (version == Some(2) && backend == Some("file")) {
        return Err(session_error(
            "Stored session is passphrase-encrypted, which sai no longer supports",
        ));
    }
    if version != Some(2) || backend != Some("test") {
        return Err(session_error("Unsupported session version"));
    }
    let session: StoredSession = decrypt(envelope, V2_AAD)?;
    let addr = session.0.addr.to_lowercase();
    Ok(Accounts {
        active: Some(addr.clone()),
        sessions: BTreeMap::from([(addr, session)]),
    })
}

impl Storage<'_> {
    pub fn load_accounts(&self) -> Result<Accounts, StorageError> {
        let accounts = self.read_accounts().map_err(StorageError::SessionData)?;
        if accounts
            .iter()
            .any(|(session, _)| session.expires_at.is_none())
        {
            return Err(StorageError::InvalidSessionExpiry);
        }
        Ok(accounts)
    }

    fn read_accounts(&self) -> Result<Accounts, SessionError> {
        let path = self.trader.join(SESSIONS);
        let fields = match read_envelope(&path)? {
            Some(fields) => fields,
            None => {
                if let Some(fields) = read_envelope(&self.trader.join(LEGACY_SESSION))? {
                    return load_v2(&fields);
                }
                // Another process may have published v3 and removed v2 between our reads.
                let Some(fields) = read_envelope(&path)? else {
                    return Ok(Accounts::default());
                };
                fields
            }
        };
        if fields["version"].as_u64() != Some(VERSION) {
            return Err(session_error("Unsupported session version"));
        }
        let StoredAccounts(mut accounts) = decrypt(&fields, AAD)?;
        accounts
            .active
            .take_if(|addr| !accounts.sessions.contains_key(addr));
        Ok(accounts)
    }

    fn save_accounts(&self, accounts: &Accounts) -> Result<(), StorageError> {
        self.write_accounts(accounts)?;
        remove_if_exists(&self.trader.join(LEGACY_SESSION))?;
        Ok(())
    }

    fn write_accounts(&self, accounts: &Accounts) -> Result<(), StorageError> {
        let cannot_save = || SessionError::new("Cannot encrypt session");
        let mut nonce = [0; 12];
        OsRng
            .try_fill_bytes(&mut nonce)
            .map_err(|_| cannot_save())?;
        let mut plaintext = Zeroizing::new(Vec::new());
        let mut serializer = serde_json::Serializer::new(&mut *plaintext);
        Accounts::serialize(accounts, &mut serializer).map_err(|_| cannot_save())?;
        let tag = cipher()
            .encrypt_in_place_detached(&Nonce::from(nonce), AAD, &mut plaintext)
            .map_err(|_| cannot_save())?;
        plaintext.extend_from_slice(&tag);
        let envelope = Envelope {
            version: VERSION,
            nonce: STANDARD.encode(nonce),
            ciphertext: STANDARD.encode(&*plaintext),
        };
        let mut temp = Builder::new()
            .prefix(".sessions-")
            .tempfile_in(&self.trader)?;
        serde_json::to_writer(temp.as_file_mut(), &envelope).map_err(io::Error::other)?;
        temp.persist(self.trader.join(SESSIONS))
            .map_err(|error| error.error)?;
        Ok(())
    }

    fn session_lock_file(&self) -> io::Result<fs::File> {
        private_file(&self.directory()?.join("sessions.lock"))
    }

    fn lock_sessions(&self) -> io::Result<fs::File> {
        let lock = self.session_lock_file()?;
        lock.lock()?;
        Ok(lock)
    }

    pub fn save_session(&self, session: Session) -> Result<(), StorageError> {
        self.save_session_locked(self.lock_sessions()?, session)
    }

    /// Wait for the session lock without blocking cancellation in the caller.
    /// Once acquired, keep the lock through the synchronous commit.
    pub async fn save_session_async(&self, session: Session) -> Result<(), StorageError> {
        let lock = self.session_lock_file()?;
        loop {
            match lock.try_lock() {
                Ok(()) => return self.save_session_locked(lock, session),
                Err(fs::TryLockError::WouldBlock) => {
                    tokio::time::sleep(Duration::from_millis(25)).await;
                }
                Err(fs::TryLockError::Error(error)) => return Err(error.into()),
            }
        }
    }

    fn save_session_locked(&self, lock: fs::File, session: Session) -> Result<(), StorageError> {
        let mut accounts = match self.load_accounts() {
            Ok(accounts) => accounts,
            Err(StorageError::SessionData(_) | StorageError::InvalidSessionExpiry) => {
                Accounts::default()
            }
            Err(error) => return Err(error),
        };
        let key = session.addr.to_lowercase();
        accounts
            .sessions
            .insert(key.clone(), StoredSession(session));
        accounts.active = Some(key.clone());
        self.save_accounts(&accounts)?;
        drop(lock);
        self.session_saved(&accounts, &key);
        Ok(())
    }

    /// Replace the account only if it still contains the session that was renewed.
    pub fn replace_session(
        &self,
        previous: &Session,
        renewed: Session,
    ) -> Result<(), StorageError> {
        let lock = self.lock_sessions()?;
        let mut accounts = self.load_accounts()?;
        let key = previous.addr.to_lowercase();
        if !accounts
            .sessions
            .get(&key)
            .is_some_and(|session| session.0.same_as(previous))
        {
            return Err(SessionError(SaiError::new(
                Code::InvalidRequest,
                "Stored session changed during renewal. Run sai auth status.",
            ))
            .into());
        }
        if crate::session::expired(&renewed, chrono::Utc::now())? {
            return Err(SessionError(SaiError::new(
                Code::KeeperError,
                "Keeper returned an already expired session",
            ))
            .into());
        }
        accounts
            .sessions
            .insert(key.clone(), StoredSession(renewed));
        self.save_accounts(&accounts)?;
        drop(lock);
        self.session_saved(&accounts, &key);
        Ok(())
    }

    fn session_saved(&self, accounts: &Accounts, key: &str) {
        let session = &accounts.sessions[key].0;
        self.emit(StorageEvent::SessionSaved {
            addr: session.addr(),
            expires_at: session.expires_at(),
            endpoint: session.endpoint().map(endpoint_origin).as_deref(),
            connected_accounts: accounts.sessions.len(),
        });
    }

    pub fn load_session(&self, addr: Option<&str>) -> Result<Session, StorageError> {
        self.load_session_with_label(addr, addr)
    }

    /// Read a session for an MCP tool without quoting its untrusted address argument.
    pub fn load_session_for_mcp(&self, addr: Option<&str>) -> Result<Session, StorageError> {
        self.load_session_with_label(addr, Some(REQUESTED_ACCOUNT))
    }

    fn load_session_with_label(
        &self,
        addr: Option<&str>,
        label: Option<&str>,
    ) -> Result<Session, StorageError> {
        let mut accounts = self.load_accounts()?;
        let key = if let Some(addr) = addr {
            accounts
                .find_with_label(addr, label.unwrap_or(addr))
                .map_err(|error| SessionError(SaiError::new(Code::SignedOut, error.0.message)))?
        } else {
            accounts.active.take().ok_or_else(|| {
                if accounts.sessions.is_empty() {
                    SessionError(SaiError::new(Code::SignedOut, NO_SESSION))
                } else {
                    let mut error = SaiError::new(
                        Code::SessionUnavailable,
                        "No active Sai account; run `sai auth use ADDR` with an addr from `sai auth list`",
                    );
                    error.hint = Some("sai auth list".into());
                    SessionError(error)
                }
            })?
        };
        Ok(accounts.sessions.remove(&key).expect("connected account").0)
    }

    pub fn use_session(&self, addr: &str) -> Result<Session, StorageError> {
        self.select_session(
            addr,
            addr,
            |_, session, _| {
                crate::session::session_expiry(session)?;
                Ok(())
            },
            |accounts| self.save_accounts(accounts),
        )
        .map(|(session, ())| session)
    }

    /// Prepare an account-switch reply from the new selection before committing it.
    pub fn use_session_prepared<T>(
        &self,
        addr: &str,
        prepare: impl FnOnce(&str, &Session, &Accounts) -> Result<T, StorageError>,
    ) -> Result<T, StorageError> {
        self.select_session(addr, REQUESTED_ACCOUNT, prepare, |accounts| {
            self.write_accounts(accounts)?;
            if remove_if_exists(&self.trader.join(LEGACY_SESSION)).is_err() {
                eprintln!("Could not remove legacy session file trader/session.json after saving accounts.");
            }
            Ok(())
        })
        .map(|(_, report)| report)
    }

    fn select_session<T>(
        &self,
        addr: &str,
        label: &str,
        prepare: impl FnOnce(&str, &Session, &Accounts) -> Result<T, StorageError>,
        save: impl FnOnce(&Accounts) -> Result<(), StorageError>,
    ) -> Result<(Session, T), StorageError> {
        let lock = self.lock_sessions()?;
        let mut accounts = self.load_accounts()?;
        let key = accounts.find_with_label(addr, label)?;
        accounts.active = Some(key.clone());
        let report = prepare(&key, &accounts.sessions[&key].0, &accounts)?;
        save(&accounts)?;
        drop(lock);
        let session = accounts.sessions.remove(&key).expect("connected account").0;
        self.emit(StorageEvent::SessionActive {
            addr: session.addr(),
        });
        Ok((session, report))
    }

    pub fn session_exists(&self) -> bool {
        self.trader.join(SESSIONS).is_file() || self.trader.join(LEGACY_SESSION).is_file()
    }

    fn remove_session_files(&self) -> io::Result<()> {
        remove_if_exists(&self.trader.join(LEGACY_SESSION))?;
        remove_if_exists(&self.trader.join(SESSIONS))
    }

    pub fn delete_session(
        &self,
        addr: Option<&str>,
    ) -> Result<Option<RemovedSession>, StorageError> {
        let lock = self.lock_sessions()?;
        if !self.session_exists() {
            return Ok(None);
        }
        let mut accounts = match self.load_accounts() {
            Ok(accounts) => accounts,
            Err(StorageError::SessionData(_) | StorageError::InvalidSessionExpiry)
                if addr.is_none() =>
            {
                self.remove_session_files()?;
                drop(lock);
                self.emit(StorageEvent::UnreadableSessionRemoved);
                return Ok(Some(RemovedSession::Unreadable));
            }
            Err(error) => return Err(error),
        };
        let Some(key) = addr
            .map(str::to_lowercase)
            .or_else(|| accounts.active.clone())
        else {
            return Ok(None);
        };
        let Some(removed) = accounts.sessions.remove(&key) else {
            return Ok(None);
        };
        if accounts.active.as_ref() == Some(&key) {
            accounts.active = None;
        }
        if accounts.sessions.is_empty() {
            self.remove_session_files()?;
        } else {
            self.save_accounts(&accounts)?;
        }
        drop(lock);
        let addr = removed.0.addr;
        self.emit(StorageEvent::SessionRemoved {
            addr: &addr,
            connected_accounts: accounts.sessions.len(),
        });
        Ok(Some(RemovedSession::Account(addr)))
    }
}
