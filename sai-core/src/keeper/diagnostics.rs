//! Lightweight endpoint probes used by doctor.

use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio::time;
use tokio_tungstenite::{
    WebSocketStream,
    tungstenite::{
        Error, Message,
        protocol::{Role, WebSocketConfig},
    },
};
use url::Url;

use super::{
    ProbeEndpoint, check_transport,
    stream::{
        Failure,
        upgrade::{self, Upgrade},
    },
    transport,
};

/// Read the LCD chain ID. None means node_info answered without a network.
pub async fn lcd_network(
    endpoint: &str,
    timeout: Duration,
) -> Result<Option<String>, &'static str> {
    const NODE_INFO: &str = "/cosmos/base/tendermint/v1beta1/node_info";
    const UNREADABLE: &str = "Unable to read contract state";
    check_transport(endpoint, ProbeEndpoint::Lcd)?;
    let url = format!("{}{NODE_INFO}", endpoint.trim_end_matches('/'));
    let (status, _, bytes) = transport::get(&url, hyper::HeaderMap::new(), timeout)
        .await
        .map_err(|_| UNREADABLE)?;
    if !status.is_success() {
        return Err(UNREADABLE);
    }
    let data: Value = serde_json::from_slice(&bytes).map_err(|_| UNREADABLE)?;
    if !data.is_object() {
        return Err(UNREADABLE);
    }
    Ok(data["default_node_info"]["network"]
        .as_str()
        .filter(|value| !value.is_empty())
        .map(str::to_owned))
}

/// Confirm graphql-transport-ws negotiation and connection_ack, with no subscription.
pub async fn websocket_handshake(endpoint: &str, timeout: Duration) -> Result<(), String> {
    check_transport(endpoint, ProbeEndpoint::WebSocket).map_err(|_| String::from("StreamError"))?;
    time::timeout(timeout, async {
        let url = Url::parse(endpoint).map_err(|_| "InvalidURI")?;
        let socket = transport::doctor_websocket_socket(&url, timeout)
            .await
            .map_err(|_| "OSError")?;
        let socket = match upgrade::connect(&url, socket).await {
            Ok(Upgrade::Connected(socket)) => socket,
            Ok(Upgrade::Rejected(status) | Upgrade::Redirect { status, .. }) => {
                return Err(format!("InvalidStatus (HTTP status {})", status.as_u16()));
            }
            Err(Failure::Disconnected(reason)) => return Err(reason.into()),
            Err(Failure::Protocol(_)) => return Err("StreamError".into()),
        };
        let config = WebSocketConfig::default()
            .max_message_size(Some(1024 * 1024))
            .max_frame_size(Some(1024 * 1024));
        let mut connection =
            WebSocketStream::from_raw_socket(socket, Role::Client, Some(config)).await;
        connection
            .send(Message::Text(
                json!({"type": "connection_init"}).to_string().into(),
            ))
            .await
            .map_err(ws_error)?;
        while let Some(message) = connection.next().await {
            let message = message.map_err(ws_error)?;
            let payload = match message {
                Message::Text(text) => serde_json::from_str::<Value>(&text),
                Message::Binary(bytes) => serde_json::from_slice(&bytes),
                Message::Close(close) => {
                    return Err(close.map_or_else(
                        || "ConnectionClosedError".into(),
                        |frame| {
                            let code = u16::from(frame.code);
                            let state = if matches!(code, 1000 | 1001) {
                                "ConnectionClosedOK"
                            } else {
                                "ConnectionClosedError"
                            };
                            format!("{state} (close code {code})")
                        },
                    ));
                }
                _ => continue,
            };
            if !payload.is_ok_and(|value| value["type"] == "connection_ack") {
                return Err("StreamError".into());
            }
            let _ = connection.close(None).await;
            return Ok(());
        }
        Err("ConnectionClosedError".into())
    })
    .await
    .unwrap_or_else(|_| Err("TimeoutError".into()))
}

fn ws_error(error: Error) -> String {
    match error {
        Error::Http(response) => {
            format!("InvalidStatus (HTTP status {})", response.status().as_u16())
        }
        Error::Io(_) => "OSError".into(),
        Error::Protocol(_) => "InvalidHandshake".into(),
        Error::ConnectionClosed | Error::AlreadyClosed => "ConnectionClosedError".into(),
        _ => "StreamError".into(),
    }
}
