"""Replay mainnet Keeper snapshots over HTTP and WebSocket on loopback."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from click.testing import CliRunner
from mcp.types import TextContent
from sai_cli.core import keeper, overview, reconcile, settings, storage, stream
from sai_cli.mcp_server.server import create_server
from sai_cli.sai_cli import main
from websockets.sync.server import serve
from websockets.typing import Subprotocol

FIXTURES = Path(__file__).parent / "fixtures"
ADDR = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
BLOCK_TIME = "2026-05-21T12:05:00+00:00"
RISK_BLOCK = 50_407_230
MARK_BLOCK = RISK_BLOCK - 37
CHAIN_ID = "fixture-1"


@pytest.fixture
def replay():
    recorded = json.loads((FIXTURES / "mainnet_account_overview.json").read_text())
    quotes = json.loads((FIXTURES / "mainnet_prices_http.json").read_text())
    risk_time = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=3)
    stop_drips = threading.Event()
    state: dict[str, Any] = {
        "riskTime": risk_time.isoformat(),
        "markTime": (risk_time - timedelta(seconds=17)).isoformat(),
        "recorded": recorded,
        "ws": recorded["balances"],
        "http": {
            keeper.OVERVIEW_TRADES: recorded["trades"],
            keeper.PENDING_MARKET_ORDERS: recorded["marketOrders"],
            keeper.PRICES: quotes,
            keeper.INTENTS: {"data": {"userIntents": []}},
            keeper.ROUTE: {
                "data": {
                    "oneClickTrading": {
                        "chainId": CHAIN_ID,
                        "contractAddress": "nibi1fixture",
                        "enabled": True,
                    }
                }
            },
        },
        "calls": [],
        "contractTrades": {},
        "oracleRates": {},
        "contractMarkets": {},
        "expiration": 300,
        "lcdDisconnected": threading.Event(),
    }

    class Handler(BaseHTTPRequestHandler):
        def answer(self, payload, status=200, served_at=None):
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            if served_at is not None:
                self.send_header("grpc-metadata-x-cosmos-block-height", str(served_at))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except BrokenPipeError:
                pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            query, variables = data["query"], data["variables"]
            assert self.headers.get("Authorization") is None
            state["calls"].append((query, variables))
            response = state["http"][query]
            self.answer(response(variables) if callable(response) else response)

        def drip(self) -> None:
            started = time.monotonic()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                while not stop_drips.is_set():
                    self.wfile.write(b" ")
                    stop_drips.wait(0.02)
            except (BrokenPipeError, ConnectionResetError):
                state["lcdDripSeconds"] = time.monotonic() - started
                state["lcdDisconnected"].set()

        def do_GET(self):
            path = urlsplit(self.path).path
            drip_path = state.get("dripPath")
            if drip_path and drip_path in path:
                del state["dripPath"]
                self.drip()
                return
            if "/smart/" in path:
                message = json.loads(base64.b64decode(unquote(path.rsplit("/", 1)[-1])))
                assert self.headers["x-cosmos-block-height"] == str(RISK_BLOCK)
                state["calls"].append(("contract", message))
                if state.get("dripQuery") in message:
                    self.drip()
                    return
                if "get_trade" in message:
                    args = message["get_trade"]
                    assert args["trader"] == ADDR
                    if args["index"] >= state.get("stallFromTrade", 10**9):
                        time.sleep(2.5)
                    data = state["contractTrades"].get(args["index"])
                elif "get_market" in message:
                    data = state["contractMarkets"].get(message["get_market"]["index"])
                elif "get_group" in message:
                    data = {"stale_price_expiration_secs": state["expiration"]}
                elif "get_oracle_address" in message:
                    data = "nibi1oraclefixture"
                elif "get_exchange_rate_stamped" in message:
                    assert "/nibi1oraclefixture/" in path
                    pair = message["get_exchange_rate_stamped"]
                    data = state["oracleRates"].get((pair["base"], pair["quote"]))
                elif "get_base_size_seed_status" in message:
                    data = state.get("seed", "complete")
                else:
                    cursor = message["live_trades_by_user"]["start_after"]
                    ids = state.get("liveTradeIds", sorted(state["contractTrades"]))
                    data = [
                        {"trade_index": index}
                        for index in ids
                        if cursor is None or index > cursor
                    ][:100]
                if data is None:
                    self.answer({"message": "not found"}, status=404)
                else:
                    self.answer(
                        {"data": data}, served_at=state.get("servedAt", RISK_BLOCK)
                    )
                return
            height = path.rsplit("/", 1)[-1]
            state["calls"].append(("lcd", height))
            if height != "latest" and state.get("lcdPruned"):
                self.answer({"message": "height not available"}, status=404)
            else:
                self.answer(
                    {
                        "block": {
                            "header": {
                                "height": str(RISK_BLOCK)
                                if height == "latest"
                                else height,
                                "time": state["riskTime"]
                                if height == "latest"
                                else BLOCK_TIME,
                                "chain_id": state.get("lcdChain", CHAIN_ID),
                            }
                        }
                    }
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
            stop_drips.set()
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
    return decode_mcp(asyncio.run(server.call_tool("account_overview", args)))


def decode_mcp(result: Any) -> dict[str, Any]:
    content = result[0] if isinstance(result, tuple) else result
    assert isinstance(content, list)
    block = content[0]
    assert isinstance(block, TextContent)
    return json.loads(block.text)


def test_market_prices_reports_missing_block_times_as_null(replay):
    prices = replay["http"][keeper.PRICES]["data"]
    rows = prices["oracle"]["tokenPricesUsd"][:11]
    prices["oracle"]["tokenPricesUsd"] = rows
    del rows[0]["lastUpdatedBlock"]["block_ts"]
    for row, stamp in zip(
        rows[1:],
        (
            0,
            None,
            "0",
            "1970-01-01T00:00:00Z",
            "1970-01-01t00:00:00z",
            "1970-01-01 01:00:00.0000000000+01:00",
            "1969-12-31T23:00:00−01:00",
            BLOCK_TIME,
            "1970-01-01T00:00:00.000000001Z",
            "1970-01-01t00:00:00.0000000001z",
        ),
        strict=True,
    ):
        row["lastUpdatedBlock"]["block_ts"] = stamp
    heights = [row["lastUpdatedBlock"]["block"] for row in rows]

    result = cli(replay, "--json", "market", "prices")
    assert result.exit_code == 0, result.output
    decoded = json.loads(result.stdout)
    blocks = [row["lastUpdatedBlock"] for row in decoded["oracle"]["tokenPricesUsd"]]
    assert [block["block"] for block in blocks] == heights
    assert [block["block_ts"] for block in blocks] == [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        BLOCK_TIME,
        "1970-01-01T00:00:00.000000001Z",
        "1970-01-01t00:00:00.0000000001z",
    ]
    server = create_server(replay["config"])
    assert decode_mcp(asyncio.run(server.call_tool("market_prices", {}))) == decoded


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
            and not (
                key == "sourceAt"
                and item is not None
                and (
                    item == value.get("fetchedAt")
                    or value.get("sourceAtBasis") == "fetchedAt"
                )
            )
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
    # The test WebSocket is served from a separate port with no proven chain.
    assert data["balance"]["sourceAt"] == decoded["balances"]["fetchedAt"]
    assert data["balance"]["sourceAtBasis"] == "fetchedAt"
    assert data["balance"]["sourceAgeSeconds"] is None
    assert data["balance"]["sourceBlock"] == 49819589
    assert data["unsettled"]["sourceAt"] is None
    assert datetime.fromisoformat(decoded["balances"]["fetchedAt"]).tzinfo == UTC
    assert decoded["positions"]["data"] == {
        "items": [],
        "unrealizedPnlUsd": 0,
        "unrealizedPnlBasis": "grossBeforeFees",
    }
    membership = decoded["positions"]["membership"]
    assert membership["status"] == "verified"
    assert membership["sourceBlock"] == RISK_BLOCK
    assert membership["sourceAt"] == replay["riskTime"]
    assert 0 <= membership["sourceAgeSeconds"] <= overview.MEMBERSHIP_MAX_AGE_SECONDS
    assert decoded["queuedMarketOrders"]["data"] == []
    session = decoded["session"]["data"]
    assert "expires in" in session["expiryWarning"]
    command = session["renewalCommand"]
    assert command.startswith('sai --endpoint "${SAI_RECONCILE_ENDPOINT:?')
    assert f"auth renew {ADDR}" in command
    assert replay["config"].endpoint not in command
    assert session["sourceAgeSeconds"] == pytest.approx(28 * 86400, abs=5)
    assert [args for query, args in replay["calls"] if query == "lcd"] == ["latest"]
    assert keeper.PRICES not in {query for query, _ in replay["calls"]}
    tool_output = mcp(replay)
    assert static_fields(decoded) == static_fields(tool_output)
    for output in (decoded, tool_output):
        for key in (
            "balances",
            "positions",
            "pendingOrders",
            "queuedMarketOrders",
            "blockingIntents",
            "session",
        ):
            assert output[key]["sourceAt"] == output[key]["fetchedAt"]
            assert datetime.fromisoformat(output[key]["sourceAt"]).tzinfo == UTC
        assert (
            output["balances"]["data"]["balance"]["sourceAt"]
            == output["balances"]["sourceAt"]
        )
        assert output["prices"] == {
            "fetchedAt": None,
            "data": [],
            "status": "skipped",
            "skipReason": "No positions or pending orders were available to price.",
        }
    human = cli(replay, "account", "overview")
    assert (
        "Skipped. No positions or pending orders were available to price."
        in human.stdout
    )
    assert not (storage.home() / "intents.sqlite3").exists()


def install_oracle_quote(replay, base, quote, *, mark, quote_usd=1):
    mark_at = int(datetime.fromisoformat(replay["markTime"]).timestamp())
    replay["oracleRates"][base, quote] = {
        "rate": {
            "base_per_quote": str(mark),
            "quote_price_usd": str(quote_usd),
            "base_last_update_time": mark_at,
            "quote_last_update_time": mark_at - 3,
        },
        "base": {"price_block": MARK_BLOCK, "price_block_time_ms": mark_at * 1000},
        "quote": {
            "price_block": MARK_BLOCK - 1,
            "price_block_time_ms": (mark_at - 3) * 1000,
        },
    }


def install_contract_trade(replay, trade, *, mark, liquidation, quote_usd=1):
    market = trade["perpBorrowing"]
    market_id = f"MarketIndex({market['marketId']})"
    replay["contractTrades"][trade["id"]] = {
        "trade": {
            "user": trade["trader"],
            "user_trade_index": f"UserTradeIndex({trade['id']})",
            "market_index": market_id,
            "collateral_index": f"TokenIndex({market['collateralToken']['id']})",
            "long": trade["isLong"],
            "is_open": True,
            "trade_type": "trade",
            "collateral_amount": str(trade["collateralAmount"]),
            "leverage": str(trade["leverage"]),
            "open_price": str(trade["openPrice"]),
            "tp": str(trade["tp"]) if trade["tp"] is not None else None,
            "sl": str(trade["sl"]) if trade["sl"] is not None else None,
        },
        "liquidation_price": str(liquidation),
    }
    base, quote = market["baseToken"]["id"], market["quoteToken"]["id"]
    replay["contractMarkets"][market_id] = {
        "base": f"TokenIndex({base})",
        "quote": f"TokenIndex({quote})",
        "group_index": "GroupIndex(0)",
    }
    install_oracle_quote(replay, base, quote, mark=mark, quote_usd=quote_usd)


def populate_trades(replay: dict[str, Any]):
    # Keeper recorded no open trades. Adapt the two historical rows and
    # serve synthetic contract responses with independently timed prices.
    long, short = replay["recorded"]["historicalTradeShape"]["data"]["perp"]["trades"]
    long.update(
        collateralAmount=12_000_000,
        leverage=4,
        openPrice=80,
        tp=112.5,
        sl=75,
        state={"liquidationPrice": 20},
    )
    short.update(
        collateralAmount=10_000_000,
        leverage=2,
        openPrice=8,
        tp=8.75,
        sl=12.5,
        state={"liquidationPrice": 25},
    )
    install_contract_trade(replay, long, mark=100, liquidation=50)
    install_contract_trade(replay, short, mark=10, liquidation=15)
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
    return long, short


def test_open_positions_limits_stops_and_next_oracle_orders(replay):
    populate_trades(replay)
    endpoint = replay["config"].endpoint
    storage.pending("accepted-close", endpoint, ADDR, "CLOSE_POSITION", "1")
    storage.set_status("accepted-close", endpoint, ADDR, "accepted")
    storage.pending("accepted-open", endpoint, ADDR, "OPEN_TRADE", "")
    storage.set_status("accepted-open", endpoint, ADDR, "accepted")
    with storage.db() as connection:
        connection.execute(
            "UPDATE intents SET created_at = 1 WHERE id = ?", ("accepted-open",)
        )
    storage.pending("unknown-close", endpoint, ADDR, "CLOSE_POSITION", "0")
    storage.pending("other-account", endpoint, OTHER, "CLOSE_POSITION", "0")
    save_session(replay, OTHER)

    expected_scopes = ["allTrades", "openTrades", "sameTrade"]
    result = cli(replay, "--json", "--as", ADDR, "account", "overview")
    assert result.exit_code == 0, result.output
    decoded = json.loads(result.stdout)
    assert decoded["addrSource"] == "argument"
    assert decoded["prices"]["sourceAt"] == decoded["prices"]["fetchedAt"]
    positions = decoded["positions"]["data"]
    long, short = positions["items"]
    assert long["collateralUsd"] == 12
    assert long["sizeUsd"] == 48
    assert long["unrealizedPnlUsd"] == 12
    assert long["unrealizedPnlPercent"] == 100
    assert short["unrealizedPnlPercent"] == -50
    assert positions["unrealizedPnlUsd"] == 7
    for row in (long, short):
        assert row["liquidationDistancePercent"] == pytest.approx(50)
        assert row["takeProfitDistancePercent"] == pytest.approx(12.5)
        assert row["stopLossDistancePercent"] == pytest.approx(25)
        assert row["sourceAt"] is not None
        assert row["riskStatus"] == "verified"
        assert row["riskSource"]["sourceBlock"] == RISK_BLOCK
        assert row["riskSource"]["sourceAt"] == replay["riskTime"]
        assert row["markSource"]["sourceAt"] == replay["markTime"]
        assert row["markSource"]["sourceBlock"] == MARK_BLOCK
        assert row["markSource"]["queryBlock"] == RISK_BLOCK
        assert row["sourceAt"] != row["riskSource"]["sourceAt"]
        for field in ("riskSource", "markSource"):
            source = row[field]
            assert source["sourceAgeSeconds"] == pytest.approx(
                (
                    datetime.fromisoformat(source["fetchedAt"])
                    - datetime.fromisoformat(source["sourceAt"])
                ).total_seconds(),
                abs=0.001,
            )

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
        "gross unrealized PnL before fees",
        "contract risk",
        "oracle mark",
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
        assert result["prices"]["status"] == "skipped"
        assert (
            result["prices"]["skipReason"]
            == "Positions and pending orders could not be loaded."
        )
    if failure == keeper.PRICES:
        assert result["prices"]["error"]["code"] == "KEEPER_ERROR"
        assert "status" not in result["prices"]
        position = result["positions"]["data"]["items"][0]
        assert position["unrealizedPnlUsd"] == 12
        assert position["liquidationPriceUsd"] == 50
        assert position["liquidationDistancePercent"] == 50
    if failure == "ws":
        assert result["balances"]["error"]["code"] == "STREAM_ERROR"
        assert result["positions"]["data"]["items"][0]["unrealizedPnlUsd"] > 0
    else:
        balance = result["balances"]["data"]["balance"]
        assert balance["sourceBlock"] == 49819589
        assert balance["sourceAt"] == result["balances"]["fetchedAt"]
        assert balance["sourceAtBasis"] == "fetchedAt"
        assert balance["sourceAgeSeconds"] is None
    assert result["queuedMarketOrders"]["data"][0]["sequence"] == 400


def test_no_default_account_gives_an_address_hint(replay):
    result = cli(replay, "account", "overview")
    assert result.exit_code != 0 and "--as ADDR" in result.output
    assert mcp(replay)["code"] == "SESSION_UNAVAILABLE"
    assert replay["calls"] == []


def test_contract_threshold_reports_crossing_even_when_keeper_estimate_does_not(replay):
    long, _ = populate_trades(replay)
    long.update(openPrice=100, leverage=10, state={"liquidationPrice": 92})
    install_contract_trade(replay, long, mark="92.50", liquidation=93)
    position = mcp(replay, addr=ADDR)["positions"]["data"]["items"][0]
    assert position["liquidationPriceUsd"] == 93
    assert position["liquidationDistancePercent"] == pytest.approx(-0.5 / 92.5 * 100)
    assert "keeperLiquidationEstimatePriceQuote" not in position


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        ("different_leverage", "inconsistent"),
        ("wrong_height", "unavailable"),
        ("no_oracle_time", "partial"),
    ],
)
def test_unverified_risk_has_only_a_labeled_estimate(replay, failure, status):
    populate_trades(replay)
    if failure == "different_leverage":
        replay["contractTrades"][0]["trade"]["leverage"] = "10"
    elif failure == "wrong_height":
        replay["servedAt"] = RISK_BLOCK - 1
    else:
        replay["oracleRates"][3, 0]["rate"]["base_last_update_time"] = None
    overview = mcp(replay, addr=ADDR)["positions"]["data"]
    position = overview["items"][0]
    assert position["riskStatus"] == status
    assert position["liquidationDistancePercent"] is None
    assert position["unrealizedPnlUsd"] is None
    assert overview["unrealizedPnlUsd"] is None
    assert position["sourceAt"] is not None
    human = cli(replay, "account", "overview", "--as", ADDR)
    assert human.exit_code == 0, human.output
    if status == "partial":
        assert position["liquidationPriceUsd"] == 50
        assert position["riskSource"]["sourceAt"] == replay["riskTime"]
        assert "keeperLiquidationEstimatePriceQuote" not in position
    else:
        assert position["liquidationPriceUsd"] is None
        assert position["keeperLiquidationEstimatePriceQuote"] == 20
        assert position["keeperLiquidationEstimateSource"]["sourceAt"] is None
        assert (
            "Keeper liquidation estimate 20 uusd. Source time unknown." in human.stdout
        )


def test_non_usd_market_compares_pair_units_and_shows_both_oracle_ages(replay):
    long, _ = populate_trades(replay)
    long.update(openPrice=50, tp=55, sl=47)
    long["perpBorrowing"]["quoteToken"] = {"id": 17, "symbol": "QUOTE"}
    install_contract_trade(replay, long, mark=50, liquidation=45, quote_usd=2)
    position = mcp(replay, addr=ADDR)["positions"]["data"]["items"][0]
    assert position["markPriceUsd"] == 100
    assert position["liquidationPriceUsd"] == 90
    assert position["liquidationDistancePercent"] == 10
    assert position["takeProfitPriceUsd"] == 110
    assert position["takeProfitDistancePercent"] == 10
    assert position["stopLossPriceUsd"] == 94
    assert position["stopLossDistancePercent"] == 6
    assert position["markSource"]["base"]["sourceAt"] == replay["markTime"]
    assert (
        position["markSource"]["quote"]["sourceAgeSeconds"]
        > position["markSource"]["base"]["sourceAgeSeconds"]
    )


@pytest.mark.parametrize(
    ("mark", "percent", "usd"), [(150, 900, 900), (50, -100, -100)]
)
def test_gross_pnl_follows_contract_cap_with_no_usdc_quote(replay, mark, percent, usd):
    long, _ = populate_trades(replay)
    long.update(openPrice=100, leverage=20, collateralAmount=100_000_000)
    install_contract_trade(replay, long, mark=mark, liquidation=96)
    replay["http"][keeper.PRICES]["data"]["oracle"]["tokenPricesUsd"] = []
    position = mcp(replay, addr=ADDR)["positions"]["data"]["items"][0]
    assert position["collateralUsd"] == 100
    assert position["sizeUsd"] == 2_000
    assert position["unrealizedPnlPercent"] == percent
    assert position["unrealizedPnlUsd"] == usd
    assert position["unrealizedPnlBasis"] == "grossBeforeFees"


def test_empty_balance_snapshot_does_not_confirm_zero(replay):
    replay["ws"] = {"data": {"userBalances": []}}
    balances = mcp(replay, addr=ADDR)["balances"]
    assert balances["error"]["code"] == "INCOMPLETE_DATA"
    assert "unknown" in balances["error"]["message"]
    assert all(row["usd"] is None for row in balances["data"].values())


def test_overview_includes_unsettled_teller_funds(replay):
    replay["ws"] = {
        "data": {
            "userBalances": [
                {
                    "amount": "100000000",
                    "usd_value": 100,
                    "lastUpdatedBlock": 49819589,
                    "token_info": {"type": "sai", "name": "USDC Unsettled on Sai"},
                }
            ]
        }
    }
    balances = mcp(replay, addr=ADDR)["balances"]
    assert "error" not in balances
    assert balances["data"]["balance"]["usd"] is None
    assert balances["data"]["unsettled"]["usd"] == 100
    assert balances["data"]["unsettled"]["sourceBlock"] == 49819589
    human = cli(replay, "account", "overview", "--as", ADDR)
    assert "Unsettled Teller" in human.stdout


@pytest.mark.parametrize("lcd", ["default", "wrong_chain"])
def test_lcd_must_match_keeper_before_assigning_block_times(replay, lcd):
    populate_trades(replay)
    if lcd == "default":
        config = replay["config"]
        replay["config"] = settings.load(
            {
                "keeper.endpoint": config.endpoint,
                "keeper.ws_endpoint": config.ws_endpoint,
            }
        )
    else:
        replay["lcdChain"] = "different-chain"
    result = mcp(replay, addr=ADDR)
    balances = result["balances"]
    assert balances["data"]["balance"]["sourceBlock"] == 49819589
    assert balances["data"]["balance"]["sourceAt"] == balances["fetchedAt"]
    assert balances["data"]["balance"]["sourceAgeSeconds"] is None
    assert result["positions"]["data"]["items"][0]["riskStatus"] == "unavailable"
    calls = [args for query, args in replay["calls"] if query == "lcd"]
    assert calls == ([] if lcd == "default" else ["latest"])


def test_verified_lcd_only_assigns_times_for_its_own_chain(replay):
    chain = overview._chain_read(replay["config"])
    assert chain is not None
    assert chain.block_time(49819589) == datetime.fromisoformat(BLOCK_TIME)
    replay["lcdChain"] = "another-chain"
    assert chain.block_time(49819589) is None


def test_future_lcd_block_time_has_zero_source_age(replay):
    fetched = datetime.now(UTC)
    future = (fetched + timedelta(minutes=5)).isoformat()
    replay["riskTime"] = future
    chain = overview._chain_read(replay["config"])
    assert chain is not None
    assert overview._block_source(
        {"block": RISK_BLOCK}, fetched.isoformat(), chain.block_time
    ) == {"sourceBlock": RISK_BLOCK, "sourceAt": future, "sourceAgeSeconds": 0.0}

    membership = mcp(replay, addr=ADDR)["positions"]["membership"]
    assert membership["status"] == "stale"
    assert membership["sourceAt"] == future
    assert membership["sourceAgeSeconds"] == 0.0


@pytest.mark.parametrize("settles", [True, False])
def test_pagination_rechecks_membership_after_a_close_between_pages(replay, settles):
    template, _ = populate_trades(replay)
    scan = 0

    def response(variables):
        nonlocal scan
        offset = variables["offset"]
        if offset == 0:
            scan += 1
        # Before reading offset 100, ID 1 closes and ID 101 shifts onto page
        # one. The next scan must pick up the surviving ID 101.
        first = 1 if scan == 1 else 2 if settles else scan
        ids = list(range(first, first + 100)) if offset == 0 else []
        return {
            "data": {
                "perp": {
                    "trades": [
                        {**template, "tradeType": "limit", "id": item} for item in ids
                    ]
                }
            }
        }

    replay["http"][keeper.OVERVIEW_TRADES] = response
    result = mcp(replay, addr=ADDR)
    pending = result["pendingOrders"]
    assert scan == 3
    if settles:
        assert [row["id"] for row in pending["data"]] == list(range(2, 102))
        assert "error" not in pending
    else:
        assert "may be incomplete" in pending["error"]["message"]
        assert result["positions"]["data"]["unrealizedPnlUsd"] is None


@pytest.mark.parametrize(
    ("failed_scan", "failed_offset", "kept"),
    [(1, 100, 100), (2, 0, 101), (2, 100, 101)],
)
def test_failed_trade_page_preserves_loaded_positions(
    replay, failed_scan, failed_offset, kept
):
    template, _ = populate_trades(replay)
    replay["lcdChain"] = "different-chain"
    scan = 0

    def response(variables):
        nonlocal scan
        offset = variables["offset"]
        if offset == 0:
            scan += 1
        if scan == failed_scan and offset == failed_offset:
            return {"errors": [{"message": "page unavailable"}]}
        return {
            "data": {
                "perp": {
                    "trades": [
                        {**template, "id": index}
                        for index in range(offset + 1, min(offset + 100, 101) + 1)
                    ]
                }
            }
        }

    replay["http"][keeper.OVERVIEW_TRADES] = response
    positions = mcp(replay, addr=ADDR)["positions"]
    assert [row["id"] for row in positions["data"]["items"]] == list(range(1, kept + 1))
    assert "may be incomplete" in positions["error"]["message"]
    assert positions["data"]["unrealizedPnlUsd"] is None


@pytest.mark.parametrize(
    ("expiration", "verified"), [(16, False), (17, True), (0, True)]
)
def test_oracle_expiration_uses_the_contract_boundary(replay, expiration, verified):
    populate_trades(replay)
    replay["expiration"] = expiration
    row = mcp(replay, addr=ADDR)["positions"]["data"]["items"][0]
    assert row["markSource"]["sourceBlock"] == MARK_BLOCK
    assert row["markSource"]["queryBlock"] == RISK_BLOCK
    assert row["markSource"]["sourceAt"] == replay["markTime"]
    if verified:
        assert row["riskStatus"] == "verified"
        assert row["liquidationDistancePercent"] == 50
    else:
        assert row["riskStatus"] == "partial"
        assert "stale" in row["riskReason"]
        assert row["liquidationPriceUsd"] == 50
        assert row["liquidationDistancePercent"] is None
        assert row["unrealizedPnlUsd"] is None


def test_after_hours_adjustment_preserves_crossed_contract_threshold(replay):
    long, _ = populate_trades(replay)
    long.update(openPrice=100, leverage=10, state={"liquidationPrice": 92})
    install_contract_trade(replay, long, mark="92.5", liquidation=93)
    replay["contractTrades"][0]["needs_after_hours_trigger"] = True
    row = mcp(replay, addr=ADDR)["positions"]["data"]["items"][0]
    assert row["afterHoursLeverageAdjustmentPending"] is True
    assert row["liquidationPriceUsd"] == 93
    assert row["liquidationDistancePercent"] < 0
    assert row["unrealizedPnlUsd"] is row["unrealizedPnlPercent"] is None
    assert row["takeProfitDistancePercent"] is row["stopLossDistancePercent"] is None
    assert "keeperLiquidationEstimatePriceQuote" not in row


@pytest.mark.parametrize(
    ("is_long", "mark", "crossed"),
    [
        (True, "92.50", True),
        (True, "93", True),
        (True, "93.50", False),
        (False, "93.50", True),
        (False, "92.50", False),
    ],
)
def test_stale_mark_preserves_only_a_liquidation_crossing(
    replay, is_long, mark, crossed
):
    long, short = populate_trades(replay)
    trade = long if is_long else short
    trade.update(openPrice=100, leverage=10, state={"liquidationPrice": 92})
    install_contract_trade(replay, trade, mark=mark, liquidation=93)
    replay["expiration"] = 16
    row = mcp(replay, addr=ADDR)["positions"]["data"]["items"][trade["id"]]
    assert row["riskStatus"] == "partial"
    warning = "at or past liquidation (stale mark)"
    assert row.get("liquidationWarning") == (warning if crossed else None)
    assert row["liquidationDistancePercent"] is row["unrealizedPnlUsd"] is None
    if is_long and mark == "92.50":
        result = cli(replay, "account", "overview", "--as", ADDR)
        assert result.exit_code == 0, result.output
        assert warning in result.stdout


def test_legacy_collateral_uses_the_pinned_oracle_price_for_pnl(replay):
    long, _ = populate_trades(replay)
    long["perpBorrowing"]["collateralToken"] = {"id": 2, "symbol": "stNIBI"}
    install_contract_trade(replay, long, mark=100, liquidation=50)
    install_oracle_quote(replay, 2, 0, mark="0.10")
    quote = next(
        row
        for row in replay["http"][keeper.PRICES]["data"]["oracle"]["tokenPricesUsd"]
        if row["token"]["id"] == 2
    )
    quote["priceUsd"] = 0.20
    row = mcp(replay, addr=ADDR)["positions"]["data"]["items"][0]
    assert row["riskStatus"] == "verified"
    assert row["unrealizedPnlUsd"] == row["collateralUsd"] == 1.2
    assert row["sizeUsd"] == 4.8
    assert row["collateralPriceSource"]["sourceBlock"] == MARK_BLOCK
    assert row["collateralPriceSource"]["queryBlock"] == RISK_BLOCK


@pytest.mark.parametrize("gap", ["keeper", "contract", "index_seeding"])
def test_unconfirmed_empty_position_list_has_no_total(replay, gap):
    if gap == "keeper":
        replay["http"][keeper.INTENTS] = {
            "data": {
                "userIntents": [
                    {
                        "intentKey": "open-awaiting-index",
                        "kind": "OPEN_TRADE",
                        "lifecycleStep": "exec_fail",
                        "execActor": "server",
                        "errorCode": reconcile.OUTCOME_NOT_INDEXED,
                    }
                ]
            }
        }
    elif gap == "contract":
        replay["liveTradeIds"] = [99]
    else:
        replay["seed"] = {"seeding": {"cursor": None}}
    snapshot = mcp(replay, addr=ADDR)["positions"]
    assert snapshot["data"]["items"] == []
    assert snapshot["data"]["unrealizedPnlUsd"] is None
    assert snapshot["error"]["code"] == "INCOMPLETE_DATA"


@pytest.mark.parametrize("has_positions", [False, True])
def test_stale_live_membership_keeps_block_provenance_but_withholds_total(
    replay, has_positions
):
    old = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    replay["riskTime"] = old.isoformat()
    replay["markTime"] = (old - timedelta(seconds=17)).isoformat()
    if has_positions:
        populate_trades(replay)
    snapshot = mcp(replay, addr=ADDR)["positions"]
    membership = snapshot["membership"]
    assert membership["status"] == "stale"
    assert membership["sourceBlock"] == RISK_BLOCK
    assert membership["sourceAt"] == old.isoformat()
    assert membership["sourceAgeSeconds"] > overview.MEMBERSHIP_MAX_AGE_SECONDS
    assert snapshot["data"]["unrealizedPnlUsd"] is None
    assert "stale LCD block" in snapshot["error"]["message"]
    if has_positions:
        assert snapshot["data"]["items"][0]["unrealizedPnlUsd"] == 12
    else:
        assert snapshot["data"]["items"] == []
        result = cli(replay, "account", "overview", "--as", ADDR)
        assert result.exit_code == 0, result.output
        assert f"Live position membership stale. Block {RISK_BLOCK}" in result.stdout
        assert old.isoformat() in result.stdout


def test_slow_lcd_stops_enrichment_and_keeps_completed_sections(replay):
    _, template = populate_trades(replay)
    rows = replay["http"][keeper.OVERVIEW_TRADES]["data"]["perp"]["trades"]
    for index in range(4, 28):
        trade = {**template, "id": index}
        install_contract_trade(replay, trade, mark=10, liquidation=15)
        rows.append(trade)
    replay["stallFromTrade"] = 1
    start = time.monotonic()
    result = mcp(replay, addr=ADDR)
    assert time.monotonic() - start < overview.LCD_BUDGET_SECONDS + 5
    positions = result["positions"]
    assert positions["data"]["items"][0]["riskStatus"] == "verified"
    assert positions["data"]["items"][-1]["riskStatus"] == "unavailable"
    assert "deadline" in positions["error"]["message"]
    assert positions["data"]["unrealizedPnlUsd"] is None
    assert result["balances"]["data"]["balance"]["usd"] == 150.813685
    assert result["queuedMarketOrders"]["data"][0]["sequence"] == 400


@pytest.mark.parametrize(
    ("query", "risk_status"), [("get_trade", "unavailable"), ("get_group", "partial")]
)
def test_individual_lcd_timeout_warns_about_partial_position_risk(
    replay, monkeypatch, query, risk_status
):
    trade, _ = populate_trades(replay)
    replay["http"][keeper.OVERVIEW_TRADES]["data"]["perp"]["trades"] = [trade]
    replay["liveTradeIds"] = [trade["id"]]
    replay["dripQuery"] = query
    monkeypatch.setattr(overview, "LCD_REQUEST_SECONDS", 0.8)

    positions = mcp(replay, addr=ADDR)["positions"]
    assert replay["lcdDisconnected"].wait(1)
    assert positions["membership"]["status"] == "verified"
    assert positions["data"]["items"][0]["riskStatus"] == risk_status
    assert positions["data"]["unrealizedPnlUsd"] is None
    assert positions["error"]["code"] == "INCOMPLETE_DATA"
    assert "deadline" in positions["error"]["message"]


@pytest.mark.parametrize("surface", ["cli", "mcp"])
@pytest.mark.parametrize(
    ("drip_path", "budget", "request_timeout"),
    [("/smart/", 1.5, 5.0), ("/latest", 5.0, 1.5)],
    ids=["overview-deadline", "request-timeout"],
)
def test_trickling_lcd_disconnects_before_the_next_overview(
    replay, monkeypatch, surface, drip_path, budget, request_timeout
):
    monkeypatch.setattr(overview, "LCD_BUDGET_SECONDS", budget)
    monkeypatch.setattr(overview, "LCD_REQUEST_SECONDS", request_timeout)
    replay["dripPath"] = drip_path

    def check_recovery(invoke: Callable[[], dict[str, Any]]) -> None:
        first = invoke()
        assert first["positions"]["membership"]["status"] == "unavailable"
        assert replay["lcdDisconnected"].wait(1), "LCD socket stayed open"
        assert replay["lcdDripSeconds"] < min(budget, request_timeout) + 1.0
        second = invoke()
        assert second["positions"]["membership"]["status"] == "verified"
        assert second["positions"]["data"]["unrealizedPnlUsd"] == 0

    if surface == "cli":

        def invoke_cli() -> dict[str, Any]:
            result = cli(replay, "account", "overview", "--as", ADDR, "--json")
            assert result.exit_code == 0, result.output
            return json.loads(result.stdout)

        check_recovery(invoke_cli)
    else:
        server = create_server(replay["config"])
        with asyncio.Runner() as runner:
            check_recovery(
                lambda: decode_mcp(
                    runner.run(server.call_tool("account_overview", {"addr": ADDR}))
                )
            )


def test_overview_bounds_stalled_lcd_resolution(replay, monkeypatch):
    config = replay["config"]
    server = create_server(
        replace(
            config, lcd_endpoint=config.lcd_endpoint.replace("127.0.0.1", "localhost")
        )
    )
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(overview, "LCD_REQUEST_SECONDS", 0.6)
    resume = threading.Event()
    resolvers: list[threading.Thread] = []
    getaddrinfo = socket.getaddrinfo

    def stalled_resolution(host, *args, **kwargs):
        if host in ("localhost", b"localhost") and not resume.is_set():
            resolvers.append(threading.current_thread())
            resume.wait()
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", stalled_resolution)
    with asyncio.Runner() as runner:
        try:
            for _ in range(10):
                result = decode_mcp(
                    runner.run(server.call_tool("account_overview", {"addr": ADDR}))
                )
                assert result["positions"]["membership"]["status"] == "unavailable"
            assert len(resolvers) == 8
        finally:
            resume.set()
            for worker in resolvers:
                worker.join(timeout=2)
        assert all(not worker.is_alive() for worker in resolvers)
        result = decode_mcp(
            runner.run(server.call_tool("account_overview", {"addr": ADDR}))
        )
        assert result["positions"]["membership"]["status"] == "verified"


def test_overview_rejects_an_invalid_lcd_port():
    result = CliRunner().invoke(
        main,
        [
            "--json",
            "--lcd-endpoint",
            "https://lcd.example.invalid:bad",
            "account",
            "overview",
            "--as",
            ADDR,
        ],
    )
    assert result.exit_code == 2, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert "--lcd-endpoint: Invalid value for lcd.endpoint" in error["message"]


def test_balance_source_label_warns_about_an_independent_websocket(replay):
    balances = mcp(replay, addr=ADDR)["balances"]
    assert balances["source"] == {
        "endpoint": replay["config"].ws_endpoint.removesuffix("/query"),
        "matchesKeeper": False,
    }
    assert "different chain" in balances["warning"]
    home = cli(replay, "account", "overview", "--as", ADDR)
    assert home.exit_code == 0, home.output
    assert "Balance WebSocket is independent" in home.stdout
    assert balances["source"]["endpoint"] in home.stdout


def test_next_oracle_pending_intent_remains_a_local_trade_blocker(replay):
    endpoint = replay["config"].endpoint
    storage.pending("pending-on-chain", endpoint, ADDR, "OPEN_TRADE", "")
    storage.set_status("pending-on-chain", endpoint, ADDR, "pending")
    result = mcp(replay, addr=ADDR)["blockingIntents"]
    assert [(row["status"], row["scope"]) for row in result["data"]] == [
        ("pending", "allTrades")
    ]
