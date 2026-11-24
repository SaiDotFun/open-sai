use std::sync::LazyLock;

use base64::{Engine as _, engine::general_purpose::STANDARD};
use chrono::DateTime;
use hyper::{HeaderMap, header::HeaderValue};
use regex::Regex;
use serde_json::{Value, json};
use url::form_urlencoded::byte_serialize;
use uuid::Uuid;

use crate::{
    errors::{Code, SaiError},
    keeper::{self, TIMEOUT},
};

use super::{invalid, malformed};
use crate::trade::cancel::lcd_url;

const HEIGHT: &str = "x-cosmos-block-height";
const SERVED_HEIGHT: &str = "grpc-metadata-x-cosmos-block-height";

pub(in crate::trade) struct Lcd<'a>(pub &'a str);

#[derive(Clone, Copy)]
pub(in crate::trade) enum Read<'a> {
    Snapshot,
    Fresh,
    At(&'a str),
}

impl Lcd<'_> {
    pub(in crate::trade) async fn network(&self) -> Result<Value, SaiError> {
        self.get(
            "/cosmos/base/tendermint/v1beta1/node_info",
            Read::Snapshot,
            false,
        )
        .await
        .map(|(response, _)| response["default_node_info"]["network"].clone())
    }

    pub(in crate::trade) async fn smart(
        &self,
        contract: &str,
        message: Value,
        read: Read<'_>,
    ) -> Result<(Value, Option<String>), SaiError> {
        let encoded = STANDARD.encode(message.to_string());
        let query: String = byte_serialize(encoded.as_bytes()).collect();
        let contract: String = byte_serialize(contract.as_bytes()).collect();
        let path = format!("/cosmwasm/wasm/v1/contract/{contract}/smart/{query}");
        let missing_cap = message.get("get_pair_custom_max_leverage").is_some();
        self.get(&path, read, missing_cap)
            .await
            .map(|(mut response, height)| (response["data"].take(), height))
    }

    pub(in crate::trade) async fn position(
        &self,
        contract: &str,
        trader: &str,
        trade_id: &Value,
        read: Read<'_>,
    ) -> Result<(Value, Option<String>), SaiError> {
        let query = json!({"get_trade_raw": {"trader": trader, "index": trade_id}});
        self.smart(contract, query, read).await
    }

    pub(in crate::trade) async fn block_timestamp(&self, height: &str) -> Result<i64, SaiError> {
        let path = format!("/cosmos/base/tendermint/v1beta1/blocks/{height}");
        let (block, _) = self.get(&path, Read::Snapshot, false).await?;
        let header = if keeper::truthy(&block["sdk_block"]) {
            &block["sdk_block"]["header"]
        } else {
            &block["block"]["header"]
        };
        if header["height"] != height {
            return Err(invalid(
                "Cannot verify the timestamp of the quoted LCD block",
            ));
        }
        DateTime::parse_from_rfc3339(header["time"].as_str().ok_or_else(malformed)?)
            .map(|time| time.timestamp())
            .map_err(|_| malformed())
    }

    async fn get(
        &self,
        path: &str,
        read: Read<'_>,
        missing_cap_is_zero: bool,
    ) -> Result<(Value, Option<String>), SaiError> {
        static ABSENT_CAP: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(r"type: cosmwasm_std::math::decimal::Decimal; key: \[[^\]]*\] not found")
                .expect("valid missing-cap pattern")
        });
        let mut url = lcd_url(&format!("{}{path}", self.0.trim_end_matches('/')))?;
        if !matches!(read, Read::Snapshot) {
            url.query_pairs_mut()
                .append_pair("_sai_query", &Uuid::new_v4().simple().to_string());
        }
        let mut headers = HeaderMap::new();
        if let Read::At(height) = read {
            headers.insert(
                HEIGHT,
                HeaderValue::from_str(height).map_err(|_| read.failure())?,
            );
        }
        let (status, headers, bytes) = keeper::lcd_get(url.as_str(), headers, TIMEOUT)
            .await
            .map_err(|_| read.failure())?;
        if !status.is_success() {
            // A Perp storage miss means the group maximum applies. Other LCD
            // failures cannot establish whether the pair has a cap.
            if missing_cap_is_zero && ABSENT_CAP.is_match(&String::from_utf8_lossy(&bytes)) {
                return Ok((json!({"data": "0"}), None));
            }
            return Err(read.failure());
        }
        let data: Value = serde_json::from_slice(&bytes).map_err(|_| read.failure())?;
        if !data.is_object() {
            return Err(read.failure());
        }
        let height = headers
            .get(SERVED_HEIGHT)
            .map(|value| String::from_utf8_lossy(value.as_bytes()).into_owned());
        if let Read::At(requested) = read
            && height.as_deref() != Some(requested)
        {
            return Err(height_error(
                "LCD did not serve the requested contract query height.",
            ));
        }
        Ok((data, height))
    }
}

impl Read<'_> {
    fn failure(&self) -> SaiError {
        match self {
            Self::Snapshot => invalid("Unable to read contract state"),
            _ => height_error("Unable to verify contract state during confirmation."),
        }
    }
}

pub(in crate::trade) fn height_error(message: &str) -> SaiError {
    SaiError::new(
        Code::PreviewMismatch,
        format!("{message} Retry the preview and confirmation."),
    )
}
