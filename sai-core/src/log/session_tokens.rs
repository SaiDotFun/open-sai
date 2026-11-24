//! Read-only v0.1 session access for redaction on the writer thread.

use std::{
    fs::{self, File},
    io::{self, Read},
    path::Path,
    sync::LazyLock,
    time::SystemTime,
};

use aes_gcm::{
    Aes256Gcm, KeyInit,
    aead::{Aead, Payload},
};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::remember;

// This label is shared by the v0.1 test-backend format. File permissions
// protect these sessions. The key derivation input is public.
static KEY: LazyLock<[u8; 32]> =
    LazyLock::new(|| Sha256::digest(b"sai-cli keyring-backend test; not a secret").into());

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) struct FileStamp {
    modified: SystemTime,
    size: u64,
    #[cfg(unix)]
    inode: u64,
}

pub(super) fn stamp(path: &Path) -> Option<FileStamp> {
    #[cfg(unix)]
    use std::os::unix::fs::MetadataExt;

    let metadata = fs::metadata(path).ok()?;
    Some(FileStamp {
        modified: metadata.modified().ok()?,
        size: metadata.len(),
        #[cfg(unix)]
        inode: metadata.ino(),
    })
}

#[derive(Default)]
pub(super) struct Tokens {
    last: Option<[Option<FileStamp>; 2]>,
}

impl Tokens {
    pub fn refresh(&mut self, home: &Path) {
        let root = home.join("trader");
        let current = root.join("sessions.json");
        let legacy = root.join("session.json");
        let observed = [stamp(&current), stamp(&legacy)];
        if self.last == Some(observed) {
            return;
        }
        if let Some(tokens) = read_tokens(&current, &legacy) {
            remember(tokens);
            self.last = Some(observed);
        }
    }
}

fn read_tokens(current: &Path, legacy: &Path) -> Option<Vec<String>> {
    let current_envelope = read(current).ok()?;
    if current_envelope.is_none()
        && let Some(old) = read(legacy).ok()?
    {
        if old.get("version")?.as_u64()? != 2 || old.get("backend")?.as_str()? != "test" {
            return None;
        }
        let session = decrypt(&old, b"sai-session-v2-test")?;
        return Some(vec![token(&session)?]);
    }
    let current = match current_envelope {
        Some(envelope) => Some(envelope),
        None => read(current).ok()?,
    };
    let Some(current) = current else {
        return Some(Vec::new());
    };
    if current.get("version")?.as_u64()? != 3 {
        return None;
    }
    let accounts = decrypt(&current, b"sai-sessions-v3")?;
    accounts
        .get("sessions")?
        .as_object()?
        .values()
        .map(token)
        .collect()
}

fn token(session: &Value) -> Option<String> {
    session.get("evmAddress")?.as_str()?;
    session.get("expiresAt")?.as_str()?;
    Some(session.get("token")?.as_str()?.to_owned())
}

fn read(path: &Path) -> io::Result<Option<Value>> {
    #[cfg(unix)]
    let file = {
        use rustix::fs::{Mode, OFlags, open};
        open(path, OFlags::RDONLY | OFlags::NONBLOCK, Mode::empty())
            .map(File::from)
            .map_err(io::Error::from)
    };
    #[cfg(not(unix))]
    let file = File::open(path);

    let file = match file {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(None);
        }
        Err(error) => return Err(error),
    };
    if !file.metadata()?.is_file() {
        return Err(io::ErrorKind::InvalidData.into());
    }
    serde_json::from_reader(file.take(8 * 1024 * 1024))
        .map(Some)
        .map_err(io::Error::other)
}

fn decrypt(envelope: &Value, aad: &[u8]) -> Option<Value> {
    let nonce: [u8; 12] = STANDARD
        .decode(envelope.get("nonce")?.as_str()?)
        .ok()?
        .try_into()
        .ok()?;
    let ciphertext = STANDARD
        .decode(envelope.get("ciphertext")?.as_str()?)
        .ok()?;
    let cipher = Aes256Gcm::new_from_slice(KEY.as_ref()).ok()?;
    let clear = cipher
        .decrypt(
            &nonce.into(),
            Payload {
                msg: &ciphertext,
                aad,
            },
        )
        .ok()?;
    serde_json::from_slice(&clear).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        log::{Field, Level, LogWriter, Surface},
        settings::Environment,
    };
    use serde_json::json;
    use std::time::Duration;

    fn envelope(version: u8, payload: &Value, aad: &[u8]) -> Value {
        let nonce = [1; 12];
        let cipher = Aes256Gcm::new_from_slice(KEY.as_ref()).unwrap();
        let message = serde_json::to_vec(payload).unwrap();
        let ciphertext = cipher
            .encrypt(&nonce.into(), Payload { msg: &message, aad })
            .unwrap();
        json!({
            "version": version,
            "backend": "test",
            "nonce": STANDARD.encode(nonce),
            "ciphertext": STANDARD.encode(ciphertext),
        })
    }

    #[test]
    fn writer_scrubs_tokens_from_current_and_legacy_session_files() {
        for version in [2, 3] {
            let home = tempfile::tempdir().unwrap();
            let trader = home.path().join("trader");
            fs::create_dir(&trader).unwrap();
            let token = format!("fixture-opaque-log-session-v{version}");
            let session = json!({"token": token, "evmAddress": "fixture", "expiresAt": "fixture"});
            let (name, payload, aad) = if version == 2 {
                ("session.json", session, b"sai-session-v2-test".as_slice())
            } else {
                (
                    "sessions.json",
                    json!({"active": "fixture", "sessions": {"fixture": session}}),
                    b"sai-sessions-v3".as_slice(),
                )
            };
            fs::write(
                trader.join(name),
                envelope(version, &payload, aad).to_string(),
            )
            .unwrap();
            let env = Environment::new(home.path().into(), |_| None);
            let logger = LogWriter::start(env).unwrap();
            logger.event(
                Surface::Cli,
                "fixture",
                Level::Info,
                [("field", Field::Arg(token.clone()))],
            );
            assert!(logger.finish(Duration::from_secs(5)));

            let log_path = home
                .path()
                .join("logs")
                .join(format!("cli-{}.jsonl", chrono::Utc::now().date_naive()));
            let text = fs::read_to_string(log_path).unwrap();
            assert!(!text.contains(&token));
            assert!(text.contains("[redacted session token]"));
        }
    }
}
