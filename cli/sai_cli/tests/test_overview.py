"""Replay mainnet Keeper snapshots over HTTP and WebSocket on loopback."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from mcp.types import TextContent
from sai_cli.core import keeper, settings, storage, stream
from sai_cli.mcp_server.server import create_server
from sai_cli.sai_cli import main
from websockets.sync.server import serve
from websockets.typing import Subprotocol

FIXTURES = Path(__file__).parent / "fixtures"
ADDR = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
BLOCK_TIME = "2026-05-21T12:05:00+00:00"


@pytest.fixture
def replay():
    recorded = json.loads((FIXTURES / "mainnet_account_overview.json").read_text())
    quotes = json.loads((FIXTURES / "mainnet_prices_http.json").read_text())
    state: dict[str, Any] = {
        "recorded": recorded,
        "ws": recorded["balances"],
        "http": {
            keeper.OVERVIEW_TRADES: recorded["trades"],
            keeper.PENDING_MARKET_ORDERS: recorded["marketOrders"],
            keeper.PRICES: quotes,
        },
        "calls": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def answer(self, payload, status=200):
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            query, variables = data["query"], data["variables"]
            assert self.headers.get("Authorization") is None
            state["calls"].append((query, variables))
            self.answer(state["http"][query])

        def do_GET(self):
            height = self.path.rsplit("/", 1)[-1]
            state["calls"].append(("lcd", height))
            if state.get("lcdPruned"):
                self.answer({"message": "height not available"}, status=404)
            else:
                self.answer(
                    {"block": {"header": {"height": height, "time": BLOCK_TIME}}}
                )

        def log_message(self, format: str, *args: Any) -> None:
            pass

    def subscribe(connection):
        assert json.loads(connection.recv()) == {"type": "connection_init"}
        connection.send(json.dumps({"type": "connection_ack"}))
        request = json.loads(connection.recv())
        assert request["type"] == "subscribe"
        assert request["payload"]["query"] == stream.BALANCES_WITH_BLOCK
        state["calls"].append(
            (stream.BALANCES_WITH_BLOCK, request["payload"]["variables"])
        )
        connection.send(json.dumps({"id": "1", "type": "next", "payload": state["ws"]}))

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with serve(
        subscribe, "127.0.0.1", 0, subprotocols=[Subprotocol("graphql-transport-ws")]
    ) as ws:
        servers = (http, ws)
        threads = [threading.Thread(target=s.serve_forever) for s in servers]
        for thread in threads:
            thread.start()
        try:
            host = f"127.0.0.1:{http.server_port}"
            state["config"] = settings.load(
                {
                    "keeper.endpoint": f"http://{host}/query",
                    "keeper.ws_endpoint": f"ws://127.0.0.1:{ws.socket.getsockname()[1]}/query",
                    "lcd.endpoint": f"http://{host}",
                }
            )
            yield state
        finally:
            for running in servers:
                running.shutdown()
            for thread in threads:
                thread.join()
            http.server_close()


def cli(replay: dict[str, Any], *args: str):
    config = replay["config"]
    return CliRunner().invoke(
        main,
        [
            "--endpoint",
            config.endpoint,
            "--ws-endpoint",
            config.ws_endpoint,
            "--lcd-endpoint",
            config.lcd_endpoint,
            *args,
        ],
    )


def mcp(replay: dict[str, Any], **args: str):
    server = create_server(replay["config"])
    result = asyncio.run(server.call_tool("account_overview", args))
    content = result[0] if isinstance(result, tuple) else result
    assert isinstance(content, list)
    block = content[0]
    assert isinstance(block, TextContent)
    return json.loads(block.text)


def save_session(replay: dict[str, Any], addr: str = ADDR):
    now = datetime.now(UTC)
    issued = (now - timedelta(days=28)).timestamp()
    part = base64.urlsafe_b64encode(json.dumps({"iat": issued}).encode()).decode()
    storage.save_session(
        {
            "token": f"test.{part}.signature",
            "evmAddress": addr,
            "expiresAt": (now + timedelta(hours=48)).isoformat(),
            "keyName": "fixture",
            "endpoint": replay["config"].endpoint,
        }
    )


def static_fields(value: Any) -> Any:
    if isinstance(value, list):
        return [static_fields(item) for item in value]
    if isinstance(value, dict):
        return {
            key: static_fields(item)
            for key, item in value.items()
            if key not in {"fetchedAt", "sourceAgeSeconds"}
        }
    return value


def test_replays_mainnet_and_preserves_freshness_on_both_surfaces(replay):
    save_session(replay)
    result = cli(replay, "account", "overview", "--json")
    assert result.exit_code == 0, result.output
    decoded = json.loads(result.stdout)
    assert decoded["addr"] == ADDR
    assert decoded["addrSource"] == "session"
    data = decoded["balances"]["data"]
    assert data["balance"]["usd"] == 150.813685
    assert data["credits"]["usd"] == 22.091064
    assert data["balance"]["sourceAt"] == BLOCK_TIME
    assert data["balance"]["sourceBlock"] == 49819589
    fetched = datetime.fromisoformat(decoded["balances"]["fetchedAt"])
    source_at = datetime.fromisoformat(data["balance"]["sourceAt"])
    assert data["balance"]["sourceAgeSeconds"] == pytest.approx(
        (fetched - source_at).total_seconds(), abs=0.001
    )
    assert decoded["positions"]["data"] == {"items": [], "unrealizedPnlUsd": 0}
    assert decoded["queuedMarketOrders"]["data"] == []
    session = decoded["session"]["data"]
    assert "expires in" in session["expiryWarning"]
    assert session["renewalCommand"] == f"sai auth renew {ADDR}"
    assert session["sourceAgeSeconds"] == pytest.approx(28 * 86400, abs=5)
    assert [query for query, _ in replay["calls"]].count("lcd") == 1
    assert keeper.PRICES not in {query for query, _ in replay["calls"]}
    assert static_fields(decoded) == static_fields(mcp(replay))
    assert not (storage.home() / "intents.sqlite3").exists()


def populate_trades(replay: dict[str, Any]):
    # The recorded account had no open trades. Keep the observed trade shape,
    # add synthetic state and orders, and use the archived oracle prices.
    long, short = replay["recorded"]["historicalTradeShape"]["data"]["perp"]["trades"]
    quotes = replay["http"][keeper.PRICES]["data"]["oracle"]["tokenPricesUsd"]
    usd = {row["token"]["id"]: row["priceUsd"] for row in quotes}
    btc, arb = usd[3], usd[21]
    long.update(
        collateralAmount=12_000_000,
        leverage=4,
        tp=btc * 1.125,
        sl=btc * 0.75,
        state={
            "liquidationPrice": btc * 0.5,
            "pnlCollateral": 2_000_000,
            "pnlPct": 0.2,
        },
    )
    short.update(
        tp=arb * 0.875,
        sl=arb * 1.25,
        state={
            "liquidationPrice": arb * 1.5,
            "pnlCollateral": -1_000_000,
            "pnlPct": -0.1,
        },
    )
    replay["http"][keeper.OVERVIEW_TRADES] = {
        "data": {
            "perp": {
                "trades": [
                    long,
                    short,
                    {**long, "id": 2, "tradeType": "limit", "state": None},
                    {**short, "id": 3, "tradeType": "stop", "state": None},
                ]
            }
        }
    }
    replay["http"][keeper.PENDING_MARKET_ORDERS] = {
        "data": {
            "perp": {
                "marketOrders": [
                    {
                        "sequence": 400,
                        "tradeId": 10,
                        "side": "open",
                        "perpMarketId": 0,
                        "status": "pending_exec",
                        "collateralAmount": 5_000_000,
                        "orderReqBlock": 41148706,
                        "orderReqBlockTs": BLOCK_TIME,
                        "orderReqTxHash": "SYNTHETIC_COSMOS_REQUEST",
                        "orderReqEvmTxHash": None,
                    },
                ]
            }
        }
    }
    return usd[1]


def test_open_positions_limits_stops_and_next_oracle_orders(replay):
    usdc = populate_trades(replay)
    endpoint = replay["config"].endpoint
    storage.pending("accepted-close", endpoint, ADDR, "CLOSE_POSITION", "1")
    storage.set_status("accepted-close", endpoint, ADDR, "accepted")
    storage.pending("recent-open", endpoint, ADDR, "OPEN_TRADE", "")
    storage.set_status("recent-open", endpoint, ADDR, "accepted")
    storage.pending("unknown-close", endpoint, ADDR, "CLOSE_POSITION", "0")
    storage.pending("other-account", endpoint, OTHER, "CLOSE_POSITION", "0")
    save_session(replay, OTHER)

    expected_scopes = ["allTrades", "mcpOpen", "sameTrade"]
    result = cli(replay, "--json", "--as", ADDR, "account", "overview")
    assert result.exit_code == 0, result.output
    decoded = json.loads(result.stdout)
    assert decoded["addrSource"] == "argument"
    positions = decoded["positions"]["data"]
    long, short = positions["items"]
    assert long["collateralUsd"] == pytest.approx(12 * usdc)
    assert long["sizeUsd"] == pytest.approx(48 * usdc)
    assert long["unrealizedPnlUsd"] == pytest.approx(2 * usdc)
    assert long["unrealizedPnlPercent"] == 20
    assert short["unrealizedPnlPercent"] == -10
    assert positions["unrealizedPnlUsd"] == pytest.approx(usdc)
    for row in (long, short):
        assert row["liquidationDistancePercent"] == pytest.approx(50)
        assert row["takeProfitDistancePercent"] == pytest.approx(12.5)
        assert row["stopLossDistancePercent"] == pytest.approx(25)
        assert row["sourceAt"] is not None

    pending = decoded["pendingOrders"]["data"]
    assert [row["orderType"] for row in pending] == ["limit", "stop"]
    queued = decoded["queuedMarketOrders"]["data"][0]
    assert queued["status"] == "pending_exec" and queued["sequence"] == 400
    assert queued["sourceAt"] == BLOCK_TIME
    assert queued["collateralBaseUnits"] == 5_000_000
    assert [
        row["scope"] for row in decoded["blockingIntents"]["data"]
    ] == expected_scopes
    assert decoded["blockingIntents"]["data"][0]["sourceAt"] is not None
    assert decoded["session"]["data"] == {"connected": False}
    assert static_fields(decoded) == static_fields(mcp(replay, addr=ADDR))
    assert storage.load_session()["evmAddress"] == OTHER

    human = cli(replay, "account", "overview", "--as", ADDR)
    assert human.exit_code == 0, human.output
    for label in (
        "Sai Funds",
        "Open Positions",
        "Pending Limit and Stop Orders",
        "Queued Market Orders",
        "SYNTHETIC_COSMOS_REQUEST",
        "BTC",
        "ARB",
        "12.5%",
        "Local Intents Blocking Trades",
        "unknown-close",
    ):
        assert label in human.stdout


@pytest.mark.parametrize("failure", [keeper.OVERVIEW_TRADES, keeper.PRICES, "ws"])
def test_a_failed_read_preserves_other_sections(replay, failure):
    populate_trades(replay)
    error = {"errors": [{"message": "synthetic failure"}]}
    if failure == "ws":
        replay["ws"] = error
    else:
        replay["http"][failure] = error
    replay["lcdPruned"] = True
    result = mcp(replay, addr=ADDR)
    if failure == keeper.OVERVIEW_TRADES:
        assert result["positions"]["error"]["code"] == "KEEPER_ERROR"
        assert result["pendingOrders"]["error"] == result["positions"]["error"]
    if failure == keeper.PRICES:
        assert result["prices"]["error"]["code"] == "KEEPER_ERROR"
        position = result["positions"]["data"]["items"][0]
        assert position["unrealizedPnlUsd"] is None
        assert position["liquidationPriceUsd"] > 0
        assert position["liquidationDistancePercent"] is None
    if failure == "ws":
        assert result["balances"]["error"]["code"] == "STREAM_ERROR"
        assert result["positions"]["data"]["items"][0]["unrealizedPnlUsd"] > 0
    else:
        balance = result["balances"]["data"]["balance"]
        assert balance["sourceBlock"] == 49819589
        assert balance["sourceAt"] is balance["sourceAgeSeconds"] is None
    assert result["queuedMarketOrders"]["data"][0]["sequence"] == 400


def test_no_default_account_gives_an_address_hint(replay):
    result = cli(replay, "account", "overview")
    assert result.exit_code != 0 and "--as ADDR" in result.output
    assert mcp(replay)["code"] == "SESSION_UNAVAILABLE"
    assert replay["calls"] == []
