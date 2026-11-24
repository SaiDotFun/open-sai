use std::{collections::BTreeMap, fs, io, path::Path};

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
    SessionError, Storage, StorageError, StorageEvent, endpoint_origin,
    private_file, remove_if_exists,
};
use crate::errors::{Code, SaiError};

const SESSIONS: &str = "sessions.json";
const LEGACY_SESSION: &str = "session.json";
const SESSION_KEY_SEED: &[u8] = b"sai-cli keyring-backend test; not a secret";
const VERSION: u64 = 3;
const AAD: &[u8] = b"sai-sessions-v3";
const V2_AAD: &[u8] = b"sai-session-v2-test";
const SIGN_IN_AGAIN: &str =
    "run `sai auth import` or `sai auth login NAME` again";
const NO_SESSION: &str =
    "No Sai session; run `sai auth import` or `sai auth login NAME`";

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
    #[serde(rename = "expiresAt")]
    expires_at: String,
    #[serde(flatten)]
    extra: BTreeMap<String, Value>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionSummary<'a> {
    pub addr: &'a str,
    pub expires_at: &'a str,
    pub endpoint: String,
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
            expires_at,
            extra: BTreeMap::from([(
                "endpoint".to_owned(),
                Value::String(endpoint),
            )]),
        }
    }

    pub fn token(&self) -> &str {
        &self.token
    }

    pub fn addr(&self) -> &str {
        &self.addr
    }

    pub fn expires_at(&self) -> &str {
        &self.expires_at
    }

    pub fn endpoint(&self) -> Option<&str> {
        self.extra.get("endpoint").and_then(Value::as_str)
    }

    pub fn summary(&self) -> Result<SessionSummary<'_>, StorageError> {
        let endpoint = self.endpoint().ok_or_else(|| {
            SessionError::new("Stored session is missing its Keeper endpoint")
        })?;
        Ok(SessionSummary {
            addr: self.addr(),
            expires_at: self.expires_at(),
            endpoint: endpoint_origin(endpoint),
        })
    }
}

fn serialize_token<S: Serializer>(
    token: &str,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(token)
}

fn deserialize_token<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Zeroizing<String>, D::Error> {
    String::deserialize(deserializer).map(Zeroizing::new)
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
    pub fn iter(&self) -> impl Iterator<Item = (&Session, bool)> {
        self.sessions.iter().map(|(addr, session)| {
            (&session.0, Some(addr) == self.active.as_ref())
        })
    }

    fn find(&self, addr: &str) -> Result<String, SessionError> {
        let key = addr.to_lowercase();
        if self.sessions.contains_key(&key) {
            Ok(key)
        } else {
            Err(SessionError(SaiError::new(
                Code::InvalidRequest,
                format!(
                    "{addr} is not connected; run `sai auth list` to see connected \
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
    let key: Zeroizing<[u8; 32]> =
        Zeroizing::new(Sha256::digest(SESSION_KEY_SEED).into());
    AesGcm::new((&*key).into())
}

fn decrypt<T: for<'de> Deserialize<'de>>(
    fields: &Value,
    aad: &[u8],
) -> Result<T, SessionError> {
    let cannot_unlock = || session_error("Cannot unlock session");
    let decode = |key: &str| {
        let field = fields[key].as_str().ok_or_else(cannot_unlock)?;
        STANDARD.decode(field).map_err(|_| cannot_unlock())
    };
    let nonce: [u8; 12] =
        decode("nonce")?.try_into().map_err(|_| cannot_unlock())?;
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
        let path = self.trader.join(SESSIONS);
        let fields = match read_envelope(&path)? {
            Some(fields) => fields,
            None => {
                if let Some(fields) =
                    read_envelope(&self.trader.join(LEGACY_SESSION))?
                {
                    return Ok(load_v2(&fields)?);
                }
                // Another process may have published v3 and removed v2 between our reads.
                let Some(fields) = read_envelope(&path)? else {
                    return Ok(Accounts::default());
                };
                fields
            }
        };
        if fields["version"].as_u64() != Some(VERSION) {
            return Err(session_error("Unsupported session version").into());
        }
        let StoredAccounts(mut accounts) = decrypt(&fields, AAD)?;
        accounts
            .active
            .take_if(|addr| !accounts.sessions.contains_key(addr));
        Ok(accounts)
    }

    fn save_accounts(&self, accounts: &Accounts) -> Result<(), StorageError> {
        let cannot_save = || SessionError::new("Cannot encrypt session");
        let mut nonce = [0; 12];
        OsRng
            .try_fill_bytes(&mut nonce)
            .map_err(|_| cannot_save())?;
        let mut plaintext = Zeroizing::new(Vec::new());
        let mut serializer = serde_json::Serializer::new(&mut *plaintext);
        Accounts::serialize(accounts, &mut serializer)
            .map_err(|_| cannot_save())?;
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
        serde_json::to_writer(temp.as_file_mut(), &envelope)
            .map_err(io::Error::other)?;
        temp.persist(self.trader.join(SESSIONS))
            .map_err(|error| error.error)?;
        remove_if_exists(&self.trader.join(LEGACY_SESSION))?;
        Ok(())
    }

    fn lock_sessions(&self) -> io::Result<fs::File> {
        let lock = private_file(&self.directory()?.join("sessions.lock"))?;
        lock.lock()?;
        Ok(lock)
    }

    pub fn save_session(&self, session: Session) -> Result<(), StorageError> {
        let lock = self.lock_sessions()?;
        let mut accounts = match self.load_accounts() {
            Ok(accounts) => accounts,
            Err(StorageError::Session(_)) => Accounts::default(),
            Err(error) => return Err(error),
        };
        let key = session.addr.to_lowercase();
        accounts
            .sessions
            .insert(key.clone(), StoredSession(session));
        accounts.active = Some(key.clone());
        self.save_accounts(&accounts)?;
        drop(lock);
        let session = &accounts.sessions[&key].0;
        self.emit(StorageEvent::SessionSaved {
            addr: session.addr(),
            expires_at: session.expires_at(),
            endpoint: session.endpoint().map(endpoint_origin).as_deref(),
            connected_accounts: accounts.sessions.len(),
        });
        Ok(())
    }

    pub fn load_session(
        &self,
        addr: Option<&str>,
    ) -> Result<Session, StorageError> {
        let mut accounts = self.load_accounts()?;
        let key = if let Some(addr) = addr {
            accounts.find(addr).map_err(|error| {
                SessionError(SaiError::new(Code::SignedOut, error.0.message))
            })?
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
        let lock = self.lock_sessions()?;
        let mut accounts = self.load_accounts()?;
        let key = accounts.find(addr)?;
        accounts.active = Some(key.clone());
        self.save_accounts(&accounts)?;
        drop(lock);
        let session =
            accounts.sessions.remove(&key).expect("connected account").0;
        self.emit(StorageEvent::SessionActive {
            addr: session.addr(),
        });
        Ok(session)
    }

    pub fn session_exists(&self) -> bool {
        self.trader.join(SESSIONS).is_file()
            || self.trader.join(LEGACY_SESSION).is_file()
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
            Err(StorageError::Session(_)) if addr.is_none() => {
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
