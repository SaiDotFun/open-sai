"""Confirm position quotes through CLI and MCP against loopback Keeper and LCD."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
import traceback
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
from click.testing import CliRunner
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent
from sai_cli.core import confirmation, keeper, margin, settings, storage
from sai_cli.core.errors import ExitCode
from sai_cli.mcp_server.server import create_server
from sai_cli.sai_cli import main

ADDR = "0x" + "a" * 40
CHECKSUM_ADDR = "0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa"
TOKEN = "fake-token"
CHAIN_ID = "test-1"
TRADER = "nibi1424242424242424242424242424242422mldj0"
OTHER_TRADER = "nibi1hwamhwamhwamhwamhwamhwamhwamhwamc9gs8a"
CLI_POSITION_ACTIONS = (
    ("close", "7"),
    ("leverage", "7", "--leverage", "3"),
    ("tpsl", "7", "--take-profit", "120"),
    ("margin", "7", "--direction", "add", "--amount", "1"),
    ("partial-close", "7", "--percent", "50"),
)
MCP_POSITION_ACTIONS = (
    ("close", {"trade_id": 7}),
    ("leverage", {"trade_id": 7, "leverage": "3"}),
    ("tpsl", {"trade_id": 7, "take_profit": "120"}),
)


@dataclass
class Position:
    chain_id: str = CHAIN_ID
    contract_address: str = "nibi1contract"
    lcd_chain_id: str | None = None
    lcd_chain_ids: deque[str] = field(default_factory=deque)
    collateral: int = 4_000_000
    trader: str = TRADER
    contract_owner: str | None = TRADER
    contract_position_unavailable: bool = False
    position_reads: int = 0
    trade_type: str = "trade"
    keeper_market_open: bool = True
    market_trading_state: str = "open"
    switch_and_logout_on_keeper_read: str | None = None
    logout_on_position_read: bool = False
    missing_trade_status: int = 400
    leverage: str = "2"
    max_leverage: int = 100
    quoted_max_leverage: int | None = None
    pair_leverage: str | None = "0"
    liquidation_threshold: str = "60"
    block_timestamp: int = 2_000_000_000
    oracle_base_age: int | None = None
    null_market_price: bool = False
    oracle_quote_age: int | None = None
    oracle_ttl: int = 300
    fill_at_next_oracle_price: bool = False
    snapshot_leverages: deque[str] = field(default_factory=deque)
    position_read_states: deque[bool] = field(default_factory=deque)
    snapshot_traders: deque[str] = field(default_factory=deque)
    indexed_collaterals: deque[int] = field(default_factory=deque)
    after_hours: bool = False
    risk_block: int = 90
    cap: str | None = "5"
    caps_on_position_read: deque[str | None] = field(default_factory=deque)
    reported_height: int | None = 120
    cache_contract_queries: bool = False
    drop_pinned_height: bool = False
    cap_query_heights: list[str | None] = field(default_factory=list)
    collateral_price: str | None = "1"
    close_fee: str = "0.002"
    minimum_position_microusd: str = "1"
    preview_inputs: bool = False
    malformed_node_info: bool = False
    deny_lcd: bool = False
    denied_lcd_targets: list[str] = field(default_factory=list)
    price: float | str = 100.5
    prices_on_position_read: deque[str | None] = field(default_factory=deque)
    price_query_heights: list[str | None] = field(default_factory=list)
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
            position_open = True
            indexed_collateral = position.collateral
            if request["query"] == keeper.POSITION:
                if position.switch_and_logout_on_keeper_read is not None:
                    storage.use_session(position.switch_and_logout_on_keeper_read)
                    assert storage.delete_session(ADDR) == ADDR
                    position.switch_and_logout_on_keeper_read = None
                if position.snapshot_leverages:
                    position.leverage = position.snapshot_leverages.popleft()
                if position.position_read_states:
                    position_open = position.position_read_states.popleft()
                if position.snapshot_traders:
                    position.trader = position.snapshot_traders.popleft()
                if position.indexed_collaterals:
                    indexed_collateral = position.indexed_collaterals.popleft()
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
                        "isOpen": position.keeper_market_open,
                        "maxLeverage": position.max_leverage
                        if position.quoted_max_leverage is None
                        else position.quoted_max_leverage,
                    }
                data = {
                    keeper.ROUTE: {
                        "oneClickTrading": {
                            "enabled": True,
                            "chainId": position.chain_id,
                            "contractAddress": position.contract_address,
                        }
                    },
                    keeper.POSITION: {
                        "perp": {
                            "trade": {
                                "id": 7,
                                "trader": position.trader,
                                "isOpen": True,
                                "tradeType": "trade",
                                "collateralAmount": indexed_collateral,
                                "leverage": float(position.leverage),
                                "perpBorrowing": market,
                            }
                            if position_open
                            else None
                        }
                    },
                    keeper.MARKET: {"perp": {"borrowing": market_row}},
                    keeper.MARKETS: {"perp": {"borrowings": [market_row]}},
                }[request["query"]]
            self.reply({"data": data})

        def do_GET(self) -> None:
            if position.deny_lcd:
                position.denied_lcd_targets.append(self.path)
                self.reply({}, status=401)
                return
            path = urlsplit(self.path).path
            if path == "/cosmos/base/tendermint/v1beta1/node_info":
                network = position.lcd_chain_id or position.chain_id
                if position.lcd_chain_ids:
                    network = position.lcd_chain_ids.popleft()
                self.reply(
                    {
                        "default_node_info": None
                        if position.malformed_node_info
                        else {"network": network}
                    }
                )
                return
            if (
                path
                == f"/cosmos/base/tendermint/v1beta1/blocks/{position.reported_height}"
            ):
                self.reply(
                    {
                        "sdk_block": {
                            "header": {
                                "height": str(position.reported_height),
                                "time": datetime.fromtimestamp(
                                    position.block_timestamp, UTC
                                ).isoformat(),
                            }
                        }
                    }
                )
                return
            prefix = f"/cosmwasm/wasm/v1/contract/{position.contract_address}/smart/"
            assert path.startswith(prefix)
            message = json.loads(base64.b64decode(unquote(path[len(prefix) :])))
            if "get_trade_raw" in message:
                position.position_reads += 1
                if position.contract_position_unavailable:
                    self.reply({}, status=503)
                    return
                index = message["get_trade_raw"]["index"]
                if index != 7:
                    self.reply(
                        {
                            "code": 3,
                            "message": f"Generic error: Trade not found for trader {TRADER} with index {index}",
                        },
                        status=position.missing_trade_status,
                    )
                    return
                if position.logout_on_position_read:
                    assert storage.delete_session(ADDR) == ADDR
                    position.logout_on_position_read = False
                if position.caps_on_position_read:
                    cap = position.caps_on_position_read.popleft()
                    if cap is not None:
                        position.cap = cap
                        if position.reported_height is not None:
                            position.reported_height += 1
                if position.prices_on_position_read:
                    price = position.prices_on_position_read.popleft()
                    if price is not None:
                        position.collateral_price = price
                        if position.reported_height is not None:
                            position.reported_height += 1
            if position.cache_contract_queries and self.path in cached_replies:
                self.reply({"data": cached_replies[self.path]})
                return
            if "get_trade_raw" in message:
                data = {
                    "user": position.contract_owner,
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
            elif "get_perp_prices" in message:
                assert message["get_perp_prices"] == {
                    "market_index": "MarketIndex(0)",
                    "collateral_index": "TokenIndex(1)",
                }
                assert self.headers.get("x-cosmos-block-height") == str(
                    position.reported_height
                )
                data = (
                    {
                        "market": None
                        if position.null_market_price
                        else {
                            "base_per_quote": str(position.price),
                            "base_last_update_time": position.block_timestamp
                            - position.oracle_base_age
                            if position.oracle_base_age is not None
                            else None,
                            "quote_last_update_time": position.block_timestamp
                            - position.oracle_quote_age
                            if position.oracle_quote_age is not None
                            else None,
                        },
                        "collateral": position.collateral_price,
                    }
                    if position.collateral_price is not None
                    else None
                )
            elif "market_order_config" in message:
                assert message["market_order_config"] == {}
                assert self.headers.get("x-cosmos-block-height") == str(
                    position.reported_height
                )
                data = {"fill_at_next_oracle_price": position.fill_at_next_oracle_price}
            elif "get_liquidation_price" in message:
                assert message["get_liquidation_price"] == {
                    "trader": TRADER,
                    "trade_id": "UserTradeIndex(7)",
                    "include_borrowing_fees": True,
                }
                assert self.headers.get("x-cosmos-block-height") == str(
                    position.reported_height
                )
                data = {"liquidation_price": position.liquidation_threshold}
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
                data = {
                    "block_last_opened": 110,
                    "block_last_closed": 100,
                    "fee_index": "FeeIndex(0)",
                    "group_index": "GroupIndex(0)",
                    "market_trading_state": position.market_trading_state,
                }
            elif "get_group" in message:
                assert message["get_group"] == {"index": "GroupIndex(0)"}
                data = {
                    "stale_price_expiration_secs": position.oracle_ttl,
                    "min_leverage": "1",
                    "max_leverage": str(position.max_leverage),
                }
            elif "get_pair_custom_max_leverage" in message:
                assert message["get_pair_custom_max_leverage"] == {"index": 0}
                if position.pair_leverage is None:
                    self.reply(
                        {
                            "code": 2,
                            "message": "type: cosmwasm_std::math::decimal::Decimal; key: [00, 18] not found: query wasm contract failed",
                        },
                        status=500,
                    )
                    return
                data = position.pair_leverage
            elif "get_fees" in message:
                assert message["get_fees"] == {"index": "FeeIndex(0)"}
                data = (
                    {
                        "open_fee_p": "0.001",
                        "close_fee_p": position.close_fee,
                        "trigger_order_fee_p": "0.003",
                        "min_position_size_usd": position.minimum_position_microusd,
                    }
                    if position.preview_inputs
                    else None
                )
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
                position.price_query_heights.append(
                    self.headers.get("x-cosmos-block-height")
                )
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

        def reply(
            self, value: dict[str, Any], height: int | None = None, *, status: int = 200
        ) -> None:
            requested_height = self.headers.get("x-cosmos-block-height")
            if requested_height is not None and height is not None:
                assert requested_height == str(height)
            data = json.dumps(value).encode()
            self.send_response(status)
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


def save_session(endpoint: str) -> None:
    storage.save_session(
        {
            "token": TOKEN,
            "evmAddress": ADDR,
            "endpoint": endpoint,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
    )


@asynccontextmanager
async def connected_client(
    base_url: str, *, lcd_endpoint: str | None = None
) -> AsyncGenerator[ClientSession]:
    endpoint = f"{base_url}/query"
    save_session(endpoint)
    config = settings.load(
        flags={"keeper.endpoint": endpoint, "lcd.endpoint": lcd_endpoint or base_url}
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


@pytest.mark.parametrize("command", CLI_POSITION_ACTIONS)
def test_cli_refuses_another_accounts_position(position_service, command):
    position, base_url = position_service
    endpoint = f"{base_url}/query"
    save_session(endpoint)
    position.trader = OTHER_TRADER
    position.contract_owner = OTHER_TRADER
    args = ["--json", "--endpoint", endpoint, "--lcd-endpoint", base_url, "trade"]
    runner = CliRunner()

    for flags in (["--dry-run"], []):
        result = runner.invoke(main, [*args, *command, *flags])
        assert result.exit_code == 1, result.output
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "INVALID_REQUEST"
        assert error["message"] == "Keeper position belongs to a different account"

    assert position.position_reads == 0
    assert position.submitted == []
    assert not (storage.home() / "intents.sqlite3").exists()


@pytest.mark.parametrize("command", CLI_POSITION_ACTIONS)
def test_cli_refuses_logout_during_keeper_position_read(position_service, command):
    position, base_url = position_service
    endpoint = f"{base_url}/query"
    save_session(endpoint)
    other_account = "0x" + "b" * 40
    storage.save_session({**storage.load_session(), "evmAddress": other_account})
    storage.use_session(ADDR)
    position.switch_and_logout_on_keeper_read = other_account

    args = ["--json", "--endpoint", endpoint, "--lcd-endpoint", base_url, "trade"]
    result = CliRunner().invoke(main, [*args, *command])
    assert position.switch_and_logout_on_keeper_read is None
    assert storage.load_session()["evmAddress"] == other_account
    assert result.exit_code == 6, result.output
    assert json.loads(result.stdout)["error"]["code"] == "SIGNED_OUT"
    assert position.submitted == []
    assert not (storage.home() / "intents.sqlite3").exists()


@pytest.mark.parametrize(
    ("action", "args"),
    [
        *MCP_POSITION_ACTIONS,
        ("margin", {"trade_id": 7, "direction": "add", "amount": "1"}),
        ("partial_close", {"trade_id": 7, "percent": 50}),
    ],
)
def test_mcp_refuses_another_accounts_position(position_service, action, args):
    position, base_url = position_service

    async def scenario():
        async with connected_client(base_url) as client:
            preview = await call(client, f"preview_{action}", args)
            assert preview["addr"] == ADDR
            position.trader = OTHER_TRADER
            position.contract_owner = OTHER_TRADER
            position_reads = position.position_reads

            refused_preview = await call(client, f"preview_{action}", args)
            refused_trade = await call(
                client,
                f"trade_{action}",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            for result in (refused_preview, refused_trade):
                assert result == {
                    "code": "INVALID_REQUEST",
                    "error": "Keeper position belongs to a different account",
                }

            assert position.position_reads == position_reads
            assert position.submitted == []
            assert not (storage.home() / "intents.sqlite3").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(("action", "args"), MCP_POSITION_ACTIONS)
def test_mcp_contract_outage_returns_unverified_preview(position_service, action, args):
    position, base_url = position_service
    position.contract_position_unavailable = True

    async def scenario():
        async with connected_client(base_url) as client:
            preview = await call(client, f"preview_{action}", args)
            assert preview["dryRun"] is True
            assert preview["addr"] == ADDR
            assert preview["positionOwnerVerified"] is False
            assert "previewDigest" not in preview
            assert preview["estimates"]["fees"]["status"] == "unavailable"

            position.contract_position_unavailable = False
            position.preview_inputs = True
            position.keeper_market_open = False
            verified = await call(client, f"preview_{action}", args)
            assert verified["intent"] == preview["intent"]
            assert len(verified["previewDigest"]) == 64
            assert verified["estimates"]["fees"]["status"] == "unavailable"
            assert position.submitted == []
            assert not (storage.home() / "intents.sqlite3").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(("action", "args"), MCP_POSITION_ACTIONS)
@pytest.mark.parametrize("scenario", ["foreign-owner", "missing-owner", "unavailable"])
def test_mcp_rechecks_contract_position_before_admission(
    position_service, action, args, scenario
):
    position, base_url = position_service
    position.trader = ADDR
    position.cache_contract_queries = True

    async def run():
        async with connected_client(base_url) as client:
            preview = await call(client, f"preview_{action}", args)
            position_reads = position.position_reads
            if scenario == "unavailable":
                position.contract_position_unavailable = True
            else:
                position.contract_owner = (
                    OTHER_TRADER if scenario == "foreign-owner" else None
                )

            result = await call(
                client,
                f"trade_{action}",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            if scenario == "unavailable":
                assert result["code"] == "PREVIEW_MISMATCH"
                assert "Unable to verify contract state" in result["error"]
            else:
                assert result == {
                    "code": "INVALID_REQUEST",
                    "error": "Contract position belongs to a different account",
                }
            assert "preview" not in result
            assert position.position_reads == position_reads + 1
            assert position.submitted == []
            assert not (storage.home() / "intents.sqlite3").exists()

    asyncio.run(run())


def test_mcp_refuses_another_account_during_quote_rebuild(position_service):
    position, base_url = position_service
    position.collateral = 100_000_000
    position.leverage = "10"
    position.after_hours = True

    async def scenario():
        async with connected_client(base_url) as client:
            args = {"trade_id": 7, "direction": "add", "amount": "5"}
            preview = await call(client, "preview_margin", args)
            # Both quotes use the initial cap. The final LCD read changes it,
            # so the third lookup must rebuild the quote from Keeper again.
            position.caps_on_position_read.extend([None, None, "4"])
            position.snapshot_traders.extend([TRADER, TRADER, OTHER_TRADER])
            result = await call(
                client,
                "trade_margin",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert result == {
                "code": "INVALID_REQUEST",
                "error": "Keeper position belongs to a different account",
            }
            assert not position.snapshot_traders
            assert position.submitted == []
            assert not (storage.home() / "intents.sqlite3").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("action", "args"),
    [
        ("margin", {"trade_id": 7, "direction": "add", "amount": "1"}),
        ("partial_close", {"trade_id": 7, "percent": 50}),
    ],
)
def test_mcp_quotes_equivalent_position_addresses_through_index_retry(
    position_service, action, args
):
    position, base_url = position_service
    position.snapshot_traders.extend([CHECKSUM_ADDR, TRADER])
    position.indexed_collaterals.append(3_000_000)

    async def scenario():
        async with connected_client(base_url) as client:
            preview = await call(client, f"preview_{action}", args)
            assert preview["addr"] == ADDR
            assert preview["quote"]["collateralBefore"] == "4000000"
            assert not position.snapshot_traders

            position.contract_owner = OTHER_TRADER
            refused = await call(
                client,
                f"trade_{action}",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert refused == {
                "code": "INVALID_REQUEST",
                "error": "Contract position belongs to a different account",
            }
            assert position.submitted == []
            assert not (storage.home() / "intents.sqlite3").exists()

    asyncio.run(scenario())


position_commands = pytest.mark.parametrize(
    ("tool", "args", "command"),
    [
        (
            "margin",
            {"trade_id": 7, "direction": "add", "amount": "1"},
            ["margin", "7", "--direction", "add", "--amount", "1"],
        ),
        (
            "partial_close",
            {"trade_id": 7, "percent": 50},
            ["partial-close", "7", "--percent", "50"],
        ),
    ],
)


def position_cli_args(base_url: str, command: list[str]) -> list[str]:
    return [
        "--json",
        "--endpoint",
        f"{base_url}/query",
        "--lcd-endpoint",
        base_url,
        "trade",
        *command,
    ]


@pytest.mark.parametrize(
    ("field", "value", "route_key"),
    [
        ("chain_id", "test-2", "chainId"),
        ("contract_address", "nibi1replacement", "contractAddress"),
    ],
)
@position_commands
def test_position_confirmation_binds_keeper_route(
    position_service: tuple[Position, str],
    field: str,
    value: str,
    route_key: str,
    tool: str,
    args: dict[str, Any],
    command: list[str],
) -> None:
    position, base_url = position_service
    cli = CliRunner()
    cli_args = position_cli_args(base_url, command)

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, f"preview_{tool}", args)
            cli_preview = cli.invoke(main, [*cli_args, "--dry-run"])
            assert cli_preview.exit_code == 0, cli_preview.output
            digest = preview["previewDigest"]
            assert json.loads(cli_preview.stdout)["previewDigest"] == digest

            setattr(position, field, value)
            cli_refusal = cli.invoke(main, [*cli_args, "--preview-digest", digest])
            assert cli_refusal.exit_code == ExitCode.REJECTED, cli_refusal.output
            assert json.loads(cli_refusal.stdout)["error"]["code"] == "PREVIEW_MISMATCH"
            mcp_refusal = await call(
                client, f"trade_{tool}", {**args, "preview_digest": digest}
            )
            assert mcp_refusal["code"] == "PREVIEW_MISMATCH", mcp_refusal
            assert position.submitted == []
            assert not (storage.home() / "intents.sqlite3").exists()

            fresh = mcp_refusal["preview"]
            assert fresh["intent"] == preview["intent"]
            assert fresh["quote"] == preview["quote"]
            assert fresh["positionRoute"] == {
                **preview["positionRoute"],
                route_key: value,
            }
            assert fresh["previewDigest"] != digest
            assert fresh == await call(client, f"preview_{tool}", args)

            accepted = cli.invoke(
                main, [*cli_args, "--preview-digest", fresh["previewDigest"]]
            )
            assert accepted.exit_code == 0, accepted.output
            assert json.loads(accepted.stdout)["status"] == "accepted"
            (submitted,) = position.submitted
            assert {key: submitted[key] for key in fresh["intent"]} == fresh["intent"]

    asyncio.run(scenario())


@position_commands
def test_position_confirmation_refuses_lcd_only_network_switch(
    position_service: tuple[Position, str],
    tool: str,
    args: dict[str, Any],
    command: list[str],
) -> None:
    position, base_url = position_service
    cli = CliRunner()
    cli_args = position_cli_args(base_url, command)

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, f"preview_{tool}", args)
            digest = preview["previewDigest"]
            trade_args = {**args, "preview_digest": digest}

            position.lcd_chain_id = "test-2"
            cli_refusal = cli.invoke(main, [*cli_args, "--preview-digest", digest])
            assert cli_refusal.exit_code == ExitCode.REJECTED, cli_refusal.output
            cli_error = json.loads(cli_refusal.stdout)["error"]
            mcp_error = await call(client, f"trade_{tool}", trade_args)
            assert cli_error["code"] == mcp_error["code"] == "PREVIEW_MISMATCH"
            assert cli_error["message"] == mcp_error["error"]
            assert "Use an LCD on Keeper's chain" in cli_error["message"]
            assert "details" not in cli_error and "preview" not in mcp_error

            for flags in ([], ["--dry-run"]):
                cli_invalid = cli.invoke(main, [*cli_args, *flags])
                assert (
                    json.loads(cli_invalid.stdout)["error"]["code"] == "INVALID_REQUEST"
                )
            assert (await call(client, f"preview_{tool}", args))[
                "code"
            ] == "INVALID_REQUEST"

            position.lcd_chain_id = None
            position.malformed_node_info = True
            malformed = await call(client, f"trade_{tool}", trade_args)
            assert malformed["code"] == "INVALID_REQUEST"
            position.malformed_node_info = False
            position.leverage = "2.5"
            position.lcd_chain_ids.append("test-2")
            cli_refusal = cli.invoke(main, [*cli_args, "--preview-digest", digest])
            assert cli_refusal.exit_code == ExitCode.REJECTED, cli_refusal.output
            cli_error = json.loads(cli_refusal.stdout)["error"]
            assert cli_error["code"] == "PREVIEW_MISMATCH"

            position.lcd_chain_ids.append("test-2")
            mcp_error = await call(client, f"trade_{tool}", trade_args)
            assert mcp_error["code"] == "PREVIEW_MISMATCH"
            fresh = await call(client, f"preview_{tool}", args)
            assert cli_error["details"]["preview"] == mcp_error["preview"] == fresh
            assert fresh["previewDigest"] != digest
            assert fresh["positionRoute"] == preview["positionRoute"]

            position.lcd_chain_ids.append("test-2")
            plain_refusal = cli.invoke(
                main, [*cli_args[1:], "--preview-digest", digest]
            )
            assert plain_refusal.exit_code == ExitCode.REJECTED
            assert "Run with --dry-run" in plain_refusal.stderr

            position.trade_type = "limit"
            position.lcd_chain_ids.append("test-2")
            unavailable = await call(client, f"trade_{tool}", trade_args)
            assert unavailable["code"] == "PREVIEW_MISMATCH"
            assert "Could not refresh the position preview" in unavailable["error"]
            assert "preview" not in unavailable
            assert position.submitted == []
            assert not (storage.home() / "intents.sqlite3").exists()

    asyncio.run(scenario())


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
            assert result["quoteStatus"] == "available"
            assert result["recovery"]["action"] == "confirm_preview"
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


def test_position_disappears_during_confirmation(position_service):
    position, base_url = position_service
    args = {"trade_id": 7, "percent": 50}

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_partial_close", args)
            position.position_read_states.extend([True, False])
            result = await call(
                client,
                "trade_partial_close",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert result["code"] == "PREVIEW_MISMATCH"
            assert "Position not found for this account" in result["error"]
            assert result["quoteStatus"] == "unavailable"
            assert result["recovery"]["action"] == "request_preview"
            assert "confirmation" in result["recovery"]["message"]
            assert "preview" not in result
            assert position.submitted == []
            assert not (storage.home() / "intents.sqlite3").exists()

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
            assert rejected["quoteStatus"] == "unavailable"
            assert rejected["recovery"]["action"] == "request_preview"
            assert "preview" not in rejected
            assert position.submitted == []

            position.drop_pinned_height = False
            accepted = await call(client, f"trade_{tool}", trade)
            assert accepted["status"] == "accepted", accepted
            assert len(position.submitted) == 1

    asyncio.run(scenario())


def test_partial_close_rechecks_oracle_at_the_final_position_height(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    position.collateral = 100_000_000
    position.leverage = "10"
    position.after_hours = True
    position.cache_contract_queries = True
    args = {"trade_id": 7, "percent": 50}

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_partial_close", args)
            assert preview["quote"]["closedNotionalUsd"] == "250"
            position.prices_on_position_read.extend([None, None, "0.98"])
            result = await call(
                client,
                "trade_partial_close",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert result.get("code") == "PREVIEW_MISMATCH", result
            assert position.submitted == []
            assert position.price_query_heights == [None, None, None, "121", None]
            fresh = result["preview"]
            assert fresh["quote"]["collateralPriceUsd"] == "0.98"
            assert fresh["quote"]["closedNotionalUsd"] == "245"
            assert fresh["intent"] == preview["intent"]
            assert fresh["previewDigest"] != preview["previewDigest"]
            assert fresh == await call(client, "preview_partial_close", args)

            accepted = await call(
                client,
                "trade_partial_close",
                {**args, "preview_digest": fresh["previewDigest"]},
            )
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
    preview = json.loads(result.stdout)
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


def test_signed_out_market_open_requires_an_account_preview(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    position.preview_inputs = True
    args = {
        "market_id": 0,
        "side": "long",
        "order_type": "market",
        "amount": "10",
        "leverage": "2",
        "slippage": "1",
    }

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            session = storage.load_session(ADDR)
            assert storage.delete_session(ADDR) == ADDR
            preview = await call(client, "preview_open", args)
            assert preview["addr"] is None
            assert preview["confirmationStatus"] == "informational"
            assert preview["recovery"]["action"] == "connect_and_preview"
            assert "preview_open" in preview["recovery"]["message"]
            assert "previewDigest" not in preview
            assert preview["referencePrice"] == "100.5"
            assert preview["estimates"]["worstCaseFill"]["status"] == "bound"
            assert position.submitted == []

            # Older versions emitted this digest for the same accountless quote.
            legacy_digest = confirmation.preview_digest(preview)
            storage.save_session(session)
            trade = {**args, "reference_price": preview["referencePrice"]}
            refused = await call(
                client, "trade_open", {**trade, "preview_digest": legacy_digest}
            )
            assert refused["code"] == "PREVIEW_MISMATCH"
            assert refused["preview"]["addr"] == ADDR
            assert position.submitted == []

            fresh = await call(client, "preview_open", {**args, "addr": ADDR})
            assert fresh["addr"] == ADDR
            assert fresh["previewDigest"] != legacy_digest
            result = await call(
                client,
                "trade_open",
                {**trade, "addr": ADDR, "preview_digest": fresh["previewDigest"]},
            )
            assert result["status"] == "accepted"
            assert result["addr"] == ADDR
            (submitted,) = position.submitted
            assert submitted["kind"] == fresh["intent"]["kind"]
            assert submitted["body"] == {
                **fresh["intent"]["body"],
                "intentPrice": fresh["referencePrice"],
            }

    asyncio.run(scenario())


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


@pytest.mark.parametrize(("side", "bound"), [("short", "99"), ("long", "101")])
def test_mcp_stop_bounds_fill_even_without_a_collateral_price(
    position_service: tuple[Position, str],
    side: str,
    bound: str,
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
                    "side": side,
                    "order_type": "stop",
                    "price": "100",
                    "amount": "10",
                    "leverage": "2",
                    "slippage": "1",
                },
            )
            estimates = preview["estimates"]
            assert estimates["worstCaseFill"]["status"] == "bound"
            assert estimates["worstCaseFill"]["price"] == bound
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
                ("partial_close", {"percent": 50}, "0.02", "unbounded", None),
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
                    assert quote["status"] in {
                        "unchanged",
                        "not_applicable",
                        "unavailable",
                    }
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


def test_lcd_http_failure_logs_only_the_origin_in_an_embedded_server(
    position_service, caplog
):
    position, base_url = position_service
    position.preview_inputs = True
    position.deny_lcd = True
    path_key, query_key = "fixture-private-lcd-path", "fixture-private-lcd-query"
    lcd = f"{base_url}/{path_key}?key={query_key}"

    async def preview():
        async with connected_client(base_url, lcd_endpoint=lcd) as client:
            return await call(
                client,
                "preview_open",
                {
                    "market_id": 0,
                    "side": "long",
                    "order_type": "limit",
                    "price": "100",
                    "amount": "10",
                    "leverage": "2",
                    "slippage": "1",
                },
            )

    with caplog.at_level(logging.INFO):
        quote = asyncio.run(preview())
        with pytest.raises(margin.QueryHeightMismatch) as denied:
            margin.block_header(lcd, "latest", deadline=time.monotonic() + 5)
    assert quote["estimates"]["fees"]["status"] == "unavailable"
    assert position.submitted == []
    assert any(record.name == "httpx" for record in caplog.records)
    assert caplog.text.count(f"LCD HTTP response from {base_url} (status 401)") == 2
    assert position.denied_lcd_targets[0] == (
        f"/{path_key}{margin.NODE_INFO_PATH}?key={query_key}"
    )
    assert position.denied_lcd_targets[1].startswith(
        f"/{path_key}{margin.BLOCK_PATH}latest?key={query_key}&_sai_query="
    )
    log_data = caplog.text + repr([record.args for record in caplog.records])
    error_trace = "".join(traceback.format_exception(denied.value))
    for secret in (path_key, query_key):
        assert secret not in log_data
        assert secret not in error_trace + json.dumps(quote)
    assert f"LCD at {base_url} returned HTTP 401" in error_trace


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
    position.cache_contract_queries = True
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


def test_cancel_refuses_another_accounts_pending_order(position_service):
    position, base_url = position_service
    position.trade_type = "limit"

    async def scenario():
        async with connected_client(base_url) as client:
            args = {"trade_id": 7, "cancel_order": True}
            preview = await call(client, "preview_close", args)
            assert preview["addr"] == ADDR
            position.contract_owner = OTHER_TRADER

            refused_preview = await call(client, "preview_close", args)
            refused_trade = await call(
                client,
                "trade_close",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert (
                refused_preview
                == refused_trade
                == {
                    "code": "INVALID_REQUEST",
                    "error": "Contract pending order belongs to a different account",
                }
            )
            assert position.submitted == []
            assert not (storage.home() / "intents.sqlite3").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("cap", ["5", None])
def test_position_mutations_show_no_zero_fee_without_an_inactive_guard(
    position_service, cap
):
    position, base_url = position_service
    position.preview_inputs = True
    position.after_hours = True
    position.leverage = "20"
    position.cap = cap

    async def scenario():
        async with connected_client(base_url) as client:
            for tool, args in (
                ("preview_tpsl", {"clear_stop_loss": True}),
                ("preview_leverage", {"leverage": "3"}),
            ):
                quote = await call(client, tool, {"trade_id": 7, **args})
                for field in ("fees", "liquidationPrice"):
                    item = quote["estimates"][field]
                    assert item["status"] == "unavailable"
                    assert "guard" in item["reason"]
                    assert "amountUsdc" not in item
            assert position.submitted == []

    asyncio.run(scenario())


def test_close_fee_uses_exact_perp_rate(position_service):
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral = 100_000_000
    position.close_fee = "0.019999999999999999"

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(client, "preview_close", {"trade_id": 7})
            assert quote["estimates"]["fees"]["amountUsdc"] == "3.999999"

    asyncio.run(scenario())


def test_partial_close_includes_the_perp_trigger_fee(position_service):
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral = 100_000_000

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(
                client, "preview_partial_close", {"trade_id": 7, "percent": 50}
            )
            assert quote["quote"]["closedNotionalUsdc"] == "100"
            assert quote["estimates"]["fees"]["amountUsdc"] == "0.5"

    asyncio.run(scenario())


def test_close_previews_detect_full_liquidation_and_unknown_partial_settlement(
    position_service,
):
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral = 100_000_000
    position.leverage = "20"
    position.max_leverage = 20
    position.price = "95"
    position.liquidation_threshold = "96.025"

    async def scenario():
        async with connected_client(base_url) as client:
            for tool, args in (
                ("preview_close", {}),
                ("preview_partial_close", {"percent": 10}),
            ):
                quote = await call(client, tool, {"trade_id": 7, **args})
                result = quote["estimates"]
                assert result["execution"]["status"] == "would_liquidate"
                assert result["execution"]["marketPrice"]["price"] == "95"
                assert result["execution"]["liquidationPrice"] == "96.025"
                assert result["fees"]["amountUsdc"] == "10"
                assert result["liquidationPrice"]["status"] == "not_applicable"

            position.price = "96.10"
            quote = await call(
                client, "preview_partial_close", {"trade_id": 7, "percent": 1}
            )
            result = quote["estimates"]
            assert result["execution"]["status"] == "unavailable"
            assert "rejected" in result["execution"]["reason"]
            assert result["liquidationPrice"]["status"] == "unavailable"
            assert "price" not in result["liquidationPrice"]
            position.reported_height = None
            quote = await call(client, "preview_close", {"trade_id": 7})
            assert quote["estimates"]["execution"]["status"] == "unavailable"
            assert quote["estimates"]["fees"]["status"] == "unavailable"

            assert position.submitted == []

    asyncio.run(scenario())


def test_cancel_refuses_logout_during_the_last_lcd_read(position_service):
    position, base_url = position_service
    position.trade_type = "limit"
    args = {"trade_id": 7, "cancel_order": True}

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(client, "preview_close", args)
            position.logout_on_position_read = True
            refusal = await call(
                client,
                "trade_close",
                {
                    **args,
                    "preview_digest": quote["previewDigest"],
                },
            )
            assert not position.logout_on_position_read
            assert refusal["code"] == "SIGNED_OUT"
            assert position.submitted == []

    asyncio.run(scenario())


def test_full_close_fees_use_carried_positions_effective_leverage(position_service):
    position, base_url = position_service
    position.preview_inputs = True
    position.after_hours = True
    position.collateral = 100_000_000
    position.leverage = "20"
    position.cap = "5"

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(client, "preview_close", {"trade_id": 7})
            estimates = quote["estimates"]
            assert estimates["execution"]["status"] == "voluntary_close"
            assert estimates["fees"]["amountUsdc"] == "1"
            assert estimates["liquidationPrice"]["status"] == "not_applicable"
            partial = await call(
                client, "preview_partial_close", {"trade_id": 7, "percent": 50}
            )
            assert partial["estimates"]["fees"]["status"] == "unavailable"
            assert position.submitted == []

    asyncio.run(scenario())


def test_voluntary_close_fees_cannot_exceed_position_collateral(position_service):
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral = 1_000_000
    position.leverage = "100"
    position.close_fee = "0.02"

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(client, "preview_close", {"trade_id": 7})
            estimates = quote["estimates"]
            assert estimates["execution"]["status"] == "voluntary_close"
            assert estimates["fees"]["amountUsdc"] == "1"

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [400, 500])
def test_cancel_missing_trade_is_a_usage_error(position_service, status):
    position, base_url = position_service
    position.missing_trade_status = status

    async def scenario():
        async with connected_client(base_url) as client:
            result = await call(
                client, "preview_close", {"trade_id": 99, "cancel_order": True}
            )
            assert result["code"] == "INVALID_REQUEST"
            assert result["error"] == "Trade 99 was not found for this account."
            assert "preview" not in result
            assert position.submitted == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("age", "execution"), [(300, "would_liquidate"), (900, "would_be_rejected")]
)
def test_close_liquidation_respects_perp_oracle_ttl(position_service, age, execution):
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral = 100_000_000
    position.leverage = "20"
    position.price = "95"
    position.liquidation_threshold = "96"
    position.oracle_base_age = 100
    position.oracle_quote_age = age

    async def scenario():
        async with connected_client(base_url) as client:
            for tool, args in (
                ("preview_close", {}),
                ("preview_partial_close", {"percent": 10}),
            ):
                quote = await call(client, tool, {"trade_id": 7, **args})
                result = quote["estimates"]
                assert result["execution"]["status"] == execution
                if age > position.oracle_ttl:
                    assert result["execution"]["code"] == "OraclePriceStale"
                    assert result["fees"]["status"] == "unavailable"
                    assert "amountUsdc" not in result["fees"]

    asyncio.run(scenario())


def test_open_liquidation_preview_requires_perps_minimum_collateral(position_service):
    position, base_url = position_service
    position.preview_inputs = True
    position.minimum_position_microusd = "100000000"
    args = {
        "market_id": 0,
        "side": "long",
        "order_type": "limit",
        "price": "100",
        "amount": "1",
        "leverage": "2",
        "slippage": "1",
    }

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(client, "preview_open", args)
            liq = quote["estimates"]["liquidationPrice"]
            assert liq["status"] == "unavailable"
            assert "outside" in liq["reason"]
            assert "price" not in liq
            boundary = await call(client, "preview_open", {**args, "amount": "2.5"})
            assert boundary["estimates"]["liquidationPrice"]["status"] == "estimate"

    asyncio.run(scenario())


def test_leverage_liquidation_preview_requires_market_leverage_cap(position_service):
    position, base_url = position_service
    position.preview_inputs = True
    position.leverage = "20"
    position.collateral = 100_000_000
    position.max_leverage = 50

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(
                client, "preview_leverage", {"trade_id": 7, "leverage": "100"}
            )
            liq = quote["estimates"]["liquidationPrice"]
            assert liq["status"] == "unavailable"
            assert "outside" in liq["reason"]
            assert "price" not in liq
            boundary = await call(
                client, "preview_leverage", {"trade_id": 7, "leverage": "50"}
            )
            assert boundary["estimates"]["liquidationPrice"]["status"] == "estimate"

    asyncio.run(scenario())


def test_null_market_price_preserves_close_preview_and_refusal(position_service):
    position, base_url = position_service
    position.preview_inputs = True
    position.null_market_price = True

    async def scenario():
        async with connected_client(base_url) as client:
            args = {"trade_id": 7}
            preview = await call(client, "preview_close", args)
            assert preview["estimates"]["execution"]["status"] == "unavailable"
            assert preview["estimates"]["fees"]["status"] == "unavailable"
            result = await call(
                client, "trade_close", {**args, "preview_digest": "wrong"}
            )
            assert result["code"] == "PREVIEW_MISMATCH"
            assert result["preview"] == preview
            assert position.submitted == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("indexed_cap", "pair_cap", "status"),
    [
        (0, "0", "estimate"),
        (0, None, "estimate"),
        (20, "4", "unavailable"),
    ],
)
def test_preview_uses_perp_group_and_pair_leverage_limits(
    position_service, indexed_cap, pair_cap, status
):
    position, base_url = position_service
    position.preview_inputs = True
    position.max_leverage = 20
    position.quoted_max_leverage = indexed_cap
    position.pair_leverage = pair_cap

    async def scenario():
        async with connected_client(base_url) as client:
            opened = await call(
                client,
                "preview_open",
                {
                    "market_id": 0,
                    "side": "long",
                    "order_type": "limit",
                    "price": "100",
                    "amount": "10",
                    "leverage": "5",
                    "slippage": "1",
                },
            )
            changed = await call(
                client, "preview_leverage", {"trade_id": 7, "leverage": "5"}
            )
            assert opened["estimates"]["fees"]["status"] == status
            assert opened["estimates"]["liquidationPrice"]["status"] == status
            assert changed["estimates"]["liquidationPrice"]["status"] == status

    asyncio.run(scenario())


def test_stale_prices_are_unavailable_for_queued_closes(position_service):
    position, base_url = position_service
    position.preview_inputs = True
    position.leverage = "20"
    position.price = "95"
    position.liquidation_threshold = "96"
    position.oracle_base_age = 900
    position.fill_at_next_oracle_price = True

    async def scenario():
        async with connected_client(base_url) as client:
            for tool, args in (
                ("preview_close", {}),
                ("preview_partial_close", {"percent": 10}),
            ):
                quote = await call(client, tool, {"trade_id": 7, **args})
                result = quote["estimates"]
                assert result["execution"]["status"] == "unavailable"
                assert "fresh oracle update" in result["execution"]["reason"]
                assert result["fees"]["status"] == "unavailable"

    asyncio.run(scenario())


@pytest.mark.parametrize("order_type", ["limit", "stop"])
def test_pending_open_estimates_follow_perp_market_state(position_service, order_type):
    position, base_url = position_service
    position.preview_inputs = True
    position.keeper_market_open = False
    position.market_trading_state = "closed"
    args = {
        "market_id": 0,
        "side": "long",
        "order_type": order_type,
        "price": "100",
        "amount": "10",
        "leverage": "2",
        "slippage": "1",
    }

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(client, "preview_open", args)
            assert quote["estimates"]["fees"]["status"] == "estimate"
            assert quote["estimates"]["worstCaseFill"]["status"] == "bound"
            position.keeper_market_open = True
            position.market_trading_state = "close_only"
            quote = await call(client, "preview_open", args)
            for field in ("fees", "liquidationPrice", "worstCaseFill"):
                item = quote["estimates"][field]
                assert item["status"] == "unavailable"
                assert "close-only" in item["reason"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("price", "queued", "age", "code"),
    [
        ("97", False, 100, "LiquidationReached"),
        ("98.4", False, None, "LiquidationReached"),
        ("97", False, 900, "OraclePriceStale"),
        ("99", False, None, None),
        ("97", True, None, None),
    ],
)
def test_leverage_preview_checks_prospective_liquidation_at_oracle_price(
    position_service, price, queued, age, code
):
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral = 100_000_000
    position.leverage = "20"
    position.max_leverage = 50
    position.price = price
    position.fill_at_next_oracle_price = queued
    position.oracle_base_age = age

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(
                client, "preview_leverage", {"trade_id": 7, "leverage": "50"}
            )
            estimates = quote["estimates"]
            liq = estimates["liquidationPrice"]
            if code:
                assert estimates["execution"]["status"] == "would_be_rejected"
                assert estimates["execution"]["code"] == code
                assert liq["status"] == "unavailable"
                assert "price" not in liq
            else:
                assert liq["status"] == "estimate"
                assert liq["price"] == "98.4"
                assert "borrowing-inclusive" in liq["assumption"]
                if queued:
                    assert estimates["execution"]["status"] == "unavailable"
                    assert "fresh oracle update" in estimates["execution"]["reason"]

    asyncio.run(scenario())
