use base64::{Engine as _, engine::general_purpose::STANDARD};
use hyper::{HeaderMap, header::HeaderValue};
use serde_json::{Value, json};
use url::{Url, form_urlencoded::byte_serialize};
use uuid::Uuid;

use super::invalid;
use crate::{
    endpoint::Endpoint,
    errors::{Code, SaiError},
    keeper::{self, TIMEOUT},
};

const HEIGHT: &str = "x-cosmos-block-height";
const SERVED_HEIGHT: &str = "grpc-metadata-x-cosmos-block-height";

pub(super) struct Lcd<'a>(pub &'a str);

pub(super) enum Read<'a> {
    Snapshot,
    Fresh,
    At(&'a str),
}

impl Lcd<'_> {
    pub(super) async fn network(&self) -> Result<Value, SaiError> {
        self.get("/cosmos/base/tendermint/v1beta1/node_info", Read::Snapshot)
            .await
            .map(|(response, _)| response["default_node_info"]["network"].clone())
    }

    pub(super) async fn smart(
        &self,
        contract: &str,
        message: Value,
        read: Read<'_>,
    ) -> Result<(Value, Option<String>), SaiError> {
        let encoded = STANDARD.encode(message.to_string());
        let query: String = byte_serialize(encoded.as_bytes()).collect();
        let path = format!("/cosmwasm/wasm/v1/contract/{contract}/smart/{query}");
        self.get(&path, read)
            .await
            .map(|(mut response, height)| (response["data"].take(), height))
    }

    pub(super) async fn position(
        &self,
        contract: &str,
        trader: &str,
        trade_id: &Value,
        read: Read<'_>,
    ) -> Result<(Value, Option<String>), SaiError> {
        let query = json!({"get_trade_raw": {"trader": trader, "index": trade_id}});
        self.smart(contract, query, read).await
    }

    async fn get(&self, path: &str, read: Read<'_>) -> Result<(Value, Option<String>), SaiError> {
        let url = format!("{}{path}", self.0.trim_end_matches('/'));
        let endpoint = Endpoint::split(&url).filter(|endpoint| {
            endpoint.scheme == "https"
                || (endpoint.scheme == "http"
                    && matches!(
                        endpoint.host_port().0.as_str(),
                        "localhost" | "127.0.0.1" | "[::1]"
                    ))
        });
        let Some(endpoint) = endpoint else {
            return Err(invalid("LCD endpoint must use HTTPS, except on loopback"));
        };
        if endpoint.authority.contains('@') {
            return Err(invalid("Credentials are not allowed in the LCD URL"));
        }

        let mut url = Url::parse(&url).map_err(|_| read.failure())?;
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

pub(super) fn height_error(message: &str) -> SaiError {
    SaiError::new(
        Code::PreviewMismatch,
        format!("{message} Retry the preview and confirmation."),
    )
}
