"""Click-free core operations: session, reconcile, trading, and stream."""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sai_cli.core import keeper, margin, reconcile, session, storage, stream, trading
from websockets.sync.server import ServerConnection, serve
from websockets.typing import Subprotocol

ADDR = "0x" + "a" * 40
ENDPOINT = keeper.MAINNET_HTTP


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))


def session_for(endpoint: str = ENDPOINT, hours: float = 1) -> dict[str, str]:
    return {
        "token": "fake-token",
        "evmAddress": ADDR,
        "endpoint": endpoint,
        "expiresAt": (datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
    }


def jwt(claims: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return f"header.{body.decode()}.signature"


def no_session() -> dict[str, str]:
    raise AssertionError("session must not be loaded")


# Session


def test_load_active_session_checks_endpoint_and_expiry():
    stored = session_for()
    storage.save_session(stored)
    assert session.load_active_session(ENDPOINT) == stored
    with pytest.raises(ValueError, match="another Keeper endpoint"):
        session.load_active_session("http://127.0.0.1:1/query")
    storage.save_session(session_for(hours=-1))
    with pytest.raises(ValueError, match="expired"):
        session.load_active_session(ENDPOINT)


def test_passphrase_session_is_refused_without_a_prompt():
    # A session from an older version that encrypted it with a passphrase.
    path = storage.directory() / "session.json"
    path.write_text(json.dumps({"version": 1, "nonce": "", "ciphertext": ""}))
    with pytest.raises(ValueError, match="passphrase-encrypted"):
        session.load_active_session(ENDPOINT)


def test_import_session_and_summary_hide_the_token():
    expires = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
    token = jwt({"sub": ADDR, "exp": expires, "purpose": "user_intent"})
    result = session.import_session(token, ENDPOINT)
    assert result["token"] == token
    assert result["endpoint"] == ENDPOINT
    assert session.summary(result) == {
        "addr": ADDR,
        "expiresAt": datetime.fromtimestamp(expires, UTC).isoformat(),
        "endpoint": "https://sai-keeper.nibiru.fi",
    }
    for bad in (
        "not-a-jwt",
        jwt({"sub": ADDR, "exp": expires, "purpose": "login"}),
        jwt({"sub": ADDR, "exp": 1, "purpose": "user_intent"}),
        jwt({"sub": "0x1", "exp": expires, "purpose": "user_intent"}),
    ):
        with pytest.raises(ValueError, match="Invalid or expired Sai session token"):
            session.import_session(bad, ENDPOINT)


# Fake Keeper over keeper.request


class FakeKeeper:
    """Answer keeper.request by query and record submitted envelopes."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.submitted: list[dict[str, Any]] = []

    def __call__(
        self,
        endpoint: str,
        query: str,
        variables: dict[str, Any] | None = None,
        token: str | None = None,
        timeout: float = 20,
    ) -> dict[str, Any]:
        if query == keeper.SUBMIT:
            assert variables is not None
            envelope = variables["input"]
            self.submitted.append(envelope)
            return {
                "recordUserIntent": {
                    "intentKey": envelope["intentKey"],
                    "lifecycleStep": "jwt_pass",
                    "execActor": "server",
                    "evmAddress": ADDR,
                }
            }
        return json.loads(json.dumps(self.answers[query]))

    def intent(self) -> dict[str, Any]:
        """Return the last submitted intent without the envelope fields."""
        body = dict(self.submitted[-1]["intent"])
        body.pop("intentSchemaVersion")
        body.pop("tsIntentInit")
        return body


def markets(price: float = 100.5, is_open: bool = True) -> dict[str, Any]:
    return {
        "perp": {
            "borrowings": [
                {
                    "marketId": 0,
                    "price": price,
                    "isOpen": is_open,
                    "minPositionSizeUSD": 1,
                    "collateralToken": {"id": 1, "symbol": "USDC"},
                }
            ]
        }
    }


# Reconcile


def intent_row(key: str, step: str) -> dict[str, Any]:
    return {
        "userIntents": [
            {
                "intentKey": key,
                "lifecycleStep": step,
                "execActor": "server",
                "evmAddress": ADDR.upper().replace("0X", "0x"),
                "txHash": "A" * 64,
            }
        ]
    }


def status_of(key: str) -> str:
    rows = storage.list_pending(ENDPOINT, ADDR)
    return next(row["status"] for row in rows if row["intentKey"] == key)


def test_reconcile_marks_server_failure_terminal(monkeypatch):
    storage.pending("k1", ENDPOINT, ADDR, "CLOSE_TRADE", "1")
    monkeypatch.setattr(
        keeper, "request", FakeKeeper({keeper.INTENTS: intent_row("k1", "exec_fail")})
    )
    data = reconcile.reconcile(ENDPOINT, ADDR, "k1")
    assert data == intent_row("k1", "exec_fail")
    assert status_of("k1") == "terminal"


def test_reconcile_matches_executed_trade_history(monkeypatch):
    storage.pending("k2", ENDPOINT, ADDR, "CLOSE_TRADE", "1")
    row = {"intentKey": "k2", "txHash": "a" * 64, "trade": {"id": 1, "trader": ADDR}}
    fake = FakeKeeper(
        {
            keeper.INTENTS: intent_row("k2", "exec_pass"),
            keeper.INTENT_HISTORY: {
                "perp": {"tradeHistory": [row, {"intentKey": "x"}]}
            },
        }
    )
    monkeypatch.setattr(keeper, "request", fake)
    data = reconcile.reconcile(ENDPOINT, ADDR, "k2")
    assert data["matchedTradeHistory"] == [row]
    assert status_of("k2") == "terminal"


def test_reconcile_releases_an_expired_unrecorded_key(monkeypatch):
    storage.pending("k3", ENDPOINT, ADDR, "CLOSE_TRADE", "1")
    with storage.db() as connection:
        connection.execute(
            "UPDATE intents SET created_at=? WHERE id='k3'",
            (time.time() - keeper.ABSENT_AFTER_SECONDS - 1,),
        )
    monkeypatch.setattr(
        keeper, "request", FakeKeeper({keeper.INTENTS: {"userIntents": []}})
    )
    assert reconcile.reconcile(ENDPOINT, ADDR, "k3") == {
        "userIntents": [],
        "localStatus": "absent",
    }
    assert reconcile.reconcile(ENDPOINT, ADDR) == {"userIntents": []}


# Trading


def open_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "market_id": 0,
        "side": "long",
        "order_type": "market",
        "amount": "10",
        "leverage": "2",
        "price": None,
        "slippage": "1",
        "take_profit": None,
        "stop_loss": None,
        "dry_run": True,
    }
    return args | overrides


def test_market_dry_run_needs_no_session_or_network(monkeypatch):
    monkeypatch.setattr(keeper, "request", FakeKeeper())
    result = trading.open_trade(ENDPOINT, no_session, **open_args())
    assert result["dryRun"] is True
    assert result["requiresMarketPriceRefresh"] is True
    assert result["intent"]["body"]["orderType"] == "market"


def test_validation_fails_before_the_session_loads():
    with pytest.raises(ValueError, match="Amount"):
        trading.open_trade(
            ENDPOINT, no_session, **open_args(amount="-1", dry_run=False)
        )


def test_live_market_order_submits_the_refreshed_price(monkeypatch):
    fake = FakeKeeper({keeper.MARKETS: markets(price=101.25)})
    monkeypatch.setattr(keeper, "request", fake)
    notices: list[str] = []
    result = trading.open_trade(
        ENDPOINT,
        session_for,
        **open_args(price="99", dry_run=False),
        notify=notices.append,
    )
    assert result["status"] == "accepted"
    assert result["referencePrice"] == "101.25"
    assert fake.intent()["body"]["intentPrice"] == "101.25"
    assert notices == [trading.MARKET_PRICE_NOTICE]


def test_live_market_order_refuses_a_closed_market(monkeypatch):
    fake = FakeKeeper({keeper.MARKETS: markets(is_open=False)})
    monkeypatch.setattr(keeper, "request", fake)
    with pytest.raises(ValueError, match="Current open USDC market data"):
        trading.open_trade(ENDPOINT, session_for, **open_args(dry_run=False))
    assert fake.submitted == []


@pytest.mark.parametrize(
    ("action", "args"),
    [
        (trading.close_trade, {"trade_id": 3, "cancel_order": False}),
        (trading.set_leverage, {"trade_id": 3, "leverage": "4"}),
        (
            trading.set_tpsl,
            {
                "trade_id": 3,
                "take_profit": "120",
                "stop_loss": None,
                "clear_take_profit": False,
                "clear_stop_loss": True,
            },
        ),
    ],
)
def test_preview_is_exactly_what_is_submitted(
    monkeypatch, action: Callable[..., dict[str, Any]], args: dict[str, Any]
):
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    preview = action(ENDPOINT, no_session, dry_run=True, **args)
    assert preview["dryRun"] is True
    result = action(ENDPOINT, session_for, dry_run=False, **args)
    assert result["status"] == "accepted"
    assert fake.intent() == preview["intent"]


class Refused(Exception):
    pass


def test_before_submit_sees_the_submitted_intent_and_can_refuse(monkeypatch):
    fake = FakeKeeper({keeper.MARKETS: markets(price=101.25)})
    monkeypatch.setattr(keeper, "request", fake)
    seen: list[dict[str, Any]] = []
    trading.open_trade(
        ENDPOINT,
        session_for,
        **open_args(dry_run=False),
        before_submit=seen.append,
    )
    assert seen[0]["referencePrice"] == "101.25"
    assert fake.intent() == seen[0]["intent"]

    def refuse(_preview: dict[str, Any]) -> None:
        raise Refused

    with pytest.raises(Refused):
        trading.close_trade(
            ENDPOINT,
            session_for,
            trade_id=3,
            cancel_order=False,
            dry_run=False,
            before_submit=refuse,
        )
    with pytest.raises(Refused):
        trading.set_leverage(
            ENDPOINT,
            session_for,
            trade_id=3,
            leverage="4",
            dry_run=False,
            before_submit=refuse,
        )
    with pytest.raises(Refused):
        trading.open_trade(
            ENDPOINT, session_for, **open_args(dry_run=False), before_submit=refuse
        )
    assert len(fake.submitted) == 1
    assert storage.list_pending(ENDPOINT, ADDR)[0]["status"] == "accepted"


def snapshots(monkeypatch, *collaterals: str) -> Iterator[margin.PositionSnapshot]:
    """Supply each indexed snapshot and repeat its contract state on recheck."""
    current: margin.PositionSnapshot | None = None
    states = iter(
        margin.PositionSnapshot(
            {
                "user": "nibi1trader",
                "user_trade_index": "UserTradeIndex(7)",
                "is_open": True,
                "trade_type": "trade",
                "collateral_amount": amount,
                "leverage": "2",
                "market_index": "MarketIndex(0)",
                "collateral_index": "TokenIndex(1)",
            },
            {"available_to_trade": "10000000"},
            {"marketId": 0, "minLeverage": 1.1, "maxLeverage": 100},
            "test-contract",
        )
        for amount in collaterals
    )

    def snapshot(*args: Any) -> margin.PositionSnapshot:
        nonlocal current
        current = next(states)
        return current

    def contract_reply(
        lcd: str, contract: str, query: dict[str, Any], **kwargs: Any
    ) -> Any:
        if "get_trade_raw" in query:
            assert current is not None
            return current.raw
        return "1" if "get_collateral_price" in query else False

    monkeypatch.setattr(margin, "snapshot", snapshot)
    monkeypatch.setattr(margin, "smart_value", contract_reply)
    monkeypatch.setattr(
        margin,
        "_latest_position",
        lambda *args: (contract_reply("", "", {"get_trade_raw": {}}), "120"),
    )
    return states


def test_margin_preview_matches_the_submitted_leverage(monkeypatch):
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    args: dict[str, Any] = {
        "trade_id": 7,
        "direction": "add",
        "amount": "1",
        "lcd_endpoint": margin.MAINNET_LCD,
    }
    snapshots(monkeypatch, "2000000")
    preview = trading.change_margin(ENDPOINT, session_for, dry_run=True, **args)
    assert preview["warning"] == trading.MARGIN_WARNING
    snapshots(monkeypatch, "2000000", "2000000")
    result = trading.change_margin(ENDPOINT, session_for, dry_run=False, **args)
    assert result["quote"] == preview["quote"]
    assert fake.intent() == preview["intent"]


def test_margin_refuses_a_position_that_changed(monkeypatch):
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    snapshots(monkeypatch, "2000000", "2500000")
    with pytest.raises(ValueError, match="review the margin change"):
        trading.change_margin(
            ENDPOINT,
            session_for,
            trade_id=7,
            direction="add",
            amount="1",
            lcd_endpoint=margin.MAINNET_LCD,
            dry_run=False,
        )
    assert fake.submitted == []


def test_partial_close_preview_matches_submission_and_rechecks(monkeypatch):
    fake = FakeKeeper({keeper.MARKETS: markets()})
    monkeypatch.setattr(keeper, "request", fake)
    args: dict[str, Any] = {
        "trade_id": 7,
        "percent": 50,
        "lcd_endpoint": margin.MAINNET_LCD,
    }
    snapshots(monkeypatch, "4000000")
    preview = trading.partial_close(ENDPOINT, session_for, dry_run=True, **args)
    assert preview["intent"]["body"]["collateral"]["amountUsdc"] == "2000000"
    snapshots(monkeypatch, "4000000", "4000000")
    trading.partial_close(ENDPOINT, session_for, dry_run=False, **args)
    assert fake.intent() == preview["intent"]
    snapshots(monkeypatch, "4000000", "3000000")
    with pytest.raises(ValueError, match="review the reduction"):
        trading.partial_close(ENDPOINT, session_for, dry_run=False, **args)
    assert len(fake.submitted) == 1


# Stream


def test_ws_endpoint_rules():
    assert stream.derive_ws("https://k.example/query") == "wss://k.example/query"
    assert stream.derive_ws("http://127.0.0.1:9/query") == "ws://127.0.0.1:9/query"
    with pytest.raises(stream.StreamError, match="Cannot derive"):
        stream.derive_ws("ftp://k.example")
    stream.check_ws("ws://localhost:1/query")
    with pytest.raises(stream.StreamError, match="WSS"):
        stream.check_ws("ws://k.example/query")


def test_check_payload_requires_the_subscribed_list():
    payload = {"data": {"perpTrades": []}}
    assert stream.check_payload(payload, stream.POSITIONS) == payload
    for bad in (None, {"data": {"perpTrades": None}}, {"errors": [1], "data": {}}):
        with pytest.raises(stream.StreamError, match="incomplete"):
            stream.check_payload(bad, stream.POSITIONS)


def ws_server(
    replies: list[dict[str, Any] | str],
    ack: str = json.dumps({"type": "connection_ack"}),
) -> tuple[str, Callable[[], None], list[dict[str, Any]]]:
    """Serve one graphql-transport-ws session on loopback.

    ACK is the raw first reply; a string reply is sent as-is.
    """
    received: list[dict[str, Any]] = []

    def handler(connection: ServerConnection) -> None:
        received.append(json.loads(connection.recv()))
        connection.send(ack)
        if ack != json.dumps({"type": "connection_ack"}):
            time.sleep(0.2)
            return
        received.append(json.loads(connection.recv()))
        for reply in replies:
            connection.send(reply if isinstance(reply, str) else json.dumps(reply))
        time.sleep(0.2)

    server = serve(
        handler, "127.0.0.1", 0, subprotocols=[Subprotocol("graphql-transport-ws")]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        thread.join()

    port = server.socket.getsockname()[1]
    return f"ws://127.0.0.1:{port}/query", stop, received


def test_snapshot_answers_ping_and_returns_the_first_payload():
    payload = {"data": {"userBalances": [{"amount": "1"}]}}
    url, stop, received = ws_server(
        [{"type": "ping"}, {"id": "1", "type": "next", "payload": payload}]
    )
    notices: list[str] = []
    try:
        result = stream.snapshot(
            url, stream.BALANCES, {"user": ADDR}, notify=notices.append
        )
    finally:
        stop()
    assert result == payload
    assert notices == [stream.CONNECTED]
    assert received[1]["payload"] == {
        "query": stream.BALANCES,
        "variables": {"user": ADDR},
    }


def test_snapshot_reports_an_ended_subscription():
    url, stop, _ = ws_server([{"id": "1", "type": "complete"}])
    try:
        with pytest.raises(stream.StreamError, match="subscription ended"):
            stream.snapshot(url, stream.BALANCES, {"user": ADDR})
    finally:
        stop()


def test_snapshot_reports_an_unreachable_endpoint():
    url, stop, _ = ws_server([])
    stop()
    with pytest.raises(stream.StreamError, match="Could not read balance snapshot"):
        stream.snapshot(url, stream.BALANCES, {"user": ADDR})


@pytest.mark.parametrize(
    ("ack", "replies"),
    [
        (json.dumps({"type": "connection_error"}), []),
        ("not json", []),
        (json.dumps({"type": "connection_ack"}), ["{broken"]),
        (json.dumps({"type": "connection_ack"}), ["[1, 2]"]),
    ],
)
def test_snapshot_reports_a_bad_handshake_or_message_as_a_stream_error(
    ack: str, replies: list[dict[str, Any] | str]
):
    url, stop, _ = ws_server(replies, ack=ack)
    try:
        with pytest.raises(stream.StreamError):
            stream.snapshot(url, stream.BALANCES, {"user": ADDR})
    finally:
        stop()
