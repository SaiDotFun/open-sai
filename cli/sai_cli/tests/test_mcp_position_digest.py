"""Confirm position quotes through MCP against loopback Keeper and LCD routes."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from collections import deque
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent
from sai_cli.core import keeper, settings, storage
from sai_cli.mcp_server.server import create_server

ADDR = "0x" + "a" * 40
TOKEN = "fake-token"
CHAIN_ID = "test-1"
TRADER = "nibi1trader"


@dataclass
class Position:
    collateral: int = 4_000_000
    leverage: str = "2"
    snapshot_leverages: deque[str] = field(default_factory=deque)
    after_hours: bool = False
    risk_block: int = 90
    cap: str | None = "5"
    caps_on_position_read: deque[str | None] = field(default_factory=deque)
    reported_height: int | None = 120
    cache_contract_queries: bool = False
    drop_pinned_height: bool = False
    cap_query_heights: list[str | None] = field(default_factory=list)
    collateral_price: str | None = "1"
    oracle_position_updates: deque[tuple[int, str] | None] = field(
        default_factory=deque
    )
    submitted: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def position_service() -> Iterator[tuple[Position, str]]:
    position = Position()
    cached_replies: dict[str, Any] = {}
    market = {
        "marketId": 0,
        "isOpen": True,
        "minLeverage": 1,
        "maxLeverage": 100,
        "minPositionSizeUSD": 1,
        "collateralToken": {"id": 1, "symbol": "USDC"},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            assert self.path == "/query"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if request["query"] == keeper.POSITION and position.snapshot_leverages:
                position.leverage = position.snapshot_leverages.popleft()
            if request["query"] == keeper.SUBMIT:
                assert self.headers["Authorization"] == f"Bearer {TOKEN}"
                envelope = request["variables"]["input"]
                position.submitted.append(envelope["intent"])
                data = {
                    "recordUserIntent": {
                        "intentKey": envelope["intentKey"],
                        "lifecycleStep": "jwt_pass",
                        "execActor": "server",
                        "evmAddress": ADDR,
                    }
                }
            else:
                data = {
                    keeper.ROUTE: {
                        "oneClickTrading": {
                            "enabled": True,
                            "chainId": CHAIN_ID,
                            "contractAddress": "nibi1contract",
                        }
                    },
                    keeper.POSITION: {
                        "perp": {
                            "trade": {
                                "trader": TRADER,
                                "isOpen": True,
                                "tradeType": "trade",
                                "collateralAmount": position.collateral,
                                "leverage": float(position.leverage),
                                "perpBorrowing": market,
                            }
                        }
                    },
                    keeper.MARKET: {"perp": {"borrowing": market}},
                    keeper.MARKETS: {"perp": {"borrowings": [market]}},
                }[request["query"]]
            self.reply({"data": data})

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/cosmos/base/tendermint/v1beta1/node_info":
                self.reply({"default_node_info": {"network": CHAIN_ID}})
                return
            prefix = "/cosmwasm/wasm/v1/contract/nibi1contract/smart/"
            assert path.startswith(prefix)
            if position.cache_contract_queries and self.path in cached_replies:
                self.reply({"data": cached_replies[self.path]})
                return
            message = json.loads(base64.b64decode(unquote(path[len(prefix) :])))
            if "get_trade_raw" in message:
                if position.caps_on_position_read:
                    cap = position.caps_on_position_read.popleft()
                    if cap is not None:
                        position.cap = cap
                        if position.reported_height is not None:
                            position.reported_height += 1
                data = {
                    "user": TRADER,
                    "user_trade_index": "UserTradeIndex(7)",
                    "market_index": "MarketIndex(0)",
                    "collateral_index": "TokenIndex(1)",
                    "is_open": True,
                    "trade_type": "trade",
                    "collateral_amount": str(position.collateral),
                    "leverage": position.leverage,
                }
            elif "get_exchange_balance" in message:
                data = {
                    "collateral_index": "TokenIndex(1)",
                    "available_to_trade": "10000000",
                }
            elif "is_after_hours_market" in message:
                assert message["is_after_hours_market"] == {
                    "market_index": "MarketIndex(0)"
                }
                data = position.after_hours
            elif "get_market" in message:
                assert message["get_market"] == {"index": "MarketIndex(0)"}
                data = {"block_last_opened": 110, "block_last_closed": 100}
            elif "get_trade_infos" in message:
                assert message["get_trade_infos"] == {
                    "trader": TRADER,
                    "start_idx": 7,
                    "scan_limit": 1,
                }
                data = [{"created_block": position.risk_block}]
            elif "get_after_hours_leverage" in message:
                position.cap_query_heights.append(
                    self.headers.get("x-cosmos-block-height")
                )
                data = position.cap
            else:
                assert message == {
                    "get_collateral_price": {"collateral_index": "TokenIndex(1)"}
                }
                data = position.collateral_price
                if position.oracle_position_updates:
                    update = position.oracle_position_updates.popleft()
                    if update is not None:
                        position.collateral, position.leverage = update
            if data is None:
                self.send_error(400, "contract query unavailable")
                return
            if position.cache_contract_queries:
                cached_replies[self.path] = data
            height = position.reported_height
            if position.drop_pinned_height and "x-cosmos-block-height" in self.headers:
                height = None
            self.reply({"data": data}, height)

        def reply(self, value: dict[str, Any], height: int | None = None) -> None:
            requested_height = self.headers.get("x-cosmos-block-height")
            if requested_height is not None and height is not None:
                assert requested_height == str(height)
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if height is not None:
                self.send_header("grpc-metadata-x-cosmos-block-height", str(height))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as httpd:
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        thread.start()
        try:
            yield position, f"http://127.0.0.1:{httpd.server_port}"
        finally:
            httpd.shutdown()
            thread.join()


@asynccontextmanager
async def connected_client(base_url: str) -> AsyncGenerator[ClientSession]:
    endpoint = f"{base_url}/query"
    storage.save_session(
        {
            "token": TOKEN,
            "evmAddress": ADDR,
            "endpoint": endpoint,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
    )
    config = settings.load(
        flags={"keeper.endpoint": endpoint, "lcd.endpoint": base_url}
    )
    mcp = create_server(config)
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        yield client


async def call(
    client: ClientSession, tool: str, args: dict[str, Any]
) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    assert not result.isError, result.content
    block = result.content[0]
    assert isinstance(block, TextContent)
    return json.loads(block.text)


@pytest.mark.parametrize(
    ("collateral", "leverage", "closed_notional"),
    [(4_000_000, "2.5", "5"), (4_000_001, "2", "4")],
)
def test_partial_close_requires_confirmation_when_position_changes(
    position_service: tuple[Position, str],
    collateral: int,
    leverage: str,
    closed_notional: str,
) -> None:
    position, base_url = position_service
    args = {"trade_id": 7, "percent": 50}

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_partial_close", args)
            trade = {**args, "preview_digest": preview["previewDigest"]}
            position.collateral = collateral
            position.leverage = leverage
            result = await call(client, "trade_partial_close", trade)

            assert result.get("code") == "PREVIEW_MISMATCH", result
            assert position.submitted == []
            fresh = result["preview"]
            assert fresh["previewDigest"] != preview["previewDigest"]
            assert fresh["intent"] == preview["intent"]
            assert preview["quote"]["closedNotionalUsd"] == "4"
            assert fresh["quote"]["percent"] == 50
            assert fresh["quote"]["collateralBefore"] == str(collateral)
            assert fresh["quote"]["leverageBefore"] == leverage
            assert fresh["quote"]["closedNotionalUsd"] == closed_notional
            assert fresh == await call(client, "preview_partial_close", args)

            position.collateral = 4_000_000
            position.leverage = "2"
            accepted = await call(client, "trade_partial_close", trade)
            assert accepted["status"] == "accepted"
            (submitted,) = position.submitted
            assert {key: submitted[key] for key in preview["intent"]} == preview[
                "intent"
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("partial_close", {"trade_id": 7, "percent": 50}),
        ("margin", {"trade_id": 7, "direction": "add", "amount": "1"}),
    ],
)
@pytest.mark.parametrize("live_leverages", [("2", "2.5"), ("2.5", "2")])
def test_trade_refuses_a_change_between_live_reads(
    position_service: tuple[Position, str],
    tool: str,
    args: dict[str, Any],
    live_leverages: tuple[str, str],
) -> None:
    position, base_url = position_service

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            confirmed = await call(client, f"preview_{tool}", args)
            position.snapshot_leverages.extend(live_leverages)
            result = await call(
                client,
                f"trade_{tool}",
                {**args, "preview_digest": confirmed["previewDigest"]},
            )
            assert result.get("code") == "PREVIEW_MISMATCH", result
            assert position.submitted == []
            assert result["preview"]["quote"]["leverageBefore"] == live_leverages[-1]
            assert result["preview"] == await call(client, f"preview_{tool}", args)

            accepted = await call(
                client,
                f"trade_{tool}",
                {**args, "preview_digest": result["preview"]["previewDigest"]},
            )
            assert accepted["status"] == "accepted"
            assert len(position.submitted) == 1

    asyncio.run(scenario())


def test_partial_close_rechecks_position_after_the_last_oracle_read(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    args = {"trade_id": 7, "percent": 50}

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_partial_close", args)
            assert preview["intent"]["body"]["collateral"]["amountUsdc"] == "2000000"
            position.oracle_position_updates.extend([None, (3_200_000, "2.5")])
            result = await call(
                client,
                "trade_partial_close",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert result.get("code") == "PREVIEW_MISMATCH", result
            assert position.submitted == []
            fresh = result["preview"]
            assert fresh["previewDigest"] != preview["previewDigest"]
            assert fresh["intent"]["body"]["collateral"]["amountUsdc"] == "1600000"
            assert fresh == await call(client, "preview_partial_close", args)

            accepted = await call(
                client,
                "trade_partial_close",
                {**args, "preview_digest": fresh["previewDigest"]},
            )
            assert accepted["status"] == "accepted"
            (submitted,) = position.submitted
            assert submitted["body"]["collateral"]["amountUsdc"] == "1600000"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("partial_close", {"trade_id": 7, "percent": 50}),
        ("margin", {"trade_id": 7, "direction": "add", "amount": "5"}),
    ],
)
@pytest.mark.parametrize("reported_height", [120, None])
def test_trade_rechecks_cap_after_reading_unchanged_position(
    position_service: tuple[Position, str],
    tool: str,
    args: dict[str, Any],
    reported_height: int | None,
) -> None:
    position, base_url = position_service
    position.collateral = 100_000_000
    position.leverage = "10"
    position.after_hours = True
    position.reported_height = reported_height

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, f"preview_{tool}", args)
            assert preview["quote"]["afterHoursLeverageCap"] == "5"
            # Both rebuilt quotes see 5x. The cap changes as the final
            # contract position read starts, with the stored trade intact.
            position.caps_on_position_read.extend([None, None, "4"])
            result = await call(
                client,
                f"trade_{tool}",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert result.get("code") == "PREVIEW_MISMATCH", result
            assert position.submitted == []
            assert position.cap_query_heights == [
                None,
                None,
                None,
                "121" if reported_height is not None else None,
                None,
            ]
            fresh = result["preview"]
            assert fresh["previewDigest"] != preview["previewDigest"]
            assert (
                fresh["quote"]["effectiveLeverage"]
                == fresh["quote"]["afterHoursLeverageCap"]
                == "4"
            )
            if tool == "margin":
                assert Decimal(preview["quote"]["requestedLeverage"]) > 4
                assert Decimal(fresh["quote"]["requestedLeverage"]) < 4
            else:
                assert preview["quote"]["closedNotionalUsd"] == "250"
                assert fresh["quote"]["closedNotionalUsd"] == "200"
                assert fresh["intent"] == preview["intent"]
            assert fresh == await call(client, f"preview_{tool}", args)
            accepted = await call(
                client,
                f"trade_{tool}",
                {**args, "preview_digest": fresh["previewDigest"]},
            )
            if reported_height is None:
                assert accepted.get("code") == "INVALID_REQUEST", accepted
                assert "same block" in accepted["error"]
                assert position.submitted == []
            else:
                assert accepted["status"] == "accepted"
                assert len(position.submitted) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("partial_close", {"trade_id": 7, "percent": 50}),
        ("margin", {"trade_id": 7, "direction": "add", "amount": "1"}),
    ],
)
def test_trade_confirms_through_lcd_cache_and_refuses_unverified_heights(
    position_service: tuple[Position, str], tool: str, args: dict[str, Any]
) -> None:
    position, base_url = position_service
    position.cache_contract_queries = True

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, f"preview_{tool}", args)
            trade = {**args, "preview_digest": preview["previewDigest"]}
            position.drop_pinned_height = True
            rejected = await call(client, f"trade_{tool}", trade)
            assert rejected.get("code") == "PREVIEW_MISMATCH", rejected
            assert "Retry" in rejected["error"]
            assert position.submitted == []

            position.drop_pinned_height = False
            accepted = await call(client, f"trade_{tool}", trade)
            assert accepted["status"] == "accepted", accepted
            assert len(position.submitted) == 1

    asyncio.run(scenario())


def test_margin_add_uses_carried_positions_effective_leverage(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    position.collateral = 100_000_000
    position.leverage = "10"
    position.after_hours = True
    args = {"trade_id": 7, "direction": "add", "amount": "5"}

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_margin", args)
            target = preview["intent"]["body"]["requestedLeverage"]
            assert Decimal(target) < Decimal(5)
            assert target == "4.761904761904761905"
            quote = preview["quote"]
            assert quote["leverageBefore"] == "10"
            assert quote["effectiveLeverage"] == quote["afterHoursLeverageCap"] == "5"
            assert quote["marginChangeIsEstimate"] is True

            position.cap = "4"
            refused = await call(
                client,
                "trade_margin",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert refused.get("code") == "PREVIEW_MISMATCH", refused
            assert position.submitted == []
            fresh = refused["preview"]
            assert fresh["quote"]["effectiveLeverage"] == "4"
            assert Decimal(fresh["intent"]["body"]["requestedLeverage"]) < Decimal(4)
            accepted = await call(
                client,
                "trade_margin",
                {**args, "preview_digest": fresh["previewDigest"]},
            )
            assert accepted["status"] == "accepted"
            (submitted,) = position.submitted
            assert submitted["body"] == fresh["intent"]["body"]

    asyncio.run(scenario())


@pytest.mark.parametrize("cap", [None, "0"])
def test_margin_refuses_when_the_guarded_leverage_cannot_size_a_change(
    position_service: tuple[Position, str], cap: str | None
) -> None:
    position, base_url = position_service
    position.leverage = "10"
    position.after_hours = True
    position.cap = cap

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            args = {"trade_id": 7, "direction": "add", "amount": "1"}
            preview = await call(client, "preview_margin", args)
            assert preview.get("code") == "INVALID_REQUEST", preview
            assert "effective leverage" in preview["error"].lower()
            result = await call(
                client, "trade_margin", {**args, "preview_digest": "0" * 64}
            )
            assert result.get("code") == "INVALID_REQUEST", result
            assert result["error"] == preview["error"]
            assert position.submitted == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "changed", "fresh_usd"),
    [("cap", "0", "0"), ("collateral_price", "1.000000000000000001", "250.000009")],
)
def test_partial_close_binds_cap_and_oracle_with_contract_rounding(
    position_service: tuple[Position, str], field: str, changed: str, fresh_usd: str
) -> None:
    position, base_url = position_service
    position.collateral = 100_000_002
    position.leverage = "10"
    position.after_hours = True
    position.cap = "5.00000009"
    position.collateral_price = "1.000000003"
    args = {"trade_id": 7, "percent": 50}

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_partial_close", args)
            assert preview["intent"]["body"]["collateral"]["amountUsdc"] == "50000001"
            quote = preview["quote"]
            assert quote["effectiveLeverage"] == "5.00000009"
            assert quote["collateralPriceUsd"] == "1.000000003"
            # floor(50_000_001 * 5.00000009) is 250_000_009.
            # Flooring that times the price leaves 250_000_009. One floor
            # after both multiplications would give 250_000_010.
            assert (
                quote["closedNotionalUsdc"]
                == quote["closedNotionalUsd"]
                == "250.000009"
            )
            assert quote["closedNotionalIsEstimate"] is False
            setattr(position, field, changed)
            result = await call(
                client,
                "trade_partial_close",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert result.get("code") == "PREVIEW_MISMATCH", result
            assert position.submitted == []
            assert result["preview"]["intent"] == preview["intent"]
            assert result["preview"]["quote"]["closedNotionalUsd"] == fresh_usd
            assert result["preview"]["quote"]["closedNotionalIsEstimate"] is False
            assert result["preview"] == await call(
                client, "preview_partial_close", args
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("missing", "expected_leverage", "expected_usd"),
    [("cap", None, "20"), ("collateral_price", "5", None)],
)
def test_partial_close_marks_incomplete_quotes_as_estimates(
    position_service: tuple[Position, str],
    missing: str,
    expected_leverage: str | None,
    expected_usd: str | None,
) -> None:
    position, base_url = position_service
    position.leverage = "10"
    position.after_hours = True
    setattr(position, missing, None)

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(
                client, "preview_partial_close", {"trade_id": 7, "percent": 50}
            )
            quote = preview["quote"]
            assert quote["effectiveLeverage"] == expected_leverage
            assert quote["closedNotionalUsd"] == expected_usd
            assert quote["closedNotionalUsdc"] == ("20" if missing == "cap" else "10")
            assert quote["closedNotionalIsEstimate"] is True

    asyncio.run(scenario())


def test_partial_close_keeps_leverage_for_risk_updated_position(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    position.after_hours = True
    position.leverage = "10"
    position.risk_block = 110
    position.cap = None

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(
                client, "preview_partial_close", {"trade_id": 7, "percent": 50}
            )
            quote = preview["quote"]
            assert quote["effectiveLeverage"] == "10"
            assert quote["closedNotionalUsd"] == "20"
            assert quote["closedNotionalIsEstimate"] is False

    asyncio.run(scenario())


def test_margin_binds_starting_leverage_even_when_the_result_rounds_the_same(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    position.collateral = 2_000_000
    position.leverage = "3.000000000000000001"
    args = {"trade_id": 7, "direction": "add", "amount": "3"}

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_margin", args)
            position.leverage = "3.000000000000000002"
            result = await call(
                client,
                "trade_margin",
                {**args, "preview_digest": preview["previewDigest"]},
            )

            assert result.get("code") == "PREVIEW_MISMATCH", result
            assert position.submitted == []
            fresh = result["preview"]
            assert fresh["previewDigest"] != preview["previewDigest"]
            assert fresh["intent"] == preview["intent"]
            assert fresh["quote"] == {
                **preview["quote"],
                "leverageBefore": position.leverage,
                "effectiveLeverage": position.leverage,
            }

    asyncio.run(scenario())
