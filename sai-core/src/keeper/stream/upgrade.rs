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

pub(in crate::keeper) enum Upgrade {
    Connected(SocketBox),
    Redirect {
        status: StatusCode,
        location: String,
    },
    Rejected(StatusCode),
}

pub(in crate::keeper) async fn connect(url: &Url, socket: SocketBox) -> Result<Upgrade, Failure> {
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
                .map(|value| Upgrade::Redirect {
                    status: reply.status(),
                    location: value.into(),
                })
                .map_err(|_| Failure::Disconnected("InvalidURI"));
        }
        if reply.status() != StatusCode::SWITCHING_PROTOCOLS {
            return Ok(Upgrade::Rejected(reply.status()));
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
    let connection = header_tokens(headers, CONNECTION)?;
    if !connection
        .iter()
        .any(|token| token.eq_ignore_ascii_case("upgrade"))
    {
        return Err(Failure::Disconnected("InvalidUpgrade"));
    }
    let upgrade = header_tokens(headers, UPGRADE)?;
    if !matches!(upgrade.as_slice(), [token] if token.eq_ignore_ascii_case("websocket")) {
        return Err(Failure::Disconnected("InvalidUpgrade"));
    }

    let mut accept_headers = headers.get_all(SEC_WEBSOCKET_ACCEPT).iter();
    let received = accept_headers
        .next()
        .filter(|_| accept_headers.next().is_none())
        .ok_or(Failure::Disconnected("InvalidHeader"))?;
    if received.as_bytes() != accept.as_bytes() {
        return Err(Failure::Disconnected("InvalidHeaderValue"));
    }

    if headers.contains_key(SEC_WEBSOCKET_EXTENSIONS) {
        return Err(Failure::Disconnected("NegotiationError"));
    }
    match header_tokens(headers, SEC_WEBSOCKET_PROTOCOL)?.as_slice() {
        [] => Ok(()),
        [protocol] if *protocol == SUBPROTOCOL => Ok(()),
        [_] => Err(Failure::Disconnected("NegotiationError")),
        _ => Err(Failure::Disconnected("InvalidHeader")),
    }
}

fn header_tokens(headers: &HeaderMap, name: HeaderName) -> Result<Vec<&str>, Failure> {
    let protocols = name == UPGRADE;
    let mut tokens = Vec::new();
    for value in headers.get_all(name).iter() {
        let value = std::str::from_utf8(value.as_bytes())
            .map_err(|_| Failure::Disconnected("InvalidHeaderFormat"))?;
        let count = tokens.len();
        for token in value
            .split(',')
            .map(|token| token.trim_matches([' ', '\t']))
        {
            if token.is_empty() {
                continue;
            }
            let valid = if protocols && let Some((name, version)) = token.split_once('/') {
                is_http_token(name) && is_http_token(version)
            } else {
                is_http_token(token)
            };
            if !valid {
                return Err(Failure::Disconnected("InvalidHeaderFormat"));
            }
            tokens.push(token);
        }
        if tokens.len() == count {
            return Err(Failure::Disconnected("InvalidHeaderFormat"));
        }
    }
    Ok(tokens)
}

fn is_http_token(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&byte))
}
