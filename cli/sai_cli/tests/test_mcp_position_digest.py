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
from urllib.parse import unquote

import pytest
from click.testing import CliRunner
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent
from sai_cli.core import keeper, settings, storage
from sai_cli.mcp_server.server import create_server
from sai_cli.sai_cli import main

ADDR = "0x" + "a" * 40
TOKEN = "fake-token"
CHAIN_ID = "test-1"
TRADER = "nibi1trader"


@dataclass
class Position:
    collateral: int = 4_000_000
    trade_type: str = "trade"
    leverage: str = "2"
    snapshot_leverages: deque[str] = field(default_factory=deque)
    after_hours: bool = False
    risk_block: int = 90
    cap: str | None = "5"
    caps_on_position_read: deque[str | None] = field(default_factory=deque)
    reported_height: int | None = 120
    cap_query_heights: list[str | None] = field(default_factory=list)
    collateral_price: str | None = "1"
    preview_inputs: bool = False
    malformed_node_info: bool = False
    price: float | str = 100.5
    oracle_position_updates: deque[tuple[int, str] | None] = field(
        default_factory=deque
    )
    submitted: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def position_service() -> Iterator[tuple[Position, str]]:
    position = Position()
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
                market_row = market
                if position.preview_inputs:
                    market_row = {
                        **market,
                        "price": position.price,
                        "openFeePct": 0.001,
                        "closeFeePct": 0.002,
                        "triggerOrderFeePct": 0.003,
                    }
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
                    keeper.MARKET: {"perp": {"borrowing": market_row}},
                    keeper.PREVIEW_FEES: {"perp": {"borrowing": market_row}},
                    keeper.MARKETS: {"perp": {"borrowings": [market_row]}},
                }[request["query"]]
            self.reply({"data": data})

        def do_GET(self) -> None:
            if self.path == "/cosmos/base/tendermint/v1beta1/node_info":
                self.reply(
                    {
                        "default_node_info": None
                        if position.malformed_node_info
                        else {"network": CHAIN_ID}
                    }
                )
                return
            prefix = "/cosmwasm/wasm/v1/contract/nibi1contract/smart/"
            assert self.path.startswith(prefix)
            message = json.loads(base64.b64decode(unquote(self.path[len(prefix) :])))
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
                    "trade_type": position.trade_type,
                    "collateral_amount": str(position.collateral),
                    "leverage": position.leverage,
                }
                if position.preview_inputs:
                    data.update(open_price="100", long=True)
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
            self.reply({"data": data}, position.reported_height)

        def reply(self, value: dict[str, Any], height: int | None = None) -> None:
            requested_height = self.headers.get("x-cosmos-block-height")
            if requested_height is not None:
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


def test_cli_preview_text_and_json_use_the_order_trigger(
    position_service: tuple[Position, str], tmp_path, monkeypatch
) -> None:
    position, base_url = position_service
    position.preview_inputs = True
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    args = [
        "--endpoint",
        f"{base_url}/query",
        "--lcd-endpoint",
        base_url,
        "trade",
        "open",
        "--market",
        "0",
        "--side",
        "long",
        "--type",
        "limit",
        "--amount",
        "10",
        "--leverage",
        "2",
        "--price",
        "100",
        "--slippage",
        "1",
        "--dry-run",
    ]
    runner = CliRunner()
    result = runner.invoke(main, args)
    assert result.exit_code == 0, result.output
    assert '"worstCaseFill": {' in result.output
    assert '"amountUsdc": "0.08"' in result.output

    result = runner.invoke(main, ["--json", *args])
    assert result.exit_code == 0, result.output
    preview = json.loads(result.output)
    assert preview["addr"] is None
    estimates = preview["estimates"]
    assert estimates["worstCaseFill"] == {
        "status": "bound",
        "price": "101",
        "side": "maximum",
        "reference": {"price": "100", "source": "Order trigger", "observedAt": None},
    }
    fees = estimates["fees"]
    assert fees["amountUsdc"] == "0.08"
    assert fees["status"] == "estimate"
    assert fees["collateralPriceUsd"]["price"] == "1"
    assert fees["collateralPriceUsd"]["observedAt"].endswith("Z")
    assert Decimal(estimates["liquidationPrice"]["price"]).quantize(
        Decimal(".0001")
    ) == Decimal(60)
    assert position.submitted == []


@pytest.mark.parametrize(
    ("side", "worst"), [("long", "102.52005"), ("short", "98.50005")]
)
def test_mcp_market_bound_and_moved_price_preview(
    position_service: tuple[Position, str], side: str, worst: str
) -> None:
    position, base_url = position_service
    position.preview_inputs = True
    args = {
        "market_id": 0,
        "side": side,
        "order_type": "market",
        "amount": "10",
        "leverage": "2",
        "slippage": "1",
    }

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_open", args)
            fill = preview["estimates"]["worstCaseFill"]
            assert fill["price"] == worst
            assert fill["status"] == "bound"
            assert fill["reference"]["price"] == preview["referencePrice"] == "100.5"
            assert fill["reference"]["source"] == "Sai Keeper market price"
            assert fill["reference"]["observedAt"].endswith("Z")
            assert preview["estimates"]["fees"]["amountUsdc"] == "0.02"
            position.price = 110
            moved = await call(
                client,
                "trade_open",
                {
                    **args,
                    "preview_digest": preview["previewDigest"],
                    "reference_price": preview["referencePrice"],
                },
            )
            assert moved["code"] == "PRICE_MOVED"
            assert (
                moved["preview"]["estimates"]["worstCaseFill"]["reference"]["price"]
                == "110"
            )
            assert position.submitted == []

    asyncio.run(scenario())


def test_mcp_stop_has_no_bound_and_unavailable_oracle_keeps_preview(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral_price = None

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(
                client,
                "preview_open",
                {
                    "market_id": 0,
                    "side": "short",
                    "order_type": "stop",
                    "price": "100",
                    "amount": "10",
                    "leverage": "2",
                    "slippage": "1",
                },
            )
            estimates = preview["estimates"]
            assert estimates["worstCaseFill"]["status"] == "unbounded"
            assert "price" not in estimates["worstCaseFill"]
            assert estimates["fees"]["status"] == "unavailable"
            assert estimates["liquidationPrice"]["status"] == "unavailable"
            assert len(preview["previewDigest"]) == 64
            assert position.submitted == []

    asyncio.run(scenario())


def test_mcp_position_estimates_do_not_change_confirmed_terms(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    position.preview_inputs = True

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            variants = (
                ("close", {}, "0.016", "unbounded", None),
                ("partial_close", {"percent": 50}, "0.008", "unbounded", "60"),
                ("leverage", {"leverage": "4"}, "0", "not_applicable", "80"),
                (
                    "margin",
                    {"direction": "add", "amount": "4"},
                    "0",
                    "not_applicable",
                    "20",
                ),
                ("tpsl", {"take_profit": "120"}, "0", "not_applicable", None),
            )
            for tool, args, fee, fill, liq in variants:
                preview = await call(client, f"preview_{tool}", {"trade_id": 7, **args})
                estimates = preview["estimates"]
                assert estimates["fees"].get("amountUsdc") == fee
                assert estimates["worstCaseFill"]["status"] == fill
                quote = estimates["liquidationPrice"]
                if liq is None:
                    assert quote["status"] in {"unchanged", "not_applicable"}
                else:
                    assert quote["status"] == "estimate"
                    assert Decimal(quote["price"]).quantize(
                        Decimal(".0001")
                    ) == Decimal(liq)
                    assert quote["entry"]["observedAt"].endswith("Z")
            assert position.submitted == []

            preview = await call(client, "preview_close", {"trade_id": 7})
            position.collateral_price = None
            fresh = await call(client, "preview_close", {"trade_id": 7})
            assert fresh["estimates"]["fees"]["status"] == "unavailable"
            assert fresh["previewDigest"] == preview["previewDigest"]
            accepted = await call(
                client,
                "trade_close",
                {
                    "trade_id": 7,
                    "preview_digest": preview["previewDigest"],
                },
            )
            assert accepted["status"] == "accepted"
            assert len(position.submitted) == 1

    asyncio.run(scenario())


def test_malformed_lcd_keeps_the_limit_preview_and_digest_refusal(position_service):
    position, base_url = position_service
    position.preview_inputs = True
    position.malformed_node_info = True
    args = {
        "market_id": 0,
        "side": "long",
        "order_type": "limit",
        "price": "100",
        "amount": "10",
        "leverage": "2",
        "slippage": "1",
    }

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(client, "preview_open", args)
            assert quote["estimates"]["fees"]["status"] == "unavailable"
            refusal = await call(
                client,
                "trade_open",
                {
                    **args,
                    "amount": "11",
                    "preview_digest": quote["previewDigest"],
                },
            )
            assert refusal["code"] == "PREVIEW_MISMATCH"
            fresh = refusal["preview"]
            assert fresh["estimates"]["fees"]["status"] == "unavailable"
            assert fresh["previewDigest"] != quote["previewDigest"]
            assert (
                fresh["previewDigest"]
                == (await call(client, "preview_open", {**args, "amount": "11"}))[
                    "previewDigest"
                ]
            )
            assert position.submitted == []

    asyncio.run(scenario())


def test_mcp_market_refuses_first_step_excess_of_one_price_atom(position_service):
    position, base_url = position_service
    position.preview_inputs = True
    position.price = "100.00000000000001"
    args = {
        "market_id": 0,
        "side": "long",
        "order_type": "market",
        "amount": "10",
        "leverage": "2",
        "slippage": "0.9999999999999999",
    }

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(client, "preview_open", args)
            assert (
                quote["estimates"]["worstCaseFill"]["price"] == "102.010000000000009997"
            )
            trade = {
                **args,
                "preview_digest": quote["previewDigest"],
                "reference_price": quote["referencePrice"],
            }
            position.price = "101.00000000000001"
            result = await call(client, "trade_open", trade)
            assert result["code"] == "PRICE_MOVED"
            assert position.submitted == []
            position.price = "101.000000000000009999"
            result = await call(client, "trade_open", trade)
            assert result["status"] == "accepted"
            assert len(position.submitted) == 1

    asyncio.run(scenario())


def test_cancel_checks_live_order_type_and_refuses_after_fill(position_service):
    position, base_url = position_service
    args = {"trade_id": 7, "cancel_order": True}

    async def scenario():
        async with connected_client(base_url) as client:
            filled = await call(client, "preview_close", args)
            assert filled["code"] == "INVALID_REQUEST"
            assert "pending order" in filled["error"]
            assert "estimates" not in filled
            position.trade_type = "limit"
            quote = await call(client, "preview_close", args)
            assert quote["estimates"]["fees"]["status"] == "unavailable"
            assert "market" in quote["estimates"]["fees"]["reason"]
            trade = {**args, "preview_digest": quote["previewDigest"]}
            position.trade_type = "trade"
            result = await call(client, "trade_close", trade)
            assert result["code"] == "INVALID_REQUEST"
            assert position.submitted == []
            position.trade_type = "limit"
            assert (await call(client, "trade_close", trade))["status"] == "accepted"
            assert position.submitted[0]["kind"] == "CANCEL_PENDING_TRADE"

    asyncio.run(scenario())
