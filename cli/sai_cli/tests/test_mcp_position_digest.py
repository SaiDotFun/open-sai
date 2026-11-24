"""Confirm position quotes through MCP against loopback Keeper and LCD routes."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote

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
            if self.path == "/cosmos/base/tendermint/v1beta1/node_info":
                self.reply({"default_node_info": {"network": CHAIN_ID}})
                return
            prefix = "/cosmwasm/wasm/v1/contract/nibi1contract/smart/"
            assert self.path.startswith(prefix)
            message = json.loads(base64.b64decode(unquote(self.path[len(prefix) :])))
            if "get_trade_raw" in message:
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
            else:
                assert "get_exchange_balance" in message
                data = {
                    "collateral_index": "TokenIndex(1)",
                    "available_to_trade": "10000000",
                }
            self.reply({"data": data})

        def reply(self, value: dict[str, Any]) -> None:
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
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
    [(4_000_000, "2.5", "5.0"), (4_000_001, "2", "4")],
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
            assert fresh["quote"] == {
                "percent": 50,
                "collateralBefore": str(collateral),
                "leverageBefore": leverage,
                "closedNotionalUsd": closed_notional,
            }
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
            }

    asyncio.run(scenario())
