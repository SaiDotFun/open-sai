//! HTTP upgrade for Keeper's WebSocket. A server may omit the subprotocol
//! selection even though the client offers graphql-transport-ws.

use std::pin::pin;

use http_body_util::Empty;
use hyper::{
    HeaderMap, StatusCode, Uri,
    body::Bytes,
    client::conn::http1,
    header::{
        CONNECTION, HeaderName, LOCATION, SEC_WEBSOCKET_ACCEPT, SEC_WEBSOCKET_EXTENSIONS,
        SEC_WEBSOCKET_KEY, SEC_WEBSOCKET_PROTOCOL, UPGRADE,
    },
};
use hyper_util::rt::TokioIo;
use tokio_tungstenite::tungstenite::{client::IntoClientRequest, handshake::derive_accept_key};
use url::Url;

use super::{Failure, SUBPROTOCOL, transport::SocketBox};

pub(super) enum Upgrade {
    Connected(SocketBox),
    Redirect(String),
}

pub(super) async fn connect(url: &Url, socket: SocketBox) -> Result<Upgrade, Failure> {
    let mut request = url.as_str().into_client_request()?;
    let path = request
        .uri()
        .path_and_query()
        .ok_or(Failure::Disconnected("InvalidURI"))?;
    *request.uri_mut() = Uri::from(path.clone());
    request.headers_mut().insert(
        SEC_WEBSOCKET_PROTOCOL,
        SUBPROTOCOL.parse().expect("valid subprotocol"),
    );
    let accept = derive_accept_key(request.headers()[SEC_WEBSOCKET_KEY].as_bytes());

    let (mut sender, connection) = http1::handshake(TokioIo::new(socket))
        .await
        .map_err(|_| Failure::Disconnected("InvalidMessage"))?;
    let response = async {
        let reply = sender
            .send_request(request.map(|()| Empty::<Bytes>::new()))
            .await
            .map_err(|_| Failure::Disconnected("InvalidMessage"))?;
        if reply.version() != hyper::Version::HTTP_11 {
            return Err(Failure::Disconnected("InvalidMessage"));
        }
        if matches!(reply.status().as_u16(), 300..=303 | 307 | 308)
            && let Some(location) = reply.headers().get(LOCATION)
        {
            return location
                .to_str()
                .map(|value| Upgrade::Redirect(value.into()))
                .map_err(|_| Failure::Disconnected("InvalidURI"));
        }
        if reply.status() != StatusCode::SWITCHING_PROTOCOLS {
            return Err(Failure::Disconnected("InvalidStatus"));
        }

        validate_headers(reply.headers(), &accept)?;
        // Hyper retains any frame bytes that arrived with the HTTP response.
        let upgraded = hyper::upgrade::on(reply)
            .await
            .map_err(|_| Failure::Disconnected("InvalidMessage"))?;
        Ok(Upgrade::Connected(Box::new(TokioIo::new(upgraded))))
    };
    let mut response = pin!(response);
    // The response uses the same socket as the driver. Poll them together so
    // cancellation or a rejected handshake drops the socket immediately.
    tokio::select! {
        biased;
        result = response.as_mut() => result,
        result = connection.with_upgrades() => {
            result.map_err(|_| Failure::Disconnected("InvalidMessage"))?;
            response.await
        }
    }
}

fn validate_headers(headers: &HeaderMap, accept: &str) -> Result<(), Failure> {
    let connected = headers
        .get_all(CONNECTION)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .any(|token| token.trim().eq_ignore_ascii_case("upgrade"));
    if !connected
        || !single(headers, UPGRADE)?.is_some_and(|value| value.eq_ignore_ascii_case("websocket"))
        || single(headers, SEC_WEBSOCKET_ACCEPT)? != Some(accept)
    {
        return Err(Failure::Disconnected("InvalidHeaderValue"));
    }

    if headers.contains_key(SEC_WEBSOCKET_EXTENSIONS)
        || single(headers, SEC_WEBSOCKET_PROTOCOL)?.is_some_and(|value| value != SUBPROTOCOL)
    {
        return Err(Failure::Disconnected("NegotiationError"));
    }
    Ok(())
}

fn single(headers: &HeaderMap, name: HeaderName) -> Result<Option<&str>, Failure> {
    let mut values = headers.get_all(name).iter();
    let value = values.next();
    if values.next().is_some() {
        return Err(Failure::Disconnected("InvalidHeaderValue"));
    }
    value
        .map(|header| {
            header
                .to_str()
                .map_err(|_| Failure::Disconnected("InvalidHeaderValue"))
        })
        .transpose()
}
