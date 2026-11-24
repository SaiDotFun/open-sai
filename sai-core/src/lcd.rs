//! Height-pinned LCD reads for the account overview.

use std::{sync::LazyLock, time::Duration};

use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::Value;
use tokio::{
    sync::Semaphore,
    time::{self, Instant},
};
use url::form_urlencoded::byte_serialize;
use uuid::Uuid;

use crate::keeper::{check_url, transport};

const HEIGHT: &str = "x-cosmos-block-height";
const METADATA_HEIGHT: &str = "grpc-metadata-x-cosmos-block-height";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(2);
static SLOTS: LazyLock<Semaphore> = LazyLock::new(|| Semaphore::new(8));

#[derive(Clone, Copy, Debug)]
pub(crate) enum ReadError {
    Invalid,
    Timeout,
}

pub(crate) async fn block(lcd: &str, height: &str, deadline: Instant) -> Result<Value, ReadError> {
    let path = format!("/cosmos/base/tendermint/v1beta1/blocks/{height}");
    let body = read(lcd, &path, None, height == "latest", deadline).await?;
    body.get("block")
        .and_then(|block| block.get("header"))
        .filter(|header| header.is_object())
        .cloned()
        .ok_or(ReadError::Invalid)
}

pub(crate) async fn smart(
    lcd: &str,
    contract: &str,
    message: Value,
    height: u64,
    deadline: Instant,
) -> Result<Value, ReadError> {
    let encoded = STANDARD.encode(message.to_string());
    let encoded: String = byte_serialize(encoded.as_bytes()).collect();
    let path = format!("/cosmwasm/wasm/v1/contract/{contract}/smart/{encoded}");
    let mut body = read(lcd, &path, Some(&height.to_string()), true, deadline).await?;
    Ok(body["data"].take())
}

async fn read(
    lcd: &str,
    path: &str,
    height: Option<&str>,
    fresh: bool,
    deadline: Instant,
) -> Result<Value, ReadError> {
    let now = Instant::now();
    if now >= deadline {
        return Err(ReadError::Timeout);
    }
    let _slot = SLOTS.try_acquire().map_err(|_| ReadError::Invalid)?;
    let deadline = deadline.min(now + REQUEST_TIMEOUT);
    let mut url = format!("{}{path}", lcd.trim_end_matches('/'));
    check_url(&url).map_err(|_| ReadError::Invalid)?;
    if fresh {
        url.push_str(&format!("?_sai_query={}", Uuid::new_v4().simple()));
    }
    // The outer deadline covers DNS and the entire body, including trickling responses.
    let (parts, bytes) = time::timeout_at(
        deadline,
        transport::get(&url, height, crate::keeper::TIMEOUT),
    )
    .await
    .map_err(|_| ReadError::Timeout)?
    .map_err(|_| ReadError::Invalid)?;
    if !parts.status.is_success() {
        return Err(ReadError::Invalid);
    }
    if let Some(height) = height {
        let header = if parts.headers.contains_key(METADATA_HEIGHT) {
            METADATA_HEIGHT
        } else {
            HEIGHT
        };
        let values = parts.headers.get_all(header);
        let mut values = values.iter();
        let served = values.next().and_then(|value| value.to_str().ok());
        if served != Some(height) || values.next().is_some() {
            return Err(ReadError::Invalid);
        }
    }
    let result: Value = serde_json::from_slice(&bytes).map_err(|_| ReadError::Invalid)?;
    if !result.is_object() {
        return Err(ReadError::Invalid);
    }
    Ok(result)
}
