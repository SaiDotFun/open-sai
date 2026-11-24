//! Keeper snapshots over graphql-transport-ws.

pub(super) mod upgrade;

use std::{env, io, pin::Pin, time::Duration};

use futures_util::{SinkExt, StreamExt};
use serde_json::{Number, Value, json};
use tokio::time::{self, Instant};
use tokio_tungstenite::{
    WebSocketStream,
    tungstenite::{
        Error as WsError, Message,
        protocol::{CloseFrame, Role, WebSocketConfig, frame::coding::CloseCode},
    },
};
use url::Url;

use super::{transport, truthy};
use crate::{
    endpoint::Endpoint,
    errors::{Code, SaiError},
};
use upgrade::Upgrade;

const DEFAULT_REDIRECT_ATTEMPTS: isize = 10;
const OPEN_TIMEOUT: Duration = Duration::from_secs(10);
const CLOSE_TIMEOUT: Duration = Duration::from_secs(2);
const SNAPSHOT_TIMEOUT: Duration = Duration::from_secs(20);
const PING_INTERVAL: Duration = Duration::from_secs(20);
const SUBPROTOCOL: &str = "graphql-transport-ws";
pub const CONNECTED: &str = "Connected; waiting for current snapshot";

const BALANCES: &str = "subscription($user:String!) { userBalances(where:{user:$user}) { amount usd_value token_info { name symbol decimals type } } }";
const BALANCES_WITH_BLOCK: &str = "subscription($user:String!) { userBalances(where:{user:$user}) { amount usd_value lastUpdatedBlock token_info { name symbol decimals type } } }";
const POSITIONS: &str = "subscription($user:String!) { perpTrades(where:{trader:$user}) { id trader isOpen isLong tradeType leverage openPrice tp sl } }";
const PRICE: &str = "subscription($id:Int!) { tokenPricesUsd(where:{tokenId:$id}) { priceUsd token { id symbol } lastUpdatedBlock { block block_ts } } }";

pub enum Subscription {
    Balances(String),
    BalancesWithBlock(String),
    Positions(String),
    Price(Number),
}

impl Subscription {
    fn parameters(&self) -> (&'static str, &'static str, Value) {
        match self {
            Self::Balances(addr) => (BALANCES, "userBalances", json!({"user": addr})),
            Self::BalancesWithBlock(addr) => {
                (BALANCES_WITH_BLOCK, "userBalances", json!({"user": addr}))
            }
            Self::Positions(addr) => (POSITIONS, "perpTrades", json!({"user": addr})),
            Self::Price(id) => (PRICE, "tokenPricesUsd", json!({"id": id})),
        }
    }
}

/// Transport failures can reconnect. A rejected GraphQL message ends the command.
pub enum Failure {
    Disconnected(&'static str),
    Protocol(SaiError),
}

impl From<WsError> for Failure {
    fn from(error: WsError) -> Self {
        let name = match error {
            WsError::Io(error) => match error.kind() {
                io::ErrorKind::TimedOut => "TimeoutError",
                io::ErrorKind::ConnectionRefused => "ConnectionRefusedError",
                _ => "OSError",
            },
            WsError::Http(_) => "InvalidStatus",
            WsError::Url(_) => "InvalidURI",
            _ => "ConnectionClosedError",
        };
        Self::Disconnected(name)
    }
}

fn protocol(message: &str) -> Failure {
    Failure::Protocol(SaiError::new(Code::StreamError, message))
}

fn balance_error(error: Failure) -> SaiError {
    match error {
        Failure::Protocol(error) => error,
        Failure::Disconnected(_) => {
            SaiError::new(Code::StreamError, "Could not read balance snapshot")
        }
    }
}

pub fn check_endpoint(endpoint: &str) -> Result<(), SaiError> {
    let url = Endpoint::split(endpoint)
        .filter(|url| {
            url.scheme == "wss"
                || (url.scheme == "ws"
                    && matches!(
                        url.host_port().0.as_str(),
                        "localhost" | "127.0.0.1" | "[::1]"
                    ))
        })
        .ok_or_else(|| {
            SaiError::new(
                Code::StreamError,
                "WebSocket endpoint must use WSS, except on loopback",
            )
        })?;
    if let Some((userinfo, _)) = url.authority.rsplit_once('@')
        && !matches!(userinfo, "" | ":")
    {
        return Err(SaiError::new(
            Code::StreamError,
            "Credentials are not allowed in the WebSocket URL",
        ));
    }
    Ok(())
}

pub async fn snapshot(
    endpoint: &str,
    subscribe: &Subscription,
    notify: impl Fn(),
    interrupt: impl Future<Output = SaiError>,
) -> Result<Value, SaiError> {
    check_endpoint(endpoint)?;
    tokio::pin!(interrupt);
    let mut connection = Connection::open(endpoint, subscribe, interrupt.as_mut())
        .await
        .map_err(balance_error)?;
    notify();
    let result = tokio::select! {
        biased;
        error = &mut interrupt => {
            connection.close().await;
            return Err(error);
        }
        result = connection.next(Some(SNAPSHOT_TIMEOUT)) => result.map_err(balance_error),
    };
    tokio::select! {
        biased;
        () = connection.close() => result,
        error = &mut interrupt => Err(error),
    }
}

pub struct Connection {
    socket: WebSocketStream<transport::SocketBox>,
    field: &'static str,
    heartbeat: Instant,
    ping: Option<[u8; 8]>,
    ping_count: u64,
}

impl Connection {
    /// Open the connection, wait for the acknowledgement, then subscribe once.
    /// The upgrade and acknowledgement share one deadline.
    pub async fn open(
        endpoint: &str,
        subscribe: &Subscription,
        mut interrupt: Pin<&mut impl Future<Output = SaiError>>,
    ) -> Result<Self, Failure> {
        let deadline = Instant::now() + OPEN_TIMEOUT;
        let (query, field, variables) = subscribe.parameters();
        let mut connection = tokio::select! {
            biased;
            error = &mut interrupt => return Err(Failure::Protocol(error)),
            upgraded = time::timeout_at(deadline, Self::upgrade(endpoint, field)) => {
                upgraded.map_err(|_| Failure::Disconnected("TimeoutError"))??
            }
        };
        let handshake = tokio::select! {
            biased;
            error = &mut interrupt => Err(Failure::Protocol(error)),
            result = time::timeout_at(deadline, connection.acknowledge()) => {
                result.unwrap_or(Err(Failure::Disconnected("TimeoutError")))
            }
        };
        if let Err(error) = handshake {
            connection
                .close_by(deadline.min(Instant::now() + CLOSE_TIMEOUT))
                .await;
            return Err(error);
        }
        let request = json!({
            "id": "1",
            "type": "subscribe",
            "payload": {"query": query, "variables": variables},
        });
        let subscribed = tokio::select! {
            biased;
            error = &mut interrupt => Err(Failure::Protocol(error)),
            result = connection.send(request) => result,
        };
        if let Err(error) = subscribed {
            connection.close().await;
            return Err(error);
        }
        Ok(connection)
    }

    async fn acknowledge(&mut self) -> Result<(), Failure> {
        self.send(json!({"type": "connection_init"})).await?;
        let message = self.receive(None).await?;
        if message["type"] != "connection_ack" {
            return Err(protocol("Keeper did not acknowledge WebSocket connection"));
        }
        Ok(())
    }

    async fn upgrade(endpoint: &str, field: &'static str) -> Result<Self, Failure> {
        let mut url = Url::parse(endpoint).map_err(|_| Failure::Disconnected("InvalidURI"))?;
        if url.fragment().is_some() {
            return Err(Failure::Disconnected("InvalidURI"));
        }
        let attempts = env::var("WEBSOCKETS_MAX_REDIRECTS")
            .ok()
            .and_then(|value| value.trim().parse::<isize>().ok())
            .unwrap_or(DEFAULT_REDIRECT_ATTEMPTS);
        for _ in 0..attempts {
            let stream = transport::websocket_socket(&url, OPEN_TIMEOUT)
                .await
                .map_err(|error| {
                    let reason = match error {
                        transport::ConnectError::CertificateVerification => {
                            "SSLCertVerificationError"
                        }
                        transport::ConnectError::NotSent => "ConnectionRefusedError",
                        transport::ConnectError::InvalidRequest => "OSError",
                    };
                    Failure::Disconnected(reason)
                })?;
            match upgrade::connect(&url, stream).await? {
                Upgrade::Connected(socket) => {
                    let config = WebSocketConfig::default()
                        .max_message_size(Some(1 << 20))
                        .max_frame_size(Some(1 << 20));
                    return Ok(Self {
                        socket: WebSocketStream::from_raw_socket(
                            socket,
                            Role::Client,
                            Some(config),
                        )
                        .await,
                        field,
                        heartbeat: Instant::now() + PING_INTERVAL,
                        ping: None,
                        ping_count: 0,
                    });
                }
                Upgrade::Redirect { location, .. } => url = redirect_target(&url, &location)?,
                Upgrade::Rejected(_) => return Err(Failure::Disconnected("InvalidStatus")),
            }
        }
        Err(Failure::Disconnected("SecurityError"))
    }

    /// Return each complete subscription payload in arrival order.
    pub async fn next(&mut self, timeout: Option<Duration>) -> Result<Value, Failure> {
        loop {
            let mut message = self.receive(timeout).await?;
            match message["type"].as_str() {
                Some("ping") => self.send(json!({"type": "pong"})).await?,
                Some("next") => {
                    let payload = message
                        .as_object_mut()
                        .and_then(|message| message.remove("payload"))
                        .unwrap_or(Value::Null);
                    if truthy(&payload["errors"]) || !payload["data"][self.field].is_array() {
                        return Err(protocol("Keeper subscription returned incomplete data"));
                    }
                    return Ok(payload);
                }
                Some("error" | "complete") => {
                    return Err(protocol("Keeper subscription ended"));
                }
                _ => {}
            }
        }
    }

    async fn send(&mut self, message: Value) -> Result<(), Failure> {
        self.socket.send(Message::text(message.to_string())).await?;
        Ok(())
    }

    async fn receive(&mut self, timeout: Option<Duration>) -> Result<Value, Failure> {
        let deadline = timeout.map(|timeout| Instant::now() + timeout);
        loop {
            let wake = deadline.map_or(self.heartbeat, |deadline| deadline.min(self.heartbeat));
            match time::timeout_at(wake, self.socket.next()).await {
                Err(_) if deadline.is_some_and(|deadline| Instant::now() >= deadline) => {
                    return Err(Failure::Disconnected("TimeoutError"));
                }
                Err(_) if self.ping.is_some() => {
                    return Err(Failure::Disconnected("ConnectionClosedError"));
                }
                Err(_) => {
                    self.ping_count = self.ping_count.wrapping_add(1);
                    let ping = self.ping_count.to_be_bytes();
                    self.socket
                        .send(Message::Ping(ping.to_vec().into()))
                        .await?;
                    self.ping = Some(ping);
                    self.heartbeat = Instant::now() + PING_INTERVAL;
                }
                Ok(Some(Ok(Message::Pong(value)))) => {
                    if self.ping.is_some_and(|ping| value.as_ref() == ping) {
                        self.ping = None;
                    }
                }
                Ok(Some(Ok(Message::Ping(_)))) => self.socket.flush().await?,
                Ok(Some(Ok(Message::Text(text)))) => {
                    return decode(text.as_bytes());
                }
                Ok(Some(Ok(Message::Binary(bytes)))) => return decode(&bytes),
                Ok(Some(Ok(Message::Close(frame)))) => {
                    let normal = frame.is_none_or(|frame| {
                        matches!(frame.code, CloseCode::Normal | CloseCode::Away)
                    });
                    let _ = self.socket.flush().await;
                    let name = if normal {
                        "ConnectionClosedOK"
                    } else {
                        "ConnectionClosedError"
                    };
                    return Err(Failure::Disconnected(name));
                }
                Ok(Some(Err(error))) => return Err(error.into()),
                Ok(None) => {
                    return Err(Failure::Disconnected("ConnectionClosedError"));
                }
                Ok(Some(Ok(Message::Frame(_)))) => {}
            }
        }
    }

    pub async fn close(&mut self) {
        self.close_by(Instant::now() + CLOSE_TIMEOUT).await;
    }

    async fn close_by(&mut self, deadline: Instant) {
        let _ = time::timeout_at(deadline, async {
            // Python exits the socket context through a subscription exception
            // or GeneratorExit after consuming the first snapshot.
            let frame = CloseFrame {
                code: CloseCode::Error,
                reason: "".into(),
            };
            if self.socket.close(Some(frame)).await.is_ok() {
                while let Some(Ok(message)) = self.socket.next().await {
                    if message.is_close() {
                        break;
                    }
                }
            }
        })
        .await;
    }
}

fn redirect_target(previous: &Url, location: &str) -> Result<Url, Failure> {
    let next = previous
        .join(location)
        .map_err(|_| Failure::Disconnected("InvalidURI"))?;
    if !matches!(next.scheme(), "ws" | "wss") || next.fragment().is_some() {
        return Err(Failure::Disconnected("InvalidURI"));
    }
    if (previous.scheme() == "wss" && next.scheme() != "wss")
        || check_endpoint(next.as_str()).is_err()
    {
        return Err(Failure::Disconnected("SecurityError"));
    }
    Ok(next)
}

fn decode(bytes: &[u8]) -> Result<Value, Failure> {
    let message: Value = serde_json::from_slice(bytes).map_err(|_| invalid_message())?;
    if !message.is_object() {
        return Err(invalid_message());
    }
    Ok(message)
}

fn invalid_message() -> Failure {
    protocol("Keeper sent an invalid WebSocket message")
}
