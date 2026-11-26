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
    available_balance: int = 10_000_000
    pinned_balance: int | None = None
    trader: str = TRADER
    contract_owner: str | None = TRADER
    contract_position_unavailable: bool = False
    position_reads: int = 0
    trade_type: str = "trade"
    keeper_market_open: bool = True
    keeper_market_visible: bool = True
    market_trading_state: str = "open"
    switch_and_logout_on_keeper_read: str | None = None
    logout_on_position_read: bool = False
    missing_trade_status: int = 400
    leverage: str = "2"
    max_leverage: int = 100
    quoted_max_leverage: int | None = None
    pair_leverage: str | None = "0"
    liquidation_threshold: str = "60"
    quoted_liquidation_threshold: str | None = None
    quote_error_height: int | None = None
    block_timestamp: int = 2_000_000_000
    oracle_base_age: int | None = 0
    null_market_price: bool = False
    oracle_quote_age: int | None = None
    oracle_ttl: int = 300
    fill_at_next_oracle_price: bool = False
    market_order_config_fails: bool = False
    snapshot_leverages: deque[str] = field(default_factory=deque)
    position_read_states: deque[bool] = field(default_factory=deque)
    snapshot_traders: deque[str] = field(default_factory=deque)
    indexed_collaterals: deque[int] = field(default_factory=deque)
    after_hours: bool = False
    risk_block: int = 90
    cap: str | None = "5"
    caps_on_position_read: deque[str | None] = field(default_factory=deque)
    collateral_on_position_read: deque[int | None] = field(default_factory=deque)
    reported_height: int | None = 120
    cache_contract_queries: bool = False
    drop_pinned_height: bool = False
    cap_query_heights: list[str | None] = field(default_factory=list)
    collateral_price: str | None = "1"
    close_fee: str = "0.002"
    minimum_position_microusd: str = "1"
    keeper_minimum_position_microusd: int = 1
    market_reads: int = 0
    preview_inputs: bool = False
    malformed_node_info: bool = False
    deny_lcd: bool = False
    denied_lcd_targets: list[str] = field(default_factory=list)
    price: float | str = 100.5
    switch_contract_on_market_read: str | None = None
    switch_contract_on_perp_market_read: str | None = None
    prices_on_position_read: deque[str | None] = field(default_factory=deque)
    price_query_heights: list[str | None] = field(default_factory=list)
    perp_price_query_heights: list[str | None] = field(default_factory=list)
    collateral_price_on_perp_read: str | None = None
    oracle_position_updates: deque[tuple[int, str] | None] = field(
        default_factory=deque
    )
    lose_admission_ack: bool = False
    reject_admission: bool = False
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
        "collateralToken": {"id": 1, "symbol": "USDC"},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            assert self.path == "/query"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            position_open = True
            if request["query"] == keeper.MARKETS:
                position.market_reads += 1
                if position.switch_contract_on_market_read is not None:
                    position.contract_address = position.switch_contract_on_market_read
                    position.switch_contract_on_market_read = None
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
                if position.reject_admission:
                    self.reply(
                        {"errors": [{"extensions": {"code": "INTENT_NOT_ACCEPTED"}}]}
                    )
                    return
                if position.lose_admission_ack:
                    self.reply({"data": {"recordUserIntent": None}})
                    return
                data = {
                    "recordUserIntent": {
                        "intentKey": envelope["intentKey"],
                        "lifecycleStep": "jwt_pass",
                        "execActor": "server",
                        "evmAddress": ADDR,
                    }
                }
            else:
                market_row = {
                    **market,
                    "minPositionSizeUSD": position.keeper_minimum_position_microusd,
                }
                if position.preview_inputs:
                    market_row = {
                        **market_row,
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
                    keeper.MARKETS: {
                        "perp": {
                            "borrowings": [market_row]
                            if position.keeper_market_visible
                            else []
                        }
                    },
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
                if position.collateral_on_position_read:
                    collateral = position.collateral_on_position_read.popleft()
                    if collateral is not None:
                        position.collateral = collateral
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
                position.perp_price_query_heights.append(
                    self.headers.get("x-cosmos-block-height")
                )
                if position.collateral_price_on_perp_read is not None:
                    position.collateral_price = position.collateral_price_on_perp_read
                    position.collateral_price_on_perp_read = None
                data = {
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
            elif "market_order_config" in message:
                assert message["market_order_config"] == {}
                assert self.headers.get("x-cosmos-block-height") == str(
                    position.reported_height
                )
                if position.market_order_config_fails:
                    self.reply({}, status=503)
                    return
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
            elif "quote_trade" in message:
                assert not position.fill_at_next_oracle_price
                assert self.headers.get("x-cosmos-block-height") == str(
                    position.reported_height
                )
                quote = message["quote_trade"]
                leverage = quote["action"]["update_leverage"]["new_leverage"]
                assert quote == {
                    "trader": TRADER,
                    "funds": [],
                    "action": {
                        "update_leverage": {
                            "trade_index": "UserTradeIndex(7)",
                            "new_leverage": leverage,
                        }
                    },
                }
                threshold = position.quoted_liquidation_threshold
                if threshold is not None:
                    assert leverage == "20"
                    if Decimal(str(position.price)) <= Decimal(threshold):
                        self.reply(
                            {
                                "code": 2,
                                "message": f"Generic error: liquidation price reached. "
                                f"Liq price {threshold}, current price 1: query wasm contract failed",
                            },
                            status=500,
                            bare_height=position.quote_error_height,
                        )
                        return
                data = {"data": None, "attributes": [], "events": [], "messages": []}
            elif "get_exchange_balance" in message:
                balance = message["get_exchange_balance"]
                assert keeper.same_account(TRADER, balance["user"])
                assert balance["collateral_index"] == "TokenIndex(1)"
                pinned = self.headers.get("x-cosmos-block-height") is not None
                available = position.available_balance
                if pinned and position.pinned_balance is not None:
                    available = position.pinned_balance
                data = {
                    "collateral_index": "TokenIndex(1)",
                    "available_to_trade": str(available),
                }
            elif "is_after_hours_market" in message:
                assert message["is_after_hours_market"] == {
                    "market_index": "MarketIndex(0)"
                }
                data = position.after_hours
            elif "get_market" in message:
                assert message["get_market"] == {"index": "MarketIndex(0)"}
                if position.switch_contract_on_perp_market_read is not None:
                    position.contract_address = (
                        position.switch_contract_on_perp_market_read
                    )
                    position.switch_contract_on_perp_market_read = None
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
            self,
            value: dict[str, Any],
            height: int | None = None,
            *,
            status: int = 200,
            bare_height: int | None = None,
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
            if bare_height is not None:
                self.send_header("x-cosmos-block-height", str(bare_height))
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


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (
            "open",
            {
                "market_id": 0,
                "side": "long",
                "order_type": "limit",
                "amount": "5",
                "leverage": "2",
                "price": "100",
                "slippage": "1",
            },
        ),
        *MCP_POSITION_ACTIONS,
        pytest.param("close", {"trade_id": 7, "cancel_order": True}, id="cancel"),
        ("margin", {"trade_id": 7, "direction": "add", "amount": "1"}),
        ("partial_close", {"trade_id": 7, "percent": 50}),
    ],
)
@pytest.mark.parametrize("lose_ack", [False, True], ids=["blocked", "lost-ack"])
def test_unresolved_trade_returns_ledger_key_and_account(
    position_service: tuple[Position, str],
    tool: str,
    args: dict[str, Any],
    lose_ack: bool,
) -> None:
    position, base_url = position_service
    position.preview_inputs = True
    position.lose_admission_ack = lose_ack
    if args.get("cancel_order"):
        position.trade_type = "limit"
    endpoint = f"{base_url}/query"
    blocked_key = "20261126T200000000Z_0123456789ABCDEF"
    recent_open = "20261126T200001000Z_0123456789ABCDEF"

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, f"preview_{tool}", args)
            if not lose_ack:
                storage.pending(blocked_key, endpoint, ADDR, "CLOSE_POSITION", "7")
            refused = await call(
                client,
                f"trade_{tool}",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            local = await call(client, "intent_local", {"addr": ADDR})
            [row] = local["intents"]
            assert row["status"] == "unknown"
            if not lose_ack:
                assert row["intentKey"] == blocked_key
            assert refused["code"] == (
                "OUTCOME_UNKNOWN" if lose_ack else "INVALID_REQUEST"
            )
            assert (refused.get("intentKey"), refused.get("addr")) == (
                row["intentKey"],
                ADDR,
            )
            assert len(position.submitted) == int(lose_ack)
            if tool == "open" and not lose_ack:
                storage.set_status(blocked_key, endpoint, ADDR, "terminal")
                storage.pending(recent_open, endpoint, ADDR, "OPEN_TRADE", "")
                refused = await call(
                    client,
                    "trade_open",
                    {**args, "preview_digest": preview["previewDigest"]},
                )
                assert refused["code"] == "RECENT_OPEN_PENDING"
                assert (
                    refused["intentKey"],
                    refused["intentKeys"],
                    refused["addr"],
                ) == (
                    recent_open,
                    [recent_open],
                    ADDR,
                )
                assert not position.submitted

    asyncio.run(scenario())


def test_mcp_rejection_preserves_recovery_when_status_write_fails(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    position.reject_admission = True
    params = {"trade_id": 7}
    with storage.db() as ledger:
        ledger.execute(
            "CREATE TRIGGER reject_status_write BEFORE UPDATE OF status ON intents "
            "BEGIN SELECT RAISE(ABORT, 'ledger write failed'); END"
        )

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_close", params)
            outcome = await call(
                client,
                "trade_close",
                {**params, "preview_digest": preview["previewDigest"]},
            )
            assert outcome["code"] == "ADMISSION_REJECTED"
            [row] = (await call(client, "intent_local", {"addr": ADDR}))["intents"]
            assert row["status"] == "unknown"
            key = row["intentKey"]
            assert (outcome.get("intentKey"), outcome.get("addr")) == (key, ADDR)
            assert all(item in outcome["next"] for item in ("intent_status", key, ADDR))
            assert len(position.submitted) == 1

    asyncio.run(scenario())


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
            position.null_market_price = True
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


OPEN_ARGS = {
    "market_id": 0,
    "side": "long",
    "amount": "10",
    "leverage": "2",
    "slippage": "1",
}
OPEN_COMMAND = [
    "open",
    "--market",
    "0",
    "--side",
    "long",
    "--amount",
    "10",
    "--leverage",
    "2",
    "--slippage",
    "1",
]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chain_id", "test-2"),
        ("contract_address", "nibi1replacement"),
        ("lcd_chain_id", "test-2"),
    ],
)
@pytest.mark.parametrize(
    ("tool", "args", "command"),
    [
        pytest.param("close", {"trade_id": 7}, ["close", "7"], id="close"),
        pytest.param(
            "close",
            {"trade_id": 7, "cancel_order": True},
            ["close", "7", "--cancel-order"],
            id="cancel",
        ),
        pytest.param(
            "leverage",
            {"trade_id": 7, "leverage": "3"},
            ["leverage", "7", "--leverage", "3"],
            id="leverage",
        ),
        pytest.param(
            "tpsl",
            {"trade_id": 7, "take_profit": "120"},
            ["tpsl", "7", "--take-profit", "120"],
            id="tpsl",
        ),
        pytest.param(
            "open",
            {**OPEN_ARGS, "order_type": "market"},
            [*OPEN_COMMAND, "--type", "market"],
            id="market-open",
        ),
        pytest.param(
            "open",
            {**OPEN_ARGS, "order_type": "limit", "price": "100"},
            [*OPEN_COMMAND, "--type", "limit", "--price", "100"],
            id="limit-open",
        ),
        pytest.param(
            "open",
            {**OPEN_ARGS, "order_type": "stop", "price": "101"},
            [*OPEN_COMMAND, "--type", "stop", "--price", "101"],
            id="stop-open",
        ),
    ],
)
def test_trade_confirmation_binds_verified_route(
    position_service: tuple[Position, str],
    field: str,
    value: str,
    tool: str,
    args: dict[str, Any],
    command: list[str],
) -> None:
    position, base_url = position_service
    position.preview_inputs = True
    if args.get("cancel_order"):
        position.trade_type = "limit"
    cli = CliRunner()
    cli_args = position_cli_args(base_url, command)

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, f"preview_{tool}", args)
            cli_preview = cli.invoke(main, [*cli_args, "--dry-run"])
            assert cli_preview.exit_code == 0, cli_preview.output
            digest = preview["previewDigest"]
            assert json.loads(cli_preview.stdout)["previewDigest"] == digest
            assert preview["positionRoute"] == {
                "chainId": CHAIN_ID,
                "contractAddress": "nibi1contract",
            }
            trade_args = {**args, "preview_digest": digest}
            cli_submit_args = cli_args
            if args.get("order_type") == "market":
                trade_args["reference_price"] = preview["referencePrice"]
                cli_submit_args = [*cli_args, "--price", preview["referencePrice"]]

            setattr(position, field, value)
            cli_refusal = cli.invoke(
                main, [*cli_submit_args, "--preview-digest", digest]
            )
            assert cli_refusal.exit_code == ExitCode.REJECTED, cli_refusal.output
            cli_error = json.loads(cli_refusal.stdout)["error"]
            mcp_error = await call(client, f"trade_{tool}", trade_args)
            assert cli_error["code"] == mcp_error["code"] == "PREVIEW_MISMATCH"
            assert position.submitted == []
            assert not (storage.home() / "intents.sqlite3").exists()

            if field == "contract_address" and tool != "open":
                position.contract_position_unavailable = True
                cli_refusal = cli.invoke(main, [*cli_args, "--preview-digest", digest])
                assert cli_refusal.exit_code == ExitCode.REJECTED, cli_refusal.output
                assert (
                    json.loads(cli_refusal.stdout)["error"]["code"]
                    == "PREVIEW_MISMATCH"
                )
                missing_target = await call(client, f"trade_{tool}", trade_args)
                assert missing_target["code"] == "PREVIEW_MISMATCH"
                assert "preview" not in missing_target
                assert position.submitted == []
                assert not (storage.home() / "intents.sqlite3").exists()
                position.contract_position_unavailable = False

            if field == "lcd_chain_id":
                assert "Use an LCD on Keeper's chain" in cli_error["message"]
                assert "preview" not in mcp_error
                setattr(position, field, None)
                accepted = await call(client, f"trade_{tool}", trade_args)
                assert accepted["status"] == "accepted", accepted
            else:
                fresh = await call(client, f"preview_{tool}", args)
                assert fresh["previewDigest"] == mcp_error["preview"]["previewDigest"]
                assert fresh["previewDigest"] != digest
                assert fresh["intent"] == preview["intent"]
                assert fresh["positionRoute"] == {
                    "chainId": position.chain_id,
                    "contractAddress": position.contract_address,
                }
                accepted = cli.invoke(
                    main, [*cli_submit_args, "--preview-digest", fresh["previewDigest"]]
                )
                assert accepted.exit_code == 0, accepted.output
                assert json.loads(accepted.stdout)["status"] == "accepted"
                if field == "contract_address" and args.get("order_type") == "market":
                    blocked = await call(client, "trade_open", trade_args)
                    assert blocked["code"] == "PREVIEW_MISMATCH"
                    (pending,) = storage.list_pending(f"{base_url}/query", ADDR)
                    assert (
                        pending["intentKey"] == json.loads(accepted.stdout)["intentKey"]
                    )
            assert len(position.submitted) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("market_open", "switch_read", "perp_state"),
    [
        pytest.param(True, "switch_contract_on_market_read", "open", id="quote"),
        pytest.param(False, "switch_contract_on_market_read", "open", id="no-quote"),
        pytest.param(
            False, "switch_contract_on_perp_market_read", "open", id="fallback"
        ),
        pytest.param(
            False,
            "switch_contract_on_perp_market_read",
            "close_only",
            id="fallback-rejection",
        ),
    ],
)
def test_market_open_rechecks_route_after_price_refresh(
    position_service, market_open, switch_read, perp_state
):
    position, base_url = position_service
    position.preview_inputs = True
    args = {**OPEN_ARGS, "order_type": "market"}
    cli = CliRunner()
    cli_args = position_cli_args(base_url, [*OPEN_COMMAND, "--type", "market"])

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_open", args)
            digest = preview["previewDigest"]
            reference = preview["referencePrice"]

            position.keeper_market_open = market_open
            position.market_trading_state = perp_state
            setattr(position, switch_read, "nibi1replacement")
            cli_result = cli.invoke(
                main, [*cli_args, "--preview-digest", digest, "--price", reference]
            )
            assert cli_result.exit_code == ExitCode.REJECTED, cli_result.output
            cli_error = json.loads(cli_result.stdout)["error"]
            assert position.contract_address == "nibi1replacement"

            position.contract_address = preview["positionRoute"]["contractAddress"]
            setattr(position, switch_read, "nibi1replacement")
            mcp_error = await call(
                client,
                "trade_open",
                {**args, "preview_digest": digest, "reference_price": reference},
            )
            assert position.contract_address == "nibi1replacement"
            assert cli_error["code"] == mcp_error["code"] == "PREVIEW_MISMATCH"
            assert "details" not in cli_error and "preview" not in mcp_error
            assert position.submitted == []
            assert storage.list_pending(f"{base_url}/query", ADDR) == []

    asyncio.run(scenario())


def test_market_open_refuses_changed_route_without_a_current_price(position_service):
    position, base_url = position_service
    position.preview_inputs = True
    args = {**OPEN_ARGS, "order_type": "market"}
    cli = CliRunner()
    cli_args = position_cli_args(base_url, [*OPEN_COMMAND, "--type", "market"])

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_open", args)
            digest = preview["previewDigest"]
            reference = preview["referencePrice"]
            cli_submit_args = [
                *cli_args,
                "--preview-digest",
                digest,
                "--price",
                reference,
            ]
            trade_args = {
                **args,
                "preview_digest": digest,
                "reference_price": reference,
            }

            position.contract_address = "nibi1replacement"
            position.keeper_market_open = False
            cli_result = cli.invoke(main, cli_submit_args)
            assert cli_result.exit_code == ExitCode.REJECTED, cli_result.output
            cli_error = json.loads(cli_result.stdout)["error"]
            mcp_error = await call(client, "trade_open", trade_args)
            assert cli_error["code"] == mcp_error["code"] == "PREVIEW_MISMATCH"
            assert "details" not in cli_error and "preview" not in mcp_error
            assert position.submitted == []
            assert not (storage.home() / "intents.sqlite3").exists()

            position.contract_address = "nibi1contract"
            cli_result = cli.invoke(main, cli_submit_args)
            assert json.loads(cli_result.stdout)["error"]["code"] == "INVALID_REQUEST"
            assert (await call(client, "trade_open", trade_args))[
                "code"
            ] == "INVALID_REQUEST"
            assert position.submitted == []

    asyncio.run(scenario())


@pytest.mark.parametrize("reference", [None, "100"])
def test_cli_confirms_market_open_with_reference_and_slippage(
    position_service, reference
):
    position, base_url = position_service
    position.preview_inputs = True
    position.price = "100"
    save_session(f"{base_url}/query")
    cli = CliRunner()
    cli_args = position_cli_args(base_url, [*OPEN_COMMAND, "--type", "market"])
    price_flag = [] if reference is None else ["--price", reference]
    quoted = cli.invoke(main, [*cli_args, *price_flag, "--dry-run"])
    assert quoted.exit_code == 0, quoted.output
    preview = json.loads(quoted.stdout)
    fill = preview["estimates"]["worstCaseFill"]
    assert preview["referencePrice"] == fill["reference"]["price"] == "100"
    assert fill["status"] == "bound"
    assert fill["price"] == "102.01"

    submit_args = [
        *cli_args,
        "--preview-digest",
        preview["previewDigest"],
        "--price",
        preview["referencePrice"],
    ]
    position.price = "101.000000000000000001"
    refused = cli.invoke(main, submit_args)
    assert refused.exit_code == ExitCode.REJECTED, refused.output
    assert json.loads(refused.stdout)["error"]["code"] == "PRICE_MOVED"
    assert position.submitted == []
    assert not (storage.home() / "intents.sqlite3").exists()

    position.price = "100.01"
    accepted = cli.invoke(main, submit_args)
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["referencePrice"] == "100.01"
    (submitted,) = position.submitted
    assert submitted["body"]["intentPrice"] == "100.01"


@pytest.mark.parametrize("source", ["env", "file", "root-flag"])
@pytest.mark.parametrize(
    "command",
    [args for args in CLI_POSITION_ACTIONS if args[0] in {"margin", "partial-close"}],
    ids=["margin", "partial-close"],
)
def test_position_preview_applies_local_lcd_before_validation(
    position_service, tmp_path, monkeypatch, source, command
):
    position, base_url = position_service
    keeper_url = f"{base_url}/query"
    for spec in settings.SETTINGS:
        monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    save_session(keeper_url)
    inherited = "https://[invalid-lcd"
    flags = []
    if source == "env":
        monkeypatch.setenv("SAI_LCD_ENDPOINT", inherited)
    elif source == "file":
        (tmp_path / "config.toml").write_text(f'[lcd]\nendpoint = "{inherited}"\n')
    else:
        flags = ["--lcd-endpoint", inherited]

    result = CliRunner().invoke(
        main,
        [
            "--json",
            "--endpoint",
            keeper_url,
            *flags,
            "trade",
            *command,
            "--dry-run",
            "--lcd-endpoint",
            base_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["dryRun"] is True
    assert position.position_reads > 0
    assert not position.submitted


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


@pytest.mark.parametrize("market_visible", [True, False], ids=["visible", "hidden"])
def test_partial_close_allows_remainder_below_fee_schedule_minimum(
    position_service: tuple[Position, str], tmp_path, monkeypatch, market_visible: bool
) -> None:
    position, base_url = position_service
    position.preview_inputs = True
    position.keeper_market_visible = market_visible
    position.collateral = 100_000_000
    position.leverage = "2"
    position.keeper_minimum_position_microusd = 100_000_000
    position.minimum_position_microusd = "100000000"
    monkeypatch.setenv("SAI_HOME", str(tmp_path))

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(
                client, "preview_partial_close", {"trade_id": 7, "percent": 60}
            )
            result = CliRunner().invoke(
                main,
                position_cli_args(
                    base_url, ["partial-close", "7", "--percent", "60", "--dry-run"]
                ),
            )
            assert result.exit_code == 0, result.output
            assert json.loads(result.stdout)["intent"] == preview["intent"]
            assert preview["intent"]["body"]["collateral"]["amountUsdc"] == "60000000"
            assert preview["quote"]["closedNotionalUsd"] == "120"
            assert preview["estimates"]["fees"]["amountUsdc"] == "0.6"
            assert position.market_reads == 0
            assert position.submitted == []

    asyncio.run(scenario())


def test_partial_close_accepts_confirmed_reduction_after_collateral_price_moves(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    args = {"trade_id": 7, "percent": 50}

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_partial_close", args)
            position.collateral_price = "1.00119"
            fresh = await call(client, "preview_partial_close", args)
            assert fresh["quote"] == {
                **preview["quote"],
                "collateralPriceUsd": "1.00119",
                "closedNotionalUsd": "4.00476",
            }
            assert fresh["intent"] == preview["intent"]
            assert fresh["previewDigest"] == preview["previewDigest"]

            position.prices_on_position_read.extend([None, "1.0012"])
            accepted = await call(
                client,
                "trade_partial_close",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert accepted["status"] == "accepted", accepted
            (submitted,) = position.submitted
            assert submitted["body"] == preview["intent"]["body"]

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
            position.cap_query_heights.clear()
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
            expected_heights = [
                None,
                None,
                "121" if reported_height is not None else None,
                None,
                "121" if reported_height is not None else None,
            ]
            if tool == "margin" and reported_height is not None:
                expected_heights.append("121")
            assert position.cap_query_heights == expected_heights
            if reported_height is None:
                assert result["quoteStatus"] == "unavailable"
                assert result["recovery"]["action"] == "request_preview"
                assert "reports the contract query height" in result["error"]
                assert "preview" not in result
                assert "previewDigest" not in result
                position.reported_height = 121
                fresh = await call(client, f"preview_{tool}", args)
            else:
                assert result["quoteStatus"] == "available"
                assert result["recovery"]["action"] == "confirm_preview"
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
            assert accepted["status"] == "accepted"
            assert len(position.submitted) == 1

    asyncio.run(scenario())


def test_partial_close_refuses_replacement_when_pinned_cap_is_malformed(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    position.leverage = "10"
    position.after_hours = True
    args = {"trade_id": 7, "percent": 50}

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_partial_close", args)
            assert preview["quote"]["closedNotionalUsd"] == "10"
            # The cap becomes unreadable at the final pinned position read.
            position.caps_on_position_read.extend([None, None, "malformed"])
            refused = await call(
                client,
                "trade_partial_close",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert refused["code"] == "PREVIEW_MISMATCH"
            assert refused["quoteStatus"] == "unavailable"
            assert refused["recovery"]["action"] == "request_preview"
            assert "effective leverage" in refused["error"]
            assert "preview" not in refused
            assert "previewDigest" not in refused
            assert position.cap_query_heights.count("121") == 2
            assert position.submitted == []

            estimate = await call(client, "preview_partial_close", args)
            assert estimate["quote"]["effectiveLeverage"] is None
            assert estimate["quote"]["closedNotionalUsd"] == "20"
            assert estimate["quote"]["closedNotionalIsEstimate"] is True

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
            assert result["status"] == "accepted", result
            (submitted,) = position.submitted
            assert submitted["body"] == preview["intent"]["body"]
            assert position.price_query_heights == [None, None, None]
            assert position.perp_price_query_heights == ["121"]
            fresh = await call(client, "preview_partial_close", args)
            assert fresh["quote"]["collateralPriceUsd"] == "0.98"
            assert fresh["quote"]["closedNotionalUsd"] == "245"
            assert fresh["previewDigest"] == preview["previewDigest"]

    asyncio.run(scenario())


def test_partial_close_accepts_collateral_price_update_at_publication_time_read(
    position_service,
):
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral = 100_000_000
    position.leverage = "10"
    position.after_hours = True
    args = {"trade_id": 7, "percent": 50}

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_partial_close", args)
            assert preview["quote"]["closedNotionalUsd"] == "250"
            position.reported_height = 121
            position.collateral_price_on_perp_read = "0.98"

            accepted = await call(
                client,
                "trade_partial_close",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert accepted["status"] == "accepted", accepted
            (submitted,) = position.submitted
            assert submitted["body"] == preview["intent"]["body"]
            assert position.perp_price_query_heights == ["120", "121"]
            assert all(height is None for height in position.price_query_heights)
            fresh = await call(client, "preview_partial_close", args)
            assert fresh["quote"]["closedNotionalUsd"] == "245"
            assert fresh["previewDigest"] == preview["previewDigest"]

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


def test_partial_close_binds_cap_and_quotes_with_contract_rounding(
    position_service: tuple[Position, str],
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
            position.cap = "0"
            result = await call(
                client,
                "trade_partial_close",
                {**args, "preview_digest": preview["previewDigest"]},
            )
            assert result.get("code") == "PREVIEW_MISMATCH", result
            assert position.submitted == []
            assert result["preview"]["intent"] == preview["intent"]
            assert result["preview"]["quote"]["closedNotionalUsd"] == "0"
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


def test_partial_close_requires_fresh_confirmation_after_collateral_price_recovers(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral_price = None
    args = {"trade_id": 7, "percent": 50}

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            outage = await call(client, "preview_partial_close", args)
            assert outage["quote"]["effectiveLeverage"] == "2"
            assert outage["quote"]["closedNotionalUsd"] is None
            assert outage["quote"]["closedNotionalIsEstimate"] is True
            assert "previewDigest" not in outage
            outage_digest = confirmation.preview_digest(outage)
            position.collateral_price = "1.00119"
            refused = await call(
                client,
                "trade_partial_close",
                {**args, "preview_digest": outage_digest},
            )
            assert refused.get("code") == "PREVIEW_MISMATCH", refused
            assert position.submitted == []
            fresh = refused["preview"]
            assert fresh["quote"]["closedNotionalUsd"] == "4.00476"
            assert fresh["quote"]["closedNotionalIsEstimate"] is False
            assert fresh["intent"] == outage["intent"]
            assert fresh["previewDigest"] != outage_digest
            accepted = await call(
                client,
                "trade_partial_close",
                {**args, "preview_digest": fresh["previewDigest"]},
            )
            assert accepted["status"] == "accepted", accepted
            (submitted,) = position.submitted
            assert submitted["body"] == fresh["intent"]["body"]

    asyncio.run(scenario())


def test_partial_close_preview_omits_digest_until_effective_leverage_is_readable(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    position.leverage = "10"
    position.after_hours = True
    position.cap = "malformed"
    args = {"trade_id": 7, "percent": 50}

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            estimate = await call(client, "preview_partial_close", args)
            assert estimate["addr"] == ADDR
            assert estimate["quote"]["effectiveLeverage"] is None
            assert estimate["quote"]["collateralPriceUsd"] == "1"
            assert estimate["quote"]["closedNotionalUsd"] == "20"
            assert estimate["quote"]["closedNotionalIsEstimate"] is True
            assert "previewDigest" not in estimate

            position.cap = "5"
            fresh = await call(client, "preview_partial_close", args)
            assert fresh["quote"]["effectiveLeverage"] == "5"
            assert fresh["quote"]["closedNotionalUsd"] == "10"
            assert len(fresh["previewDigest"]) == 64
            assert position.submitted == []

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
    ("slippage", "accepted"),
    [
        ("0", False),
        ("0.0", False),
        ("0.00000000000000001", False),
        ("0.0000000000000001", True),
        ("0.000000000000000100", True),
        ("100", True),
        ("100.00000000000000000", True),
        ("100.0000000000000000000", False),
        ("100.0000000000000001", False),
    ],
)
@pytest.mark.parametrize("order_type", ["market", "limit", "stop"])
def test_open_checks_slippage_before_preview(
    position_service: tuple[Position, str],
    tmp_path,
    monkeypatch,
    slippage: str,
    accepted: bool,
    order_type: str,
) -> None:
    position, base_url = position_service
    position.preview_inputs = True
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    args = {
        "market_id": 0,
        "side": "long",
        "order_type": order_type,
        "amount": "10",
        "leverage": "2",
        "slippage": slippage,
    }
    price_options = []
    trade_args = {"preview_digest": "0" * 64}
    if order_type == "market":
        trade_args["reference_price"] = "100.5"
    else:
        args["price"] = "100"
        price_options = ["--price", "100"]
    cli = CliRunner().invoke(
        main,
        [
            "--json",
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
            order_type,
            "--amount",
            "10",
            "--leverage",
            "2",
            "--slippage",
            slippage,
            *price_options,
            "--dry-run",
        ],
    )

    async def scenario() -> None:
        async with connected_client(base_url) as client:
            preview = await call(client, "preview_open", args)
            if accepted:
                assert cli.exit_code == 0, cli.output
                assert json.loads(cli.stdout)["intent"]["body"]["maxSlippagePct"] == (
                    slippage
                )
                assert preview["intent"]["body"]["maxSlippagePct"] == slippage
                if order_type == "market":
                    assert preview["referencePrice"] == "100.5"
            else:
                assert cli.exit_code == 2, cli.output
                error = json.loads(cli.stdout)["error"]
                assert error["code"] == "INVALID_REQUEST"
                assert "Slippage" in error["message"]
                assert preview["code"] == "INVALID_REQUEST"
                assert preview["error"] == error["message"]
                trade = await call(
                    client,
                    "trade_open",
                    {**args, **trade_args},
                )
                assert trade == preview
                assert position.market_reads == 0
            assert position.submitted == []

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


def test_malformed_lcd_keeps_the_limit_preview_without_a_digest(position_service):
    position, base_url = position_service
    position.preview_inputs = True
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
            verified = await call(client, "preview_open", args)
            position.malformed_node_info = True
            quote = await call(client, "preview_open", args)
            assert quote["estimates"]["fees"]["status"] == "unavailable"
            assert "previewDigest" not in quote
            assert "positionRoute" not in quote
            refusal = await call(
                client,
                "trade_open",
                {**args, "preview_digest": verified["previewDigest"]},
            )
            assert refusal["code"] == "INVALID_REQUEST"
            assert position.submitted == []
            assert not (storage.home() / "intents.sqlite3").exists()

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


def test_close_previews_withhold_estimates_and_digests_without_oracle_time(
    position_service,
):
    position, base_url = position_service
    position.preview_inputs = True
    position.price = "95"
    position.liquidation_threshold = "96"
    position.oracle_base_age = None

    def assert_unavailable(preview):
        execution = preview["estimates"]["execution"]
        assert execution["status"] == "unavailable"
        assert execution["code"] == "OraclePublicationTimeUnknown"
        assert preview["estimates"]["fees"]["status"] == "unavailable"
        assert "previewDigest" not in preview

    async def scenario():
        async with connected_client(base_url) as client:
            for action, options in (("close", {}), ("partial_close", {"percent": 10})):
                args = {"trade_id": 7, **options}
                assert_unavailable(await call(client, f"preview_{action}", args))
                refused = await call(
                    client, f"trade_{action}", {**args, "preview_digest": "wrong"}
                )
                assert refused["code"] == "PREVIEW_MISMATCH"
                assert_unavailable(refused["preview"])
            result = CliRunner().invoke(
                main,
                [
                    "--endpoint",
                    f"{base_url}/query",
                    "--lcd-endpoint",
                    base_url,
                    "--json",
                    "trade",
                    "partial-close",
                    "7",
                    "--percent",
                    "10",
                    "--dry-run",
                ],
            )
            assert result.exit_code == 0, result.output
            assert_unavailable(json.loads(result.stdout))
            assert position.submitted == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("action", "options"), [("close", {}), ("partial_close", {"percent": 10})]
)
def test_close_digest_is_refused_when_oracle_time_disappears(
    position_service, action, options
):
    position, base_url = position_service
    position.preview_inputs = True

    async def scenario():
        args = {"trade_id": 7, **options}
        async with connected_client(base_url) as client:
            preview = await call(client, f"preview_{action}", args)
            digest = preview["previewDigest"]
            position.oracle_base_age = None

            refusal = await call(
                client, f"trade_{action}", {**args, "preview_digest": digest}
            )
            assert refusal["code"] == "PREVIEW_MISMATCH"
            assert "publication time is unknown" in refusal["error"]
            assert refusal["quoteStatus"] == "unavailable"
            assert refusal["recovery"]["action"] == "request_preview"
            assert refusal["preview"] == await call(client, f"preview_{action}", args)
            assert "previewDigest" not in refusal["preview"]
            assert position.submitted == []

    asyncio.run(scenario())


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize(
    ("action", "options"), [("close", {}), ("partial_close", {"percent": 10})]
)
def test_close_digest_is_refused_when_market_price_disappears(
    position_service, action, options, queued
):
    position, base_url = position_service
    position.preview_inputs = True
    position.fill_at_next_oracle_price = queued

    async def scenario():
        args = {"trade_id": 7, **options}
        async with connected_client(base_url) as client:
            dated = await call(client, f"preview_{action}", args)
            position.null_market_price = True
            position.market_order_config_fails = queued
            refused = await call(
                client,
                f"trade_{action}",
                {**args, "preview_digest": dated["previewDigest"]},
            )
            assert refused["code"] == "PREVIEW_MISMATCH"
            assert refused["preview"] == await call(client, f"preview_{action}", args)
            assert refused["quoteStatus"] == "unavailable"
            assert refused["recovery"]["action"] == "request_preview"
            assert "previewDigest" not in refused["preview"]
            assert refused["preview"]["estimates"]["execution"]["code"] == (
                "OraclePublicationTimeUnknown"
            )
            assert position.submitted == []

    asyncio.run(scenario())


def test_full_close_digest_is_refused_when_lcd_height_disappears(position_service):
    position, base_url = position_service
    position.preview_inputs = True
    args = {"trade_id": 7}

    async def scenario():
        async with connected_client(base_url) as client:
            dated = await call(client, "preview_close", args)
            digest = dated["previewDigest"]
            position.reported_height = None

            refused = await call(
                client, "trade_close", {**args, "preview_digest": digest}
            )
            assert position.submitted == []
            assert refused["code"] == "PREVIEW_MISMATCH"
            assert refused["quoteStatus"] == "unavailable"
            assert refused["recovery"]["action"] == "request_preview"
            replacement = refused["preview"]
            assert "previewDigest" not in replacement
            assert replacement == await call(client, "preview_close", args)

            cli_refusal = CliRunner().invoke(
                main,
                [
                    *position_cli_args(base_url, ["close", "7"]),
                    "--preview-digest",
                    digest,
                ],
            )
            assert cli_refusal.exit_code == ExitCode.REJECTED, cli_refusal.output
            cli_error = json.loads(cli_refusal.stdout)["error"]
            assert cli_error["code"] == "PREVIEW_MISMATCH"
            assert cli_error["details"]["preview"] == replacement
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


@pytest.mark.parametrize(("price", "age"), [("95", 900), ("95", 100), ("99", 100)])
def test_queued_closes_defer_settlement_to_the_next_tick(position_service, price, age):
    position, base_url = position_service
    position.preview_inputs = True
    position.leverage = "20"
    position.price = price
    position.liquidation_threshold = "96"
    position.oracle_base_age = age
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
                assert result["liquidationPrice"]["status"] == "unavailable"

    asyncio.run(scenario())


@pytest.mark.parametrize("order_type", ["market", "limit", "stop"])
def test_open_previews_follow_perp_market_state(position_service, order_type):
    position, base_url = position_service
    position.preview_inputs = True
    position.keeper_market_open = order_type == "market"
    position.market_trading_state = "open" if order_type == "market" else "closed"
    args = {
        "market_id": 0,
        "side": "long",
        "order_type": order_type,
        "amount": "10",
        "leverage": "2",
        "slippage": "1",
    }
    if order_type != "market":
        args["price"] = "100"

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(client, "preview_open", args)
            assert quote["estimates"]["fees"]["status"] == "estimate"
            assert quote["estimates"]["worstCaseFill"]["status"] == "bound"
            position.market_trading_state = "close_only"
            for indexed_open in (True, False):
                position.keeper_market_open = indexed_open
                quote = await call(client, "preview_open", args)
                assert_rejected_preview(quote, "MarketClosed", "close-only")

            if order_type == "market":
                position.market_trading_state = "closed"
                quote = await call(client, "preview_open", args)
                assert_rejected_preview(quote, "MarketClosed", "closed market")
            assert position.submitted == []

    asyncio.run(scenario())


def test_signed_out_open_reports_rejection_without_connect_action(
    position_service: tuple[Position, str],
) -> None:
    position, base_url = position_service
    position.preview_inputs = True
    position.market_trading_state = "close_only"
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
            assert storage.delete_session(ADDR) == ADDR
            preview = await call(client, "preview_open", args)
            assert preview["addr"] is None
            assert preview["confirmationStatus"] == "informational"
            assert_rejected_preview(preview, "MarketClosed", "close-only")
            assert "recovery" not in preview
            assert position.submitted == []

    asyncio.run(scenario())


def assert_rejected_preview(quote: dict[str, Any], code: str, reason: str) -> None:
    assert "quote" not in quote
    assert "previewDigest" not in quote
    estimates = quote["estimates"]
    execution = estimates["execution"]
    assert execution["status"] == "would_be_rejected"
    assert execution["code"] == code
    assert reason in execution["reason"]
    for key in ("fees", "liquidationPrice", "worstCaseFill"):
        assert estimates[key] == {
            "status": "unavailable",
            "reason": execution["reason"],
        }


@pytest.mark.parametrize(
    ("price", "queued", "age", "code"),
    [
        ("97", False, 100, "LiquidationReached"),
        ("98.4", False, None, "LiquidationReached"),
        ("97", False, 900, "OraclePriceStale"),
        ("99", False, 301, "OraclePriceStale"),
        ("99", False, 300, None),
        ("99", False, None, None),
        ("97", True, None, None),
        ("97", True, 900, None),
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
                reason = (
                    "stale oracle"
                    if code == "OraclePriceStale"
                    else "prospective liquidation threshold"
                )
                assert_rejected_preview(quote, code, reason)
            else:
                assert liq["status"] == "estimate"
                assert liq["price"] == "98.4"
                assert "borrowing-inclusive" in liq["assumption"]
                if queued:
                    assert estimates["execution"]["status"] == "unavailable"
                    assert "fresh oracle update" in estimates["execution"]["reason"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    (
        "action",
        "args",
        "price",
        "age",
        "close_only",
        "code",
        "reason",
    ),
    [
        (
            "margin",
            {"direction": "remove", "amount": "50"},
            "95",
            100,
            False,
            "LiquidationReached",
            "prospective liquidation threshold",
        ),
        (
            "leverage",
            {"leverage": "5"},
            "99",
            100,
            False,
            "InsufficientDepositBalance",
            "100.000000 USDC",
        ),
        (
            "margin",
            {"direction": "add", "amount": "100"},
            "99",
            100,
            False,
            "InsufficientDepositBalance",
            "100.000000 USDC",
        ),
        (
            "margin",
            {"direction": "remove", "amount": "50"},
            "99",
            301,
            False,
            "OraclePriceStale",
            "stale oracle",
        ),
        (
            "margin",
            {"direction": "remove", "amount": "50"},
            "99",
            301,
            True,
            "MarketClosed",
            "close-only",
        ),
        (
            "leverage",
            {"leverage": "5"},
            "99",
            301,
            True,
            "MarketClosed",
            "close-only",
        ),
    ],
)
def test_cli_and_mcp_have_the_same_position_rejection(
    position_service, action, args, price, age, close_only, code, reason
):
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral = 100_000_000
    position.leverage = "10"
    position.price = price
    position.available_balance = 100_000_000
    position.pinned_balance = 0
    position.oracle_base_age = age
    if close_only:
        position.keeper_market_open = False
        position.market_trading_state = "close_only"

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(client, f"preview_{action}", {"trade_id": 7, **args})
            assert_rejected_preview(quote, code, reason)

    asyncio.run(scenario())
    flags = []
    for name, value in args.items():
        flags.extend((f"--{name}", value))
    result = CliRunner().invoke(
        main,
        [
            "--json",
            "--endpoint",
            f"{base_url}/query",
            "--lcd-endpoint",
            base_url,
            "trade",
            action,
            "7",
            *flags,
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert_rejected_preview(json.loads(result.stdout), code, reason)
    assert position.submitted == []


def test_margin_refuses_position_drift_during_rejection_check(position_service):
    position, base_url = position_service
    position.preview_inputs = True

    async def scenario():
        async with connected_client(base_url) as client:
            request = {"trade_id": 7, "direction": "add", "amount": "1"}
            quote = await call(client, "preview_margin", request)
            # Both rebuilt quotes and the position comparison still match.
            # The rejection check's own position read sees the resized trade.
            position.collateral_on_position_read.extend([None, None, None, 3_000_000])
            result = await call(
                client,
                "trade_margin",
                {**request, "preview_digest": quote["previewDigest"]},
            )
            assert result["code"] == "PREVIEW_MISMATCH"
            assert "final preview check" in result["error"]
            assert position.submitted == []

    asyncio.run(scenario())


@pytest.mark.parametrize(("target", "required"), [("5", "100"), ("4", "150")])
def test_leverage_submit_rechecks_balance_after_confirmation(
    position_service, target, required
):
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral = 100_000_000
    position.leverage = "10"
    position.pinned_balance = 100_000_000

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(
                client, "preview_leverage", {"trade_id": 7, "leverage": "5"}
            )
            assert quote["estimates"]["liquidationPrice"]["status"] == "estimate"
            assert quote["previewDigest"]

            position.pinned_balance = 0
            refused = await call(
                client,
                "trade_leverage",
                {
                    "trade_id": 7,
                    "leverage": target,
                    "preview_digest": quote["previewDigest"],
                },
            )
            assert refused["code"] == "PREVIEW_MISMATCH"
            assert refused["quoteStatus"] == "unavailable"
            assert refused["recovery"]["action"] == "request_preview"
            assert_rejected_preview(
                refused["preview"],
                "InsufficientDepositBalance",
                f"{required}.000000 USDC",
            )
            assert (
                refused["error"]
                == refused["preview"]["estimates"]["execution"]["reason"]
            )
            assert position.submitted == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("leverage", {"leverage": "5"}),
        ("margin", {"direction": "add", "amount": "100"}),
    ],
)
def test_queued_leverage_warns_about_funding_at_fill(position_service, tool, args):
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral = 100_000_000
    position.leverage = "10"
    position.available_balance = 99_000_000
    position.fill_at_next_oracle_price = True

    async def scenario():
        async with connected_client(base_url) as client:
            request = {"trade_id": 7, **args}
            quote = await call(client, f"preview_{tool}", request)
            execution = quote["estimates"]["execution"]
            assert execution["status"] == "unavailable"
            assert "100.000000 USDC" in execution["warning"]
            assert "99.000000 USDC" in execution["warning"]
            assert "Perp checks funding at fill" in execution["warning"]
            result = await call(
                client,
                f"trade_{tool}",
                {**request, "preview_digest": quote["previewDigest"]},
            )
            assert result["status"] == "accepted"
            assert len(position.submitted) == 1
            assert position.submitted[0]["body"] == quote["intent"]["body"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("price", "queued", "rejected"),
    [
        ("90", False, True),
        ("92.2", False, True),
        ("93", False, False),
        ("90", True, False),
    ],
)
def test_leverage_checks_stored_liquidation_before_after_hours_cap(
    position_service, price, queued, rejected
):
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral = 100_000_000
    position.leverage = "10"
    position.after_hours = True
    position.cap = "5"
    position.price = price
    position.oracle_base_age = 100
    position.liquidation_threshold = "92.2"
    position.fill_at_next_oracle_price = queued

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(
                client,
                "preview_margin",
                {"trade_id": 7, "direction": "remove", "amount": "50"},
            )
            if rejected:
                assert_rejected_preview(quote, "LiquidationReached", "stored leverage")
            else:
                assert (
                    "after-hours guard"
                    in quote["estimates"]["liquidationPrice"]["reason"]
                )
                assert quote["previewDigest"]
            assert position.submitted == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("price", "queued", "rejected", "quote_error_height"),
    [
        ("96.10", False, True, None),
        ("96.20", False, True, 120),
        ("96.21", False, False, None),
        ("96.10", True, False, None),
        ("96.10", False, False, 121),
    ],
)
def test_margin_checks_perps_borrowing_inclusive_leverage_quote(
    position_service, price, queued, rejected, quote_error_height
):
    position, base_url = position_service
    position.preview_inputs = True
    position.collateral = 100_000_000
    position.leverage = "10"
    position.price = price
    position.oracle_base_age = 100
    # The 20x position has $50 of collateral. With $2 of accrued borrowing,
    # Perp's liquidation threshold moves from $96 to $96.20.
    position.quoted_liquidation_threshold = "96.20"
    position.quote_error_height = quote_error_height
    position.fill_at_next_oracle_price = queued

    async def scenario():
        async with connected_client(base_url) as client:
            quote = await call(
                client,
                "preview_margin",
                {"trade_id": 7, "direction": "remove", "amount": "50"},
            )
            if rejected:
                assert_rejected_preview(
                    quote, "LiquidationReached", "prospective liquidation threshold"
                )
            else:
                assert quote["estimates"]["liquidationPrice"]["price"] == "96"
                assert quote["previewDigest"]
            assert position.submitted == []

    asyncio.run(scenario())
