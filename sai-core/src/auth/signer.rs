use std::{
    fs, io,
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::Path,
    process::Stdio,
};

use regex::Regex;
use serde_json::Value;
use tempfile::Builder;
use thiserror::Error;
use tokio::process::Command;
use zeroize::Zeroizing;

use crate::{
    errors::{Code, SaiError},
    keeper::{Keeper, is_evm_address},
    storage::Session,
};

use super::{exchange, fetch_challenge, installed, keeper_error, run, run_success};

#[derive(Debug, Error)]
pub enum LoginError {
    #[error(transparent)]
    Failure(#[from] SaiError),
    #[error("Wallet signing failed")]
    SigningFailed,
    #[error(
        "ud could not sign with {0}. The Foundry keystore ~/.foundry/keystores/{0} may have another password, which cast can ask for."
    )]
    NeedsTerminal(String),
}

struct UdWallet {
    addr: String,
    cast_state: Option<String>,
}

impl UdWallet {
    fn state_hint(&self, name: &str) -> String {
        let state = self
            .cast_state
            .as_deref()
            .filter(|state| !state.is_empty())
            .unwrap_or("unknown");
        let prefix = format!("ud wallet {name} has Cast keystore state {state}");
        match state {
            "missing" => {
                format!("{prefix}; run `ud evm wallet sync {name}` to create it")
            }
            "name-conflict" => format!(
                "{prefix}: another keystore already uses that name; rename it, then run `ud evm wallet sync {name}`"
            ),
            _ => format!("{prefix}; check `ud evm wallet show {name}`"),
        }
    }
}

#[derive(Clone, Copy)]
enum Signer {
    Cast,
    Ud,
}

/// Sign in using the named Foundry or ud wallet on this machine.
/// The caller handles terminal recovery guidance and persistence.
pub async fn login(
    keeper: &Keeper<'_>,
    home: &Path,
    name: &str,
    expected: Option<&str>,
    terminal: bool,
    warn: impl Fn(&str),
) -> Result<Session, LoginError> {
    if let Some(addr) = expected.filter(|addr| !is_evm_address(addr)) {
        return Err(invalid(format!("Not an EVM addr: {addr}")));
    }
    let path = home.join(".foundry/keystores").join(name);
    let has_keystore = path.is_file();
    let wallet = match ud_wallet(name).await {
        Ok(wallet) => wallet,
        Err(error) if has_keystore => {
            let found = keystore_address(&path, name).await?;
            let Some(addr) = expected.filter(|addr| addr.eq_ignore_ascii_case(&found)) else {
                return Err(invalid(format!(
                    "{error}, so sai cannot tell whether ud holds another account named {name}. Check that ud works, then retry. To sign in as the Foundry keystore's account {found}, pass `--addr {found}`."
                )));
            };
            return sign(keeper, addr, name, Signer::Cast).await;
        }
        Err(error) => {
            return Err(invalid(format!("{error}; check that ud works, then retry")));
        }
    };
    if let Some(wallet) = wallet
        .as_ref()
        .filter(|wallet| wallet.cast_state.as_deref() == Some("matching"))
    {
        let addr = checked_address(&wallet.addr, expected)?;
        match sign(keeper, addr, name, Signer::Ud).await {
            Err(LoginError::SigningFailed) if has_keystore => {
                if !terminal {
                    return Err(LoginError::NeedsTerminal(name.into()));
                }
                // The name is resolved again by cast. Check it again after ud fails.
                let current = keystore_address(&path, name).await?;
                if !current.eq_ignore_ascii_case(addr) {
                    return Err(invalid(format!(
                        "The Foundry keystore ~/.foundry/keystores/{name} now holds {current}, not {addr}; nothing was signed with it."
                    )));
                }
                warn(&format!(
                    "ud could not sign with {name}; signing with the Foundry keystore ~/.foundry/keystores/{name} instead."
                ));
                return sign(keeper, addr, name, Signer::Cast).await;
            }
            result => return result,
        }
    }
    if has_keystore {
        let found = keystore_address(&path, name).await?;
        let expected = match wallet
            .as_ref()
            .filter(|wallet| !wallet.addr.eq_ignore_ascii_case(&found))
        {
            Some(wallet) => Some(chosen_keystore(name, wallet, &found, expected)?),
            None => expected,
        };
        return sign(
            keeper,
            checked_address(&found, expected)?,
            name,
            Signer::Cast,
        )
        .await;
    }
    if let Some(wallet) = wallet {
        return Err(invalid(wallet.state_hint(name)));
    }
    Err(invalid(format!(
        "No key named {name}: there is no Foundry keystore ~/.foundry/keystores/{name}. Run `cast wallet list` to see keystores, or connect with `sai auth import`."
    )))
}

fn invalid(message: String) -> LoginError {
    SaiError::new(Code::InvalidRequest, message).into()
}

fn checked_address<'a>(found: &'a str, expected: Option<&str>) -> Result<&'a str, LoginError> {
    if let Some(expected) = expected.filter(|expected| !expected.eq_ignore_ascii_case(found)) {
        return Err(invalid(format!(
            "--addr {expected} does not match the wallet addr {found}"
        )));
    }
    Ok(found)
}

fn chosen_keystore<'a>(
    name: &str,
    ud: &UdWallet,
    keystore: &str,
    expected: Option<&'a str>,
) -> Result<&'a str, LoginError> {
    let both = format!(
        "{name} names two accounts: ud's wallet holds {} and the Foundry keystore ~/.foundry/keystores/{name} holds {keystore}.",
        ud.addr
    );
    match expected {
        None => Err(invalid(format!(
            "{both} To sign in as the keystore's account, run `sai auth login {name} --addr {keystore}`. To sign in as ud's account, rename the keystore, then run `ud evm wallet sync {name}`."
        ))),
        Some(addr) if addr.eq_ignore_ascii_case(&ud.addr) => {
            Err(invalid(format!("{both} {}", ud.state_hint(name))))
        }
        Some(addr) if !addr.eq_ignore_ascii_case(keystore) => {
            Err(invalid(format!("{both} --addr {addr} matches neither.")))
        }
        Some(addr) => Ok(addr),
    }
}

async fn ud_wallet(name: &str) -> Result<Option<UdWallet>, String> {
    if !installed("ud") {
        return Ok(None);
    }
    let tool = format!("`ud evm wallet show {name}`");
    let mut command = Command::new("ud");
    command
        .args(["evm", "wallet", "show", name])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let output = run(&mut command, 60)
        .await
        .map_err(|_| format!("{tool} did not run"))?;
    if !output.status.success() {
        let unknown = Regex::new(&format!(
            r"\A(?:Error: )?wallet {} is not in pass entry \S+\z",
            regex::escape(name)
        ))
        .expect("wallet name is escaped");
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr
            .lines()
            .map(str::trim)
            .rfind(|line| !line.is_empty())
            .is_some_and(|line| unknown.is_match(line))
        {
            return Ok(None);
        }
        return Err(format!("{tool} failed"));
    }
    let stdout = std::str::from_utf8(&output.stdout).unwrap_or_default();
    parse_wallet(stdout)
        .map(Some)
        .ok_or_else(|| format!("{tool} printed no addr"))
}

fn parse_wallet(output: &str) -> Option<UdWallet> {
    output.match_indices('{').find_map(|(start, _)| {
        let value = serde_json::Deserializer::from_str(&output[start..])
            .into_iter::<Value>()
            .next()?
            .ok()?;
        let addr = value
            .get("address")?
            .as_str()
            .filter(|addr| is_evm_address(addr))?;
        Some(UdWallet {
            addr: addr.into(),
            cast_state: value["cast"]["state"].as_str().map(str::to_owned),
        })
    })
}

async fn keystore_address(path: &Path, name: &str) -> Result<String, SaiError> {
    if let Some(addr) = recorded_address(path) {
        return Ok(addr);
    }
    let mut command = Command::new("cast");
    command
        .args(["wallet", "address", "--account", name])
        .stdin(Stdio::inherit())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    let output = run_success(&mut command, 300)
        .await
        .map_err(|_| keeper_error(&format!("cast could not read keystore account {name}")))?;
    let addr = std::str::from_utf8(&output.stdout)
        .unwrap_or_default()
        .trim();
    if !addr.starts_with("0x") || addr.chars().count() != 42 {
        return Err(keeper_error("cast returned an invalid addr"));
    }
    Ok(addr.into())
}

fn recorded_address(path: &Path) -> Option<String> {
    let keystore: Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    let raw = keystore.get("address")?.as_str()?;
    let addr = if raw.to_ascii_lowercase().starts_with("0x") {
        raw.to_owned()
    } else {
        format!("0x{raw}")
    };
    is_evm_address(&addr).then_some(addr)
}

async fn sign(
    keeper: &Keeper<'_>,
    addr: &str,
    name: &str,
    signer: Signer,
) -> Result<Session, LoginError> {
    let (typed, nonce) = fetch_challenge(keeper, addr).await?;
    let signature = sign_file(&typed, name, signer).await?;
    if !signature.starts_with("0x") || signature.chars().count() != 132 {
        return Err(keeper_error("Wallet signer returned an invalid signature").into());
    }
    Ok(exchange(keeper, addr, &nonce, &signature, name).await?)
}

async fn sign_file(
    typed: &Value,
    name: &str,
    signer: Signer,
) -> Result<Zeroizing<String>, LoginError> {
    let directory = Builder::new()
        .prefix("sai-auth-")
        .permissions(fs::Permissions::from_mode(0o700))
        .tempdir()
        .map_err(|_| SaiError::internal())?;
    let path = directory.path().join("challenge.json");
    write_challenge(&path, typed).map_err(|_| SaiError::internal())?;
    let mut command;
    let seconds = match signer {
        Signer::Cast => {
            command = Command::new("cast");
            command
                .args(["wallet", "sign", "--account", name, "--data", "--from-file"])
                .stderr(Stdio::inherit());
            300
        }
        Signer::Ud => {
            command = Command::new("ud");
            command
                .args([
                    "evm",
                    "wallet",
                    "run",
                    name,
                    "--",
                    "wallet",
                    "sign",
                    "--data",
                    "--from-file",
                ])
                .stderr(Stdio::piped());
            120
        }
    };
    command
        .arg(path)
        .stdin(Stdio::inherit())
        .stdout(Stdio::piped());
    let output = run_success(&mut command, seconds)
        .await
        .map_err(|_| LoginError::SigningFailed)?;
    let bytes = Zeroizing::new(output.stdout);
    let text = std::str::from_utf8(&bytes).map_err(|_| SaiError::internal())?;
    Ok(Zeroizing::new(text.trim().to_owned()))
}

fn write_challenge(path: &Path, typed: &Value) -> io::Result<()> {
    let file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    serde_json::to_writer(file, typed).map_err(io::Error::other)
}
