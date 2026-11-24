use std::sync::LazyLock;

use base64::{Engine as _, engine::general_purpose::STANDARD};
use chrono::DateTime;
use hyper::{HeaderMap, header::HeaderValue};
use regex::Regex;
use serde_json::{Value, json};
use url::form_urlencoded::byte_serialize;
use uuid::Uuid;

use crate::keeper::{TIMEOUT, lcd_get};
use crate::trade::cancel::lcd_url;

use super::decimal;

const HEIGHT: &str = "x-cosmos-block-height";
const RESPONSE_HEIGHT: &str = "grpc-metadata-x-cosmos-block-height";

pub(super) struct QuoteLcd<'a> {
    endpoint: &'a str,
    contract: String,
}

pub(super) enum ReadError {
    Failed,
    UnverifiedHeight,
    Http(String),
}

impl<'a> QuoteLcd<'a> {
    pub(super) fn new(endpoint: &'a str, contract: String) -> Self {
        Self {
            endpoint: endpoint.trim_end_matches('/'),
            contract: byte_serialize(contract.as_bytes()).collect(),
        }
    }

    pub(super) async fn smart(
        &self,
        query: Value,
        height: Option<&str>,
    ) -> Result<Value, ReadError> {
        self.smart_read(query, height, false)
            .await
            .map(|(value, _)| value)
    }

    pub(super) async fn anchor(&self, query: Value) -> Option<(Value, String)> {
        let (raw, height) = self.smart_read(query, None, true).await.ok()?;
        let height = height?;
        (height.bytes().all(|b| b.is_ascii_digit()) && height.bytes().any(|b| b != b'0'))
            .then_some((raw, height))
    }

    async fn smart_read(
        &self,
        query: Value,
        height: Option<&str>,
        fresh: bool,
    ) -> Result<(Value, Option<String>), ReadError> {
        let encoded = STANDARD.encode(query.to_string());
        let encoded: String = byte_serialize(encoded.as_bytes()).collect();
        let path = format!(
            "/cosmwasm/wasm/v1/contract/{}/smart/{encoded}",
            self.contract
        );
        let (mut response, height) = self.read(&path, height, fresh).await?;
        Ok((response["data"].take(), height))
    }

    async fn read(
        &self,
        path: &str,
        height: Option<&str>,
        fresh: bool,
    ) -> Result<(Value, Option<String>), ReadError> {
        let failure = || {
            if height.is_some() || fresh {
                ReadError::UnverifiedHeight
            } else {
                ReadError::Failed
            }
        };
        let mut url = lcd_url(&format!("{}{path}", self.endpoint)).map_err(|_| failure())?;
        let mut headers = HeaderMap::new();
        if let Some(height) = height {
            headers.insert(
                HEIGHT,
                HeaderValue::from_str(height).map_err(|_| failure())?,
            );
        }
        if fresh || height.is_some() {
            url.query_pairs_mut()
                .append_pair("_sai_query", &Uuid::new_v4().simple().to_string());
        }
        let (status, headers, body) = lcd_get(url.as_str(), headers, TIMEOUT)
            .await
            .map_err(|_| failure())?;
        if !status.is_success() {
            return Err(if height.is_some() || fresh {
                ReadError::UnverifiedHeight
            } else {
                ReadError::Http(String::from_utf8_lossy(&body).into_owned())
            });
        }
        let value: Value = serde_json::from_slice(&body).map_err(|_| failure())?;
        if !value.is_object() {
            return Err(failure());
        }
        let actual = headers
            .get(RESPONSE_HEIGHT)
            .and_then(|value| value.to_str().ok());
        if height.is_some() && actual != height {
            return Err(ReadError::UnverifiedHeight);
        }
        Ok((value, actual.map(str::to_owned)))
    }

    pub(super) async fn market(&self, index: &Value, height: Option<&str>) -> Option<Value> {
        self.object(json!({"get_market": {"index": index}}), height)
            .await
    }

    pub(super) async fn group(&self, pair: &Value, height: Option<&str>) -> Option<Value> {
        self.object(
            json!({"get_group": {"index": pair.get("group_index")?}}),
            height,
        )
        .await
    }

    pub(super) async fn fees(&self, pair: &Value, height: Option<&str>) -> Option<Value> {
        self.object(
            json!({"get_fees": {"index": pair.get("fee_index")?}}),
            height,
        )
        .await
    }

    async fn object(&self, query: Value, height: Option<&str>) -> Option<Value> {
        let result = self.smart(query, height).await.ok()?;
        result.is_object().then_some(result)
    }

    pub(super) async fn pair_leverage_cap(&self, index: &Value) -> Option<u128> {
        static ABSENT: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(r"type: cosmwasm_std::math::decimal::Decimal; key: \[[^\]]*\] not found")
                .expect("valid missing-cap pattern")
        });
        match self
            .smart(
                json!({"get_pair_custom_max_leverage": {"index": index}}),
                None,
            )
            .await
        {
            Ok(value) => decimal(&value),
            Err(ReadError::Http(body)) if ABSENT.is_match(&body) => Some(0),
            Err(_) => None,
        }
    }

    pub(super) async fn block_timestamp(&self, height: &str) -> Option<i64> {
        let path = format!("/cosmos/base/tendermint/v1beta1/blocks/{height}");
        let (result, _) = self.read(&path, None, false).await.ok()?;
        let block = if crate::keeper::truthy(&result["sdk_block"]) {
            &result["sdk_block"]
        } else {
            &result["block"]
        };
        let header = block.get("header")?;
        if header["height"] != height {
            return None;
        }
        Some(
            DateTime::parse_from_rfc3339(header["time"].as_str()?)
                .ok()?
                .timestamp(),
        )
    }
}
