"""Keeper GraphQL subscriptions over graphql-transport-ws."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect
from websockets.typing import Subprotocol

from . import keeper
from .errors import Code, SaiError

BALANCES = "subscription($user:String!) { userBalances(where:{user:$user}) { amount usd_value token_info { name symbol decimals type } } }"
POSITIONS = "subscription($user:String!) { perpTrades(where:{trader:$user}) { id trader isOpen isLong tradeType leverage openPrice tp sl } }"
PRICE = "subscription($id:Int!) { tokenPricesUsd(where:{tokenId:$id}) { priceUsd token { id symbol } lastUpdatedBlock { block block_ts } } }"

# The list field each subscription must return.
FIELDS = ("userBalances", "perpTrades", "tokenPricesUsd")

CONNECTED = "Connected; waiting for current snapshot"

# Transport failures a caller may retry by reconnecting.
DISCONNECTS = (OSError, TimeoutError, WebSocketException)


class StreamError(SaiError, ValueError):
    """Keeper's subscription refused, ended, or sent incomplete data."""

    code = Code.STREAM_ERROR


def _ignore(_message: str) -> None:
    return None


def derive_ws(endpoint: str) -> str:
    """Return the WebSocket URL that serves the Keeper HTTP ENDPOINT."""
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise StreamError("Cannot derive WebSocket endpoint")
    return parsed._replace(scheme="wss" if parsed.scheme == "https" else "ws").geturl()


def check_ws(ws_endpoint: str) -> None:
    """Refuse a plain-text WebSocket URL except on loopback, or one with
    credentials."""
    try:
        keeper.check_url(ws_endpoint, "WebSocket", secure="wss")
    except ValueError as exc:
        raise StreamError(str(exc)) from exc


def check_payload(payload: Any, query: str) -> dict[str, Any]:
    """Return PAYLOAD if it holds the list field that QUERY subscribes to."""
    expected = next((name for name in FIELDS if name in query), None)
    if (
        not isinstance(payload, dict)
        or payload.get("errors")
        or not isinstance(payload.get("data"), dict)
        or expected is None
        or not isinstance(payload["data"].get(expected), list)
    ):
        raise StreamError("Keeper subscription returned incomplete data")
    return payload


def decode(raw: str | bytes) -> dict[str, Any]:
    """Parse one protocol message, refusing anything but a JSON object."""
    try:
        message = json.loads(raw)
    except ValueError as exc:
        raise StreamError("Keeper sent an invalid WebSocket message") from exc
    if not isinstance(message, dict):
        raise StreamError("Keeper sent an invalid WebSocket message")
    return message


# Seconds to wait for the server's close frame when disconnecting.
CLOSE_TIMEOUT = 2.0


@contextmanager
def _open(
    ws_endpoint: str, timeout: float, close_timeout: float = CLOSE_TIMEOUT
) -> Generator[ClientConnection]:
    """Connect and finish the graphql-transport-ws handshake.

    The connect and the wait for `connection_ack` share one TIMEOUT in
    seconds; the close at exit waits at most CLOSE_TIMEOUT for the server.
    Raises StreamError on a refused handshake and one of DISCONNECTS on a
    dropped or slow connection.
    """
    deadline = time.monotonic() + timeout
    with connect(
        ws_endpoint,
        subprotocols=[Subprotocol("graphql-transport-ws")],
        open_timeout=timeout,
        close_timeout=min(close_timeout, timeout),
    ) as socket:
        try:
            socket.send(json.dumps({"type": "connection_init"}))
            remaining = max(deadline - time.monotonic(), 0.001)
            message = decode(socket.recv(timeout=remaining))
            if message.get("type") != "connection_ack":
                raise StreamError("Keeper did not acknowledge WebSocket connection")
        except BaseException:
            # A failed handshake must not wait past TIMEOUT for the close.
            socket.close_timeout = max(deadline - time.monotonic(), 0.001)
            raise
        yield socket


def handshake(ws_endpoint: str, timeout: float) -> None:
    """Check that WS_ENDPOINT acknowledges a connection, then disconnect
    without subscribing, all within TIMEOUT seconds."""
    check_ws(ws_endpoint)
    deadline = time.monotonic() + timeout
    with _open(ws_endpoint, timeout) as socket:
        # The close must fit in what is left of TIMEOUT.
        socket.close_timeout = max(deadline - time.monotonic(), 0.001)


def payloads(
    ws_endpoint: str,
    query: str,
    variables: dict[str, Any],
    timeout: float | None = None,
    notify: Callable[[str], None] = _ignore,
) -> Generator[dict[str, Any]]:
    """Connect once and yield each checked snapshot payload.

    Raises StreamError when Keeper does not acknowledge the connection, sends
    an invalid message, or fails or ends the subscription, and one of DISCONNECTS
    when the connection drops or a read exceeds TIMEOUT seconds.
    """
    with _open(ws_endpoint, 10) as socket:
        socket.send(
            json.dumps(
                {
                    "id": "1",
                    "type": "subscribe",
                    "payload": {"query": query, "variables": variables},
                }
            )
        )
        notify(CONNECTED)
        while True:
            message = decode(socket.recv(timeout=timeout))
            if message.get("type") == "ping":
                socket.send(json.dumps({"type": "pong"}))
            elif message.get("type") == "next":
                yield check_payload(message.get("payload"), query)
            elif message.get("type") in ("error", "complete"):
                raise StreamError("Keeper subscription ended")


def snapshot(
    ws_endpoint: str,
    query: str,
    variables: dict[str, Any],
    notify: Callable[[str], None] = _ignore,
) -> dict[str, Any]:
    """Read the first snapshot of a subscription and disconnect."""
    check_ws(ws_endpoint)
    stream = payloads(ws_endpoint, query, variables, timeout=20, notify=notify)
    try:
        return next(stream)
    except DISCONNECTS as exc:
        raise StreamError("Could not read balance snapshot") from exc
    finally:
        stream.close()
