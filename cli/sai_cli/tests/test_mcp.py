"""Sai MCP server: annotations, tool listing, sign-in, tool calls, errors,
and secrets."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

import pytest
from click.testing import CliRunner
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent
from sai_cli.core import confirmation, keeper, margin, settings, storage, stream, update
from sai_cli.core import log as sai_log
from sai_cli.core import session as sessions
from sai_cli.mcp_server import info as sai_info
from sai_cli.mcp_server import server, tools
from sai_cli.sai_cli import main

ADDR = "0x" + "b" * 40
OTHER = "0x" + "c" * 40
ENDPOINT = keeper.MAINNET_HTTP
TOKEN = "header.secret-session-token-value.signature"

TRADE_TOOLS = {
    "trade_open",
    "trade_close",
    "trade_tpsl",
    "trade_leverage",
    "trade_margin",
    "trade_partial_close",
}
READ_TOOLS = {
    "mcp_info",
    "auth_status",
    "auth_list",
    "auth_use",
    "market_list",
    "market_prices",
    "account_positions",
    "account_history",
    "account_balances",
    "account_overview",
    "intent_local",
    "intent_status",
    "preview_open",
    "preview_close",
    "preview_tpsl",
    "preview_leverage",
    "preview_margin",
    "preview_partial_close",
    "logs_intent",
    "intent_wait",
}
LOCAL_TOOLS = {"mcp_info", "auth_status", "auth_list", "auth_use", "intent_local"}
# Changes only which connected account is active on this machine.
SWITCH_TOOLS = {"auth_use"}


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    for name in (
        "SAI_ENDPOINT",
        "SAI_WS_ENDPOINT",
        "SAI_LCD_ENDPOINT",
        "SAI_UPDATE_AUTO",
    ):
        monkeypatch.delenv(name, raising=False)


def stored_session(hours: float = 1, endpoint: str = ENDPOINT) -> dict[str, str]:
    return {
        "token": TOKEN,
        "evmAddress": ADDR,
        "endpoint": endpoint,
        "expiresAt": (datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
    }


def text_of(content: Any) -> str:
    block = content[0]
    assert isinstance(block, TextContent)
    return block.text


def invoke(mcp: FastMCP, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call a tool through FastMCP and return its decoded JSON text."""
    result = asyncio.run(mcp.call_tool(name, arguments or {}))
    text = text_of(result[0] if isinstance(result, tuple) else result)
    assert TOKEN not in text
    return json.loads(text)


class FakeKeeper:
    """Answer keeper.request by query and record calls and submissions."""

    def __init__(self, answers: dict[str, Any] | None = None, lose_ack=False):
        self.answers = answers or {}
        self.lose_ack = lose_ack
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.submitted: list[dict[str, Any]] = []

    def __call__(
        self,
        endpoint: str,
        query: str,
        variables: dict[str, Any] | None = None,
        token: str | None = None,
        timeout: float = 20,
        *,
        attempt: keeper.RequestAttempt | None = None,
    ) -> dict[str, Any]:
        self.calls.append((query, variables))
        if query == keeper.SUBMIT:
            if attempt is not None:
                attempt.transport_started = True
            assert token == TOKEN and variables is not None
            envelope = variables["input"]
            self.submitted.append(envelope)
            if self.lose_ack:
                raise keeper.KeeperError(
                    "Keeper request failed or returned invalid JSON"
                )
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


@pytest.fixture
def mcp() -> FastMCP:
    return server.create_server()


# Schema and listing


def test_every_tool_has_a_description_and_the_right_annotations(mcp):
    registered = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert set(registered) == READ_TOOLS | TRADE_TOOLS
    for name, tool in registered.items():
        hints = tool.annotations
        assert tool.description, name
        assert hints is not None and hints.title, name
        if name in TRADE_TOOLS:
            assert hints.readOnlyHint is False, name
            assert hints.destructiveHint is True, name
            assert hints.idempotentHint is False, name
            assert hints.openWorldHint is True, name
        elif name in SWITCH_TOOLS:
            assert hints.readOnlyHint is False, name
            assert hints.destructiveHint is False, name
            assert hints.idempotentHint is True, name
            assert hints.openWorldHint is False, name
        else:
            assert hints.readOnlyHint is True, name
            assert hints.destructiveHint is False, name
            assert hints.idempotentHint is True, name
            assert hints.openWorldHint is (name not in LOCAL_TOOLS), name


def test_no_tool_accepts_a_token_or_passphrase(mcp):
    for tool in mcp._tool_manager.list_tools():
        for field in tool.parameters.get("properties", {}):
            assert "token" not in field and "pass" not in field, tool.name


def names(mcp: FastMCP) -> set[str]:
    return {tool.name for tool in mcp._tool_manager.list_tools()}


async def client_listing(mcp: FastMCP) -> tuple[list[str], dict[str, Any]]:
    """List tools and call mcp_info through an in-memory MCP client."""
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        listed = await client.list_tools()
        called = await client.call_tool("mcp_info", {})
    return [tool.name for tool in listed.tools], json.loads(text_of(called.content))


def test_every_tool_is_listed_signed_in_or_out(mcp):
    """Agents doing discovery see one stable tool set, trade tools included,
    whatever the sign-in state."""
    signed_out, info = asyncio.run(client_listing(mcp))
    assert set(signed_out) == READ_TOOLS | TRADE_TOOLS
    assert info["trading"] == {
        "available": False,
        "reason": "signed-out",
        "addr": None,
    }
    assert "exposedTools" not in info

    storage.save_session(stored_session())
    signed_in, info = asyncio.run(client_listing(mcp))
    assert signed_in == signed_out
    assert info["trading"] == {
        "available": True,
        "reason": "signed-in",
        "addr": ADDR,
    }
    assert info["tools"] == sorted(signed_in)


@pytest.mark.parametrize(
    ("session", "reason", "code"),
    (
        (None, "signed-out", "SIGNED_OUT"),
        (stored_session(hours=-1), "expired", "SESSION_EXPIRED"),
        (
            stored_session(endpoint="http://127.0.0.1:9/query"),
            "other-endpoint",
            "SESSION_UNAVAILABLE",
        ),
    ),
)
def test_trade_without_a_usable_session_submits_nothing(
    mcp, monkeypatch, session, reason, code
):
    """Each trade tool refuses before any preview rebuild or Keeper call and
    tells the agent how the user signs in."""
    if session is not None:
        storage.save_session(session)
    fake = FakeKeeper({keeper.MARKETS: markets()})
    monkeypatch.setattr(keeper, "request", fake)
    trading = invoke(mcp, "mcp_info")["trading"]
    assert trading["available"] is False and trading["reason"] == reason
    for name, arguments in TRADES:
        error = invoke(mcp, name, {**arguments, "preview_digest": "0" * 64})
        assert error["code"] == code, name
        assert "sai auth login" in error["error"], name
        assert "sai auth import" in error["error"], name
    assert fake.calls == []
    assert storage.list_pending(ENDPOINT, ADDR) == []


MALFORMED = ({}, {"trade_id": "seven"}, {"preview_digest": 5, "extra": True})


@pytest.mark.parametrize(
    ("session", "code"),
    (
        (None, "SIGNED_OUT"),
        (stored_session(hours=-1), "SESSION_EXPIRED"),
        (stored_session(endpoint="http://127.0.0.1:9/query"), "SESSION_UNAVAILABLE"),
    ),
)
def test_session_check_comes_before_argument_validation(
    mcp, monkeypatch, session, code
):
    """A malformed trade call without a usable session gets the sign-in
    error over MCP, not FastMCP's validation error."""
    if session is not None:
        storage.save_session(session)
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)

    async def calls() -> list[tuple[bool, dict[str, Any], Any]]:
        results = []
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            for name in sorted(TRADE_TOOLS):
                for arguments in MALFORMED:
                    called = await client.call_tool(name, arguments)
                    results.append(
                        (
                            called.isError,
                            json.loads(text_of(called.content)),
                            called.structuredContent,
                        )
                    )
        return results

    for is_error, error, structured in asyncio.run(calls()):
        assert is_error is False
        assert error["code"] == code
        assert "`sai auth login NAME` or `sai auth import`" in error["error"]
        assert structured == {"result": json.dumps(error)}
    assert fake.calls == []


def test_signed_in_malformed_trade_still_gets_validation_errors(mcp):
    storage.save_session(stored_session())

    async def call() -> Any:
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            return await client.call_tool("trade_close", {})

    called = asyncio.run(call())
    assert called.isError is True
    assert "trade_id" in text_of(called.content)


def test_signed_out_trade_close_over_the_client_returns_signed_out(mcp, monkeypatch):
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)

    async def close() -> dict[str, Any]:
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            called = await client.call_tool(
                "trade_close", {"trade_id": 7, "preview_digest": "0" * 64}
            )
        return json.loads(text_of(called.content))

    error = asyncio.run(close())
    assert error["code"] == "SIGNED_OUT"
    assert "`sai auth login NAME` or `sai auth import`" in error["error"]
    assert fake.calls == [] and fake.submitted == []


def served_settings(monkeypatch, flags) -> settings.Settings:
    """Run `server.serve(FLAGS)` without serving; return what it resolved."""
    seen: list[settings.Settings | None] = []

    class Idle:
        def run(self) -> None:
            pass

    def build(resolved: settings.Settings | None = None) -> Idle:
        seen.append(resolved)
        return Idle()

    monkeypatch.setattr(server, "create_server", build)
    server.serve(flags)
    assert len(seen) == 1 and seen[0] is not None
    return seen[0]


def test_serve_flags_win_over_the_environment_and_file(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        '[keeper]\nendpoint = "http://127.0.0.1:2/query"\n'
        '[lcd]\nendpoint = "http://127.0.0.1:1"\n'
    )
    monkeypatch.setenv("SAI_ENDPOINT", "http://127.0.0.1:9/query")
    flags = {
        "keeper.endpoint": "http://127.0.0.1:5/query",
        "keeper.ws_endpoint": "ws://127.0.0.1:6/query",
        "lcd.endpoint": "http://127.0.0.1:4",
    }
    resolved = served_settings(monkeypatch, flags)
    assert resolved.endpoint == "http://127.0.0.1:5/query"
    assert resolved.ws_endpoint == "ws://127.0.0.1:6/query"
    assert resolved.lcd_endpoint == "http://127.0.0.1:4"
    assert resolved.sources["keeper.endpoint"] == "flag"
    partial = served_settings(monkeypatch, {"lcd.endpoint": "http://127.0.0.1:3"})
    assert partial.endpoint == "http://127.0.0.1:9/query"
    assert partial.lcd_endpoint == "http://127.0.0.1:3"
    assert served_settings(monkeypatch, None).lcd_endpoint == "http://127.0.0.1:1"


def test_server_settings_resolve_once_from_the_environment(monkeypatch):
    server.create_server()
    config = server.server_settings()
    assert config.endpoint == ENDPOINT
    assert config.ws_endpoint == keeper.MAINNET_WS
    monkeypatch.setenv("SAI_ENDPOINT", "http://127.0.0.1:9/query")
    assert server.server_settings() is config
    monkeypatch.setenv("SAI_LCD_ENDPOINT", "http://127.0.0.1:8")
    server.create_server()
    config = server.server_settings()
    assert config.ws_endpoint == "ws://127.0.0.1:9/query"
    assert config.lcd_endpoint == "http://127.0.0.1:8"
    monkeypatch.setenv("SAI_WS_ENDPOINT", "ws://127.0.0.1:7/query")
    server.create_server()
    assert server.server_settings().ws_endpoint == "ws://127.0.0.1:7/query"


def test_server_reads_the_config_file_and_reports_its_path(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[keeper]\nendpoint = "http://127.0.0.1:9/query"\n'
    )
    info = invoke(server.create_server(), "mcp_info")
    assert info["endpoints"]["keeper"] == "http://127.0.0.1:9"
    assert info["endpoints"]["websocket"] == "ws://127.0.0.1:9"
    assert info["configFile"] == str(tmp_path / "config.toml")


def test_bad_config_file_is_a_tool_error_naming_the_path(tmp_path):
    (tmp_path / "config.toml").write_text("[keeper\n")
    error = invoke(server.create_server(), "market_list")
    assert error["code"] == "BAD_SETTINGS"
    assert str(tmp_path / "config.toml") in error["error"]


# Reads


def test_market_prices_returns_keeper_data(mcp, monkeypatch):
    prices = {"oracle": {"tokenPricesUsd": [{"priceUsd": 1.0}]}}
    fake = FakeKeeper({keeper.PRICES: prices})
    monkeypatch.setattr(keeper, "request", fake)
    assert invoke(mcp, "market_prices") == prices


def test_account_positions_defaults_to_the_session_addr(mcp, monkeypatch):
    fake = FakeKeeper({keeper.POSITIONS: {"perp": {"trades": []}}})
    monkeypatch.setattr(keeper, "request", fake)
    error = invoke(mcp, "account_positions")
    assert error["code"] == "SESSION_UNAVAILABLE"
    assert "Pass addr" in error["error"]
    assert fake.calls == []

    storage.save_session(stored_session())
    assert invoke(mcp, "account_positions") == {
        "addr": ADDR,
        "addrSource": "session",
        "sessionExpired": False,
        "endpointMatches": True,
        "perp": {"trades": []},
    }
    assert fake.calls[-1] == (keeper.POSITIONS, {"trader": ADDR})
    other = "0x" + "c" * 40
    assert invoke(mcp, "account_positions", {"addr": other}) == {
        "addr": other,
        "addrSource": "argument",
        "perp": {"trades": []},
    }
    assert fake.calls[-1] == (keeper.POSITIONS, {"trader": other})


def test_expired_session_still_supplies_the_addr_flagged(mcp, monkeypatch):
    storage.save_session(stored_session(hours=-1))
    fake = FakeKeeper({keeper.HISTORY: {"perp": {"tradeHistory": []}}})
    monkeypatch.setattr(keeper, "request", fake)
    result = invoke(mcp, "account_history")
    assert result["addr"] == ADDR
    assert result["addrSource"] == "session"
    assert result["sessionExpired"] is True


@pytest.mark.parametrize(
    "name",
    [
        "account_positions",
        "account_history",
        "account_balances",
        "intent_local",
        "intent_status",
    ],
)
def test_session_for_another_endpoint_is_refused_for_the_default_addr(
    mcp, monkeypatch, name
):
    storage.save_session(stored_session(endpoint="http://127.0.0.1:1/query"))
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    monkeypatch.setattr(stream, "snapshot", fake)
    error = invoke(mcp, name)
    assert error["code"] == "SESSION_UNAVAILABLE"
    assert "Pass addr explicitly" in error["error"]
    assert fake.calls == []


def test_account_balances_reads_one_snapshot(mcp, monkeypatch):
    seen: list[tuple[str, dict[str, Any]]] = []

    def snapshot(ws: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert query == stream.BALANCES
        seen.append((ws, variables))
        return {"data": {"userBalances": []}}

    monkeypatch.setattr(stream, "snapshot", snapshot)
    result = invoke(mcp, "account_balances", {"addr": ADDR})
    assert result == {
        "addr": ADDR,
        "addrSource": "argument",
        "data": {"userBalances": []},
    }
    assert seen == [(keeper.MAINNET_WS, {"user": ADDR})]


# Previews and trades


OPEN_ARGS = {
    "market_id": 0,
    "side": "long",
    "order_type": "limit",
    "amount": "10",
    "leverage": "2",
    "slippage": "1",
    "price": "100",
    "take_profit": "120",
}


def markets(price: float = 100.5) -> dict[str, Any]:
    return {
        "perp": {
            "borrowings": [
                {
                    "marketId": 0,
                    "price": price,
                    "isOpen": True,
                    "minPositionSizeUSD": 1,
                    "collateralToken": {"id": 1, "symbol": "USDC"},
                }
            ]
        }
    }


def snapshots(monkeypatch, *collaterals: str) -> None:
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
            {"chainId": "test-1", "contractAddress": "test-contract"},
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


def confirm(
    mcp: FastMCP, preview_tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Preview, and return the trade arguments that bind to the preview."""
    preview = invoke(mcp, preview_tool, arguments)
    assert preview["dryRun"] is True
    return {**arguments, "preview_digest": preview["previewDigest"]}


def test_preview_open_is_exactly_what_trade_open_submits(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_open", OPEN_ARGS)
    assert preview["dryRun"] is True
    assert preview["previewDigest"] == confirmation.preview_digest(preview)
    assert fake.submitted == []

    result = invoke(
        mcp, "trade_open", {**OPEN_ARGS, "preview_digest": preview["previewDigest"]}
    )
    assert result["status"] == "accepted"
    assert "not yet confirmed" in result["message"]
    assert fake.intent() == preview["intent"]
    assert fake.submitted[-1]["intentKey"] == result["intentKey"]


def test_digest_binds_account_and_reference_not_market_intent_price():
    def market(intent_price: str, reference: str, account: str) -> dict:
        return {
            "addr": account,
            "referencePrice": reference,
            "intent": {
                "kind": "OPEN_TRADE",
                "body": {"orderType": "market", "intentPrice": intent_price},
            },
        }

    base = confirmation.preview_digest(market("5", "100", ADDR))
    # The refreshed intent price, reference spelling, and addr case don't
    # change what the user confirmed.
    assert confirmation.preview_digest(market("6", "100.0", ADDR.upper())) == base
    assert confirmation.preview_digest(market("5", "101", ADDR)) != base
    assert confirmation.preview_digest(market("5", "100", OTHER)) != base
    limit = {"intent": {"kind": "OPEN_TRADE", "body": {"orderType": "limit"}}}
    assert confirmation.preview_digest(limit) != confirmation.preview_digest(
        {
            "intent": {
                "kind": "OPEN_TRADE",
                "body": {"orderType": "limit", "intentPrice": "5"},
            }
        }
    )
    assert len(confirmation.preview_digest(limit)) == 64


def test_trades_require_the_preview_digest(mcp):
    storage.save_session(stored_session())
    for name in TRADE_TOOLS:
        tool = mcp._tool_manager.get_tool(name)
        assert tool is not None
        assert "preview_digest" in tool.parameters["required"], name
    error = invoke(mcp, "trade_close", {"trade_id": 1, "preview_digest": ""})
    assert error["code"] == "INVALID_REQUEST"
    assert "previewDigest" in error["error"]


PREVIEWS = (
    ("preview_open", OPEN_ARGS),
    ("preview_open", {**OPEN_ARGS, "order_type": "market", "price": None}),
    ("preview_close", {"trade_id": 7}),
    ("preview_tpsl", {"trade_id": 7, "take_profit": "120"}),
    ("preview_leverage", {"trade_id": 7, "leverage": "3"}),
    ("preview_margin", {"trade_id": 7, "direction": "add", "amount": "1"}),
    ("preview_partial_close", {"trade_id": 7, "percent": 50}),
)


TRADES = (
    ("trade_open", OPEN_ARGS),
    (
        "trade_open",
        {**OPEN_ARGS, "order_type": "market", "price": None, "reference_price": "100"},
    ),
    ("trade_close", {"trade_id": 7}),
    ("trade_tpsl", {"trade_id": 7, "take_profit": "120"}),
    ("trade_leverage", {"trade_id": 7, "leverage": "3"}),
    ("trade_margin", {"trade_id": 7, "direction": "add", "amount": "1"}),
    ("trade_partial_close", {"trade_id": 7, "percent": 50}),
)


@pytest.mark.parametrize(("name", "arguments"), PREVIEWS)
def test_previews_never_submit(mcp, monkeypatch, name, arguments):
    storage.save_session(stored_session())
    fake = FakeKeeper({keeper.MARKETS: markets()})
    monkeypatch.setattr(keeper, "request", fake)
    snapshots(monkeypatch, "4000000")
    preview = invoke(mcp, name, arguments)
    assert preview["dryRun"] is True
    assert len(preview["previewDigest"]) == 64
    assert all(query != keeper.SUBMIT for query, _ in fake.calls)
    assert storage.list_pending(ENDPOINT, ADDR) == []


@pytest.mark.parametrize(
    ("name", "preview_args", "trade_args"),
    [
        ("close", {"trade_id": 7}, {"trade_id": 8}),
        (
            "leverage",
            {"trade_id": 7, "leverage": "3"},
            {"trade_id": 7, "leverage": "4"},
        ),
        (
            "tpsl",
            {"trade_id": 7, "take_profit": "120"},
            {"trade_id": 7, "take_profit": "121"},
        ),
        ("open", OPEN_ARGS, {**OPEN_ARGS, "amount": "11"}),
    ],
)
def test_trade_that_differs_from_its_preview_is_refused(
    mcp, monkeypatch, name, preview_args, trade_args
):
    storage.save_session(stored_session())
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    digest = confirm(mcp, f"preview_{name}", preview_args)["preview_digest"]
    error = invoke(mcp, f"trade_{name}", {**trade_args, "preview_digest": digest})
    assert error["code"] == "PREVIEW_MISMATCH"
    assert "confirmation again" in error["error"]
    fresh = invoke(mcp, f"preview_{name}", trade_args)
    assert error["preview"] == fresh
    assert fake.submitted == []
    assert storage.list_pending(ENDPOINT, ADDR) == []


MARKET_ARGS = {**OPEN_ARGS, "order_type": "market", "price": None}


def test_market_open_binds_to_the_reference_price(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper({keeper.MARKETS: markets(price=100)})
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_open", MARKET_ARGS)
    assert preview["requiresMarketPriceRefresh"] is True
    assert preview["referencePrice"] == "100"
    trade = {
        **MARKET_ARGS,
        "preview_digest": preview["previewDigest"],
        "reference_price": preview["referencePrice"],
    }

    missing = invoke(mcp, "trade_open", {**trade, "reference_price": None})
    assert missing["code"] == "INVALID_REQUEST"
    assert "referencePrice" in missing["error"]

    # Slippage is 1%: a 1.5% move is refused with a fresh preview.
    fake.answers[keeper.MARKETS] = markets(price=101.5)
    moved = invoke(mcp, "trade_open", trade)
    assert moved["code"] == "PRICE_MOVED"
    assert "1.5000%" in moved["error"]
    assert moved["preview"]["referencePrice"] == "101.5"
    assert moved["preview"]["previewDigest"] != preview["previewDigest"]
    assert fake.submitted == []

    # Retrying the old confirmation with the new price needs a new digest.
    retry = invoke(mcp, "trade_open", {**trade, "reference_price": "101.5"})
    assert retry["code"] == "PREVIEW_MISMATCH"
    assert retry["preview"]["previewDigest"] == moved["preview"]["previewDigest"]
    assert fake.submitted == []

    # A 0.5% move is within slippage and submits the refreshed price.
    fake.answers[keeper.MARKETS] = markets(price=100.5)
    result = invoke(mcp, "trade_open", trade)
    assert result["status"] == "accepted"
    assert result["referencePrice"] == "100.5"
    assert fake.intent()["body"]["intentPrice"] == "100.5"


def test_market_open_resubmits_only_with_the_new_confirmation(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper({keeper.MARKETS: markets(price=100)})
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_open", MARKET_ARGS)
    fake.answers[keeper.MARKETS] = markets(price=110)
    moved = invoke(
        mcp,
        "trade_open",
        {
            **MARKET_ARGS,
            "preview_digest": preview["previewDigest"],
            "reference_price": preview["referencePrice"],
        },
    )
    assert moved["code"] == "PRICE_MOVED"
    fresh = moved["preview"]
    result = invoke(
        mcp,
        "trade_open",
        {
            **MARKET_ARGS,
            "preview_digest": fresh["previewDigest"],
            "reference_price": fresh["referencePrice"],
        },
    )
    assert result["status"] == "accepted"
    assert fake.intent()["body"]["intentPrice"] == "110"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("close", {"trade_id": 7}),
        ("leverage", {"trade_id": 7, "leverage": "3"}),
        ("tpsl", {"trade_id": 7, "take_profit": "120"}),
        ("open", OPEN_ARGS),
    ],
)
def test_preview_is_bound_to_the_signed_in_account(mcp, monkeypatch, name, arguments):
    storage.save_session(stored_session())
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, f"preview_{name}", arguments)
    assert preview["addr"] == ADDR
    # The person signs in to another account before confirming.
    storage.save_session({**stored_session(), "evmAddress": OTHER})
    error = invoke(
        mcp,
        f"trade_{name}",
        {**arguments, "preview_digest": preview["previewDigest"]},
    )
    assert error["code"] == "PREVIEW_MISMATCH"
    assert error["preview"]["addr"] == OTHER
    assert fake.submitted == []
    assert storage.list_pending(ENDPOINT, OTHER) == []


OTHER_TOKEN = "header.other-account-session-token.signature"


def connect_two_accounts() -> None:
    """Connect ADDR, then OTHER, which becomes active."""
    storage.save_session(stored_session())
    storage.save_session(
        {**stored_session(), "evmAddress": OTHER, "token": OTHER_TOKEN}
    )


def test_auth_list_and_auth_use_switch_accounts(mcp):
    connect_two_accounts()
    listed = invoke(mcp, "auth_list")
    assert OTHER_TOKEN not in json.dumps(listed)
    assert {r["addr"]: r["active"] for r in listed["accounts"]} == {
        ADDR: False,
        OTHER: True,
    }
    assert all(r["usable"] for r in listed["accounts"])
    assert invoke(mcp, "auth_status")["addr"] == OTHER
    assert invoke(mcp, "auth_status", {"addr": ADDR})["addr"] == ADDR

    switched = invoke(mcp, "auth_use", {"addr": ADDR.upper().replace("0X", "0x")})
    assert {r["addr"]: r["active"] for r in switched["accounts"]} == {
        ADDR: True,
        OTHER: False,
    }
    assert storage.load_session()["evmAddress"] == ADDR
    info = invoke(mcp, "mcp_info")["session"]
    assert (info["addr"], info["connectedAccounts"]) == (ADDR, 2)

    missing = invoke(mcp, "auth_use", {"addr": "0x" + "d" * 40})
    assert missing["code"] == "INVALID_REQUEST"
    assert "is not connected" in missing["error"]


def test_trade_acts_as_the_requested_connected_account(mcp, monkeypatch):
    connect_two_accounts()
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    args = {"trade_id": 7, "addr": ADDR}
    preview = invoke(mcp, "preview_close", args)
    assert preview["addr"] == ADDR
    result = invoke(
        mcp, "trade_close", {**args, "preview_digest": preview["previewDigest"]}
    )
    # FakeKeeper checks that the submission carries ADDR's token, not the
    # active account's.
    assert result["status"] == "accepted", result
    assert result["addr"] == ADDR
    assert storage.load_session()["evmAddress"] == OTHER


def test_trade_checks_sign_in_for_the_requested_account(mcp, monkeypatch):
    """With the active account signed in, a trade for another connected
    account that expired, or one that is not connected, is still refused
    before any Keeper call."""
    storage.save_session(stored_session(hours=-1))
    storage.save_session(
        {**stored_session(), "evmAddress": OTHER, "token": OTHER_TOKEN}
    )
    fake = FakeKeeper({keeper.MARKETS: markets()})
    monkeypatch.setattr(keeper, "request", fake)
    assert invoke(mcp, "mcp_info")["trading"]["available"] is True
    expired = invoke(
        mcp,
        "trade_close",
        {"trade_id": 7, "preview_digest": "0" * 64, "addr": ADDR},
    )
    assert expired["code"] == "SESSION_EXPIRED"
    missing = invoke(
        mcp,
        "trade_close",
        {"trade_id": 7, "preview_digest": "0" * 64, "addr": "0x" + "d" * 40},
    )
    assert missing["code"] == "SESSION_UNAVAILABLE"
    assert "is not connected" in missing["error"]
    assert fake.calls == []


def test_sign_in_for_the_requested_account_comes_before_validation(mcp, monkeypatch):
    """The gate ahead of argument validation reads `addr`: a malformed trade
    for an expired account is refused for the session, not the arguments,
    even while the active account is signed in."""
    storage.save_session(stored_session(hours=-1))
    storage.save_session(
        {**stored_session(), "evmAddress": OTHER, "token": OTHER_TOKEN}
    )
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    error = invoke(mcp, "trade_close", {"trade_id": "not-a-number", "addr": ADDR})
    assert error["code"] == "SESSION_EXPIRED"
    assert fake.calls == []


def test_switching_accounts_after_a_preview_refuses_the_trade(mcp, monkeypatch):
    connect_two_accounts()
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_close", {"trade_id": 7})
    assert preview["addr"] == OTHER
    invoke(mcp, "auth_use", {"addr": ADDR})
    error = invoke(
        mcp,
        "trade_close",
        {"trade_id": 7, "preview_digest": preview["previewDigest"]},
    )
    assert error["code"] == "PREVIEW_MISMATCH"
    assert error["preview"]["addr"] == ADDR
    assert fake.submitted == []


def test_switch_during_submission_cannot_borrow_another_preview(mcp, monkeypatch):
    # ADDR is active; the user confirmed a preview for OTHER.
    storage.save_session(
        {**stored_session(), "evmAddress": OTHER, "token": OTHER_TOKEN}
    )
    storage.save_session(stored_session())
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_close", {"trade_id": 7, "addr": OTHER})
    # Another process switches to OTHER right after the trade loads ADDR.
    real = sessions.load_active_session

    def switch_after_load(endpoint, addr=None):
        result = real(endpoint, addr)
        storage.use_session(OTHER)
        return result

    monkeypatch.setattr(sessions, "load_active_session", switch_after_load)
    error = invoke(
        mcp, "trade_close", {"trade_id": 7, "preview_digest": preview["previewDigest"]}
    )
    assert error["code"] == "PREVIEW_MISMATCH"
    assert error["preview"]["addr"] == ADDR
    assert fake.submitted == []


def test_unknown_arguments_are_rejected(mcp, monkeypatch):
    connect_two_accounts()
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    for tool in mcp._tool_manager.list_tools():
        assert tool.parameters.get("additionalProperties") is False, tool.name
    # A client still using the old name must not fall back to the active
    # account.
    with pytest.raises(ToolError, match="address"):
        asyncio.run(mcp.call_tool("preview_close", {"trade_id": 7, "address": ADDR}))
    assert fake.submitted == []


MARKET_OPEN = {**OPEN_ARGS, "order_type": "market", "price": None}


def test_preview_with_no_account_never_authorizes_a_trade(mcp, monkeypatch):
    fake = FakeKeeper({keeper.MARKETS: markets()})
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_open", MARKET_OPEN)
    assert preview["addr"] is None
    storage.save_session(stored_session())
    error = invoke(
        mcp,
        "trade_open",
        {
            **MARKET_OPEN,
            "preview_digest": preview["previewDigest"],
            "reference_price": preview["referencePrice"],
            "allow_concurrent_open": True,
        },
    )
    assert error["code"] == "PREVIEW_MISMATCH"
    assert error["preview"]["addr"] == ADDR
    assert fake.submitted == []


def test_logout_during_the_price_refresh_refuses_the_trade(mcp, monkeypatch):
    """A signed-out preview, then sign-in, then logout while the market
    price refreshes: the trade must not submit with the session it loaded
    before the logout."""
    fake = FakeKeeper({keeper.MARKETS: markets()})
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_open", MARKET_OPEN)
    assert preview["addr"] is None
    storage.save_session(stored_session())

    def logout_on_refresh(endpoint, query, variables=None, token=None, timeout=20):
        if query == keeper.MARKETS:
            storage.delete_session()
        return fake(endpoint, query, variables, token)

    monkeypatch.setattr(keeper, "request", logout_on_refresh)
    error = invoke(
        mcp,
        "trade_open",
        {
            **MARKET_OPEN,
            "preview_digest": preview["previewDigest"],
            "reference_price": preview["referencePrice"],
            "allow_concurrent_open": True,
        },
    )
    assert error["code"] == "SIGNED_OUT"
    assert "`sai auth login NAME` or `sai auth import`" in error["error"]
    assert any(query == keeper.MARKETS for query, _ in fake.calls[1:])
    assert fake.submitted == []


def test_logout_during_the_refresh_refuses_a_confirmed_signed_in_trade(
    mcp, monkeypatch
):
    storage.save_session(stored_session())
    fake = FakeKeeper({keeper.MARKETS: markets()})
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_open", MARKET_OPEN)
    assert preview["addr"] == ADDR

    def logout_on_refresh(endpoint, query, variables=None, token=None, timeout=20):
        if query == keeper.MARKETS:
            storage.delete_session()
        return fake(endpoint, query, variables, token)

    monkeypatch.setattr(keeper, "request", logout_on_refresh)
    error = invoke(
        mcp,
        "trade_open",
        {
            **MARKET_OPEN,
            "preview_digest": preview["previewDigest"],
            "reference_price": preview["referencePrice"],
            "allow_concurrent_open": True,
        },
    )
    assert error["code"] == "SIGNED_OUT"
    assert fake.submitted == []


def test_preview_for_an_unconnected_account_fails(mcp, monkeypatch):
    connect_two_accounts()
    monkeypatch.setattr(keeper, "request", FakeKeeper())
    error = invoke(mcp, "preview_close", {"trade_id": 7, "addr": "0x" + "d" * 40})
    assert error["code"] == "SESSION_UNAVAILABLE"
    assert "is not connected" in error["error"]


def test_errors_redact_every_connected_token():
    connect_two_accounts()
    text = f"failed with {TOKEN} and {OTHER_TOKEN}"
    assert tools.redact(text) == f"failed with {tools.REDACTED} and {tools.REDACTED}"


def test_preview_without_a_session_names_no_account(mcp, monkeypatch):
    monkeypatch.setattr(keeper, "request", FakeKeeper())
    preview = invoke(mcp, "preview_close", {"trade_id": 7})
    assert preview["addr"] is None


def test_reference_price_is_only_for_market_orders(mcp, monkeypatch):
    storage.save_session(stored_session())
    monkeypatch.setattr(keeper, "request", FakeKeeper())
    trade = confirm(mcp, "preview_open", OPEN_ARGS)
    error = invoke(mcp, "trade_open", {**trade, "reference_price": "100"})
    assert error["code"] == "INVALID_REQUEST"


def test_margin_trade_binds_to_the_confirmed_quote(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    args = {"trade_id": 7, "direction": "add", "amount": "1"}
    snapshots(monkeypatch, "2000000")
    preview = invoke(mcp, "preview_margin", args)
    trade = {**args, "preview_digest": preview["previewDigest"]}

    # The position changed after the user confirmed: a fresh, consistent
    # pair of snapshots still differs from the confirmed quote.
    snapshots(monkeypatch, "2500000", "2500000")
    error = invoke(mcp, "trade_margin", trade)
    assert error["code"] == "PREVIEW_MISMATCH"
    assert error["preview"]["quote"]["collateralBefore"] == "2500000"
    assert fake.submitted == []

    snapshots(monkeypatch, "2000000", "2000000")
    result = invoke(mcp, "trade_margin", trade)
    assert result["status"] == "accepted"
    assert result["quote"] == preview["quote"]
    assert fake.intent() == preview["intent"]


def test_partial_close_trade_binds_to_the_confirmed_size(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper({keeper.MARKETS: markets()})
    monkeypatch.setattr(keeper, "request", fake)
    args = {"trade_id": 7, "percent": 50}
    snapshots(monkeypatch, "4000000")
    preview = invoke(mcp, "preview_partial_close", args)
    assert preview["intent"]["body"]["collateral"]["amountUsdc"] == "2000000"
    trade = {**args, "preview_digest": preview["previewDigest"]}

    snapshots(monkeypatch, "3000000", "3000000")
    error = invoke(mcp, "trade_partial_close", trade)
    assert error["code"] == "PREVIEW_MISMATCH"
    assert error["preview"]["intent"]["body"]["collateral"]["amountUsdc"] == "1500000"
    assert fake.submitted == []

    snapshots(monkeypatch, "4000000", "4000000")
    result = invoke(mcp, "trade_partial_close", trade)
    assert result["status"] == "accepted"
    assert fake.intent() == preview["intent"]


def test_recent_pending_open_blocks_a_second_open(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    first = invoke(mcp, "trade_open", confirm(mcp, "preview_open", OPEN_ARGS))
    assert first["status"] == "accepted"

    other = {**OPEN_ARGS, "side": "short"}
    trade = confirm(mcp, "preview_open", other)
    blocked = invoke(mcp, "trade_open", trade)
    assert blocked["code"] == "RECENT_OPEN_PENDING"
    assert blocked["intentKeys"] == [first["intentKey"]]
    assert "intent_status" in blocked["error"]
    assert len(fake.submitted) == 1

    # A close is not an open and is not blocked.
    invoke(mcp, "trade_close", confirm(mcp, "preview_close", {"trade_id": 7}))
    assert len(fake.submitted) == 2

    allowed = invoke(mcp, "trade_open", {**trade, "allow_concurrent_open": True})
    assert allowed["status"] == "accepted"
    assert len(fake.submitted) == 3


def test_unknown_open_after_a_timeout_blocks_with_its_key(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper(lose_ack=True)
    monkeypatch.setattr(keeper, "request", fake)
    trade = confirm(mcp, "preview_open", OPEN_ARGS)
    lost = invoke(mcp, "trade_open", trade)
    assert lost["code"] == "OUTCOME_UNKNOWN"
    fake.lose_ack = False
    blocked = invoke(mcp, "trade_open", trade)
    assert blocked["code"] == "RECENT_OPEN_PENDING"
    assert blocked["intentKeys"] == [lost["intentKey"]]
    assert len(fake.submitted) == 1


def test_sign_in_comes_before_validation(mcp):
    bad = {**OPEN_ARGS, "amount": "-1", "preview_digest": "0" * 64}
    assert invoke(mcp, "trade_open", bad)["code"] == "SIGNED_OUT"


def test_validation_comes_before_keeper(mcp):
    storage.save_session(stored_session())
    error = invoke(
        mcp, "trade_open", {**OPEN_ARGS, "amount": "-1", "preview_digest": "0" * 64}
    )
    assert error["code"] == "INVALID_REQUEST"
    assert "Amount" in error["error"]


def test_lost_ack_reports_unknown_then_intent_status_reconciles(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper(lose_ack=True)
    monkeypatch.setattr(keeper, "request", fake)
    error = invoke(mcp, "trade_close", confirm(mcp, "preview_close", {"trade_id": 7}))
    key = fake.submitted[-1]["intentKey"]
    assert error["code"] == "OUTCOME_UNKNOWN"
    assert error["intentKey"] == key
    assert "intent_status" in error["next"]
    local = invoke(mcp, "intent_local")
    assert local["addr"] == ADDR and local["addrSource"] == "session"
    assert local["intents"] == [
        {"intentKey": key, "kind": "CLOSE_POSITION", "status": "unknown"}
    ]

    blocked = invoke(mcp, "trade_close", confirm(mcp, "preview_close", {"trade_id": 8}))
    assert blocked["code"] == "INVALID_REQUEST"
    assert key in blocked["error"]

    row = {
        "intentKey": key,
        "lifecycleStep": "exec_pass",
        "execActor": "server",
        "evmAddress": ADDR,
        "txHash": "0x" + "C" * 64,
    }
    trade = {
        "intentKey": key,
        "evmTxHash": "0x" + "c" * 64,
        "trade": {"id": 7, "trader": ADDR},
    }
    fake.answers = {
        keeper.INTENTS: {"userIntents": [row]},
        keeper.INTENT_HISTORY: {"perp": {"tradeHistory": [trade]}},
    }
    status = invoke(mcp, "intent_status", {"key": key})
    assert status["matchedTradeHistory"] == [trade]
    assert fake.calls[-2] == (keeper.INTENTS, {"user": ADDR, "key": key})
    assert status["addr"] == ADDR
    assert invoke(mcp, "intent_local")["intents"][0]["status"] == "terminal"


def test_passphrase_session_is_refused_without_a_prompt(mcp, monkeypatch):
    # A session from an older version that encrypted it with a passphrase.
    path = storage.directory() / "session.json"
    path.write_text(json.dumps({"version": 1, "nonce": "", "ciphertext": ""}))
    fake = FakeKeeper({keeper.POSITIONS: {"perp": {"trades": []}}})
    monkeypatch.setattr(keeper, "request", fake)
    for name, arguments in (
        ("preview_margin", {"trade_id": 1, "direction": "add", "amount": "5"}),
        ("preview_partial_close", {"trade_id": 1, "percent": 50}),
        ("trade_close", {"trade_id": 1, "preview_digest": "0" * 64}),
        ("auth_status", {}),
    ):
        error = invoke(mcp, name, arguments)
        assert error["code"] == "SESSION_UNAVAILABLE", name
        assert "passphrase-encrypted" in error["error"]
        assert "sai auth login" in error["error"]
    assert invoke(mcp, "account_positions")["code"] == "SESSION_UNAVAILABLE"
    assert invoke(mcp, "account_positions", {"addr": ADDR})["perp"] == {"trades": []}
    info = invoke(mcp, "mcp_info")["session"]
    assert info["signedIn"] is False and "passphrase-encrypted" in info["error"]
    assert fake.submitted == []


def test_expired_or_missing_session_is_reported(mcp):
    assert invoke(mcp, "auth_status")["code"] == "SIGNED_OUT"
    storage.save_session(stored_session(hours=-1))
    error = invoke(mcp, "auth_status")
    assert error["code"] == "SESSION_EXPIRED"
    assert "sai auth login" in error["error"]
    session = invoke(mcp, "mcp_info")["session"]
    assert session["expired"] is True and session["usable"] is False


# Errors and secrets


def test_keeper_errors_map_to_stable_codes(mcp, monkeypatch):
    cases = (
        (keeper.AdmissionRejected("rejected"), "ADMISSION_REJECTED"),
        (keeper.NotSent("not sent"), "NOT_SENT"),
        (keeper.KeeperError("failed"), "KEEPER_ERROR"),
        (stream.StreamError("ended"), "STREAM_ERROR"),
        (KeyError("perp"), "INCOMPLETE_DATA"),
    )
    for exc, code in cases:

        def fail(*_args: Any, exc: Exception = exc, **_kwargs: Any) -> Any:
            raise exc

        monkeypatch.setattr(keeper, "request", fail)
        assert invoke(mcp, "market_list")["code"] == code


def test_unexpected_errors_are_generic_and_logged(mcp, monkeypatch, caplog):
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"boom {TOKEN}")

    monkeypatch.setattr(keeper, "request", fail)
    with caplog.at_level("ERROR", logger="sai_cli.mcp_server"):
        result = invoke(mcp, "market_list")
    assert result == {"error": tools.INTERNAL, "code": "INTERNAL_ERROR"}
    assert "RuntimeError" in caplog.text


def test_logged_and_returned_errors_redact_the_session_token(mcp, monkeypatch, caplog):
    storage.save_session(stored_session())

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"boom {TOKEN}")

    monkeypatch.setattr(keeper, "request", fail)
    with caplog.at_level("ERROR", logger="sai_cli.mcp_server"):
        assert invoke(mcp, "market_list")["code"] == "INTERNAL_ERROR"
    assert "RuntimeError: boom" in caplog.text
    assert tools.REDACTED in caplog.text
    assert TOKEN not in caplog.text

    def known(*_args: Any, **_kwargs: Any) -> Any:
        raise keeper.KeeperError(f"failed {TOKEN}")

    monkeypatch.setattr(keeper, "request", known)
    error = invoke(mcp, "market_list")
    assert error["error"] == f"failed {tools.REDACTED}"


def test_mcp_info_strips_credentials_from_endpoints(monkeypatch):
    monkeypatch.setenv("SAI_ENDPOINT", "https://user:pw@k.example:8443/query?key=s")
    monkeypatch.setenv(
        "SAI_LCD_ENDPOINT", "http://[::1]:9/fixture-lcd-path?key=fixture-lcd-query"
    )
    storage.save_session(stored_session(endpoint="https://u:p@k.example/q?t=s"))
    info = invoke(server.create_server(), "mcp_info")
    assert info["endpoints"] == {
        "keeper": "https://k.example:8443",
        "websocket": "wss://k.example:8443",
        "lcd": "http://[::1]:9",
    }
    assert info["session"]["endpoint"] == "https://k.example"
    text = json.dumps(info)
    for secret in (
        "user:pw",
        "key=",
        "fixture-lcd-path",
        "fixture-lcd-query",
        "u:p@",
        "t=s",
    ):
        assert secret not in text


def test_mcp_info_reports_versions_without_network(mcp, monkeypatch):
    info = invoke(mcp, "mcp_info")
    assert info["version"] == sai_info.RUNNING_VERSION == sai_info.version()
    assert info["installedVersion"] == info["version"]
    assert info["restartNeeded"] is False
    assert info["latestVersion"] is None

    update.write_cache(update.Cache("sai-cli/v9.8.7", update.now()))
    # A process started before a reinstall still runs its old version.
    monkeypatch.setattr(sai_info, "RUNNING_VERSION", "0.0.1")
    info = invoke(mcp, "mcp_info")
    assert info["version"] == "0.0.1"
    assert info["installedVersion"] == sai_info.version()
    assert info["restartNeeded"] is True
    assert info["latestVersion"] == "9.8.7"


def test_session_reports_never_include_the_token(mcp):
    storage.save_session(stored_session())
    status = invoke(mcp, "auth_status")
    assert set(status) == {
        "addr",
        "expiresAt",
        "endpoint",
        "expiryWarning",
        "renewalCommand",
    }
    assert (status["addr"], status["endpoint"]) == (
        ADDR,
        ENDPOINT.removesuffix("/query"),
    )
    info = invoke(mcp, "mcp_info")
    assert info["session"]["addr"] == ADDR
    assert info["session"]["usable"] is True
    assert "expires in" in info["session"]["expiryWarning"]
    assert info["session"]["renewalCommand"] == status["renewalCommand"]
    assert "expiryWarning" in invoke(mcp, "auth_list")["accounts"][0]
    assert info["provenance"]["kind"] in ("source-checkout", "installed")
    assert info["endpoints"]["keeper"] == "https://sai-keeper.nibiru.fi"


def test_session_reports_keep_an_account_without_an_endpoint(mcp):
    stored = stored_session()
    del stored["endpoint"]
    storage.save_session(stored)

    info = invoke(mcp, "mcp_info")
    [account] = invoke(mcp, "auth_list")["accounts"]
    assert info["session"]["signedIn"] is True
    assert account["active"] is True
    for report in (info["session"], account):
        assert report["addr"] == ADDR
        assert report["endpoint"] is None
        assert report["endpointMatches"] is False
        assert report["usable"] is False


# Stdio


def sai_command() -> list[str]:
    """Return the installed `sai` script, or the module when it is absent."""
    script = Path(sys.executable).with_name("sai.exe" if os.name == "nt" else "sai")
    return [str(script)] if script.exists() else [sys.executable, "-m", "sai_cli"]


def rpc(message_id: int | None, method: str, params: dict[str, Any]) -> str:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
    if message_id is not None:
        message["id"] = message_id
    return json.dumps(message) + "\n"


@pytest.mark.parametrize("global_flags", [[], ["--json", "--as", "another-account"]])
def test_sai_mcp_serve_writes_only_protocol_to_stdout(tmp_path, global_flags):
    """initialize and tools/list over a real `sai mcp serve` process; every
    stdout line is a JSON-RPC message."""
    initialize = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    }
    command = [
        *sai_command(),
        *global_flags,
        "mcp",
        "serve",
        "--endpoint",
        "http://127.0.0.1:9/query",
        "--lcd-endpoint",
        "http://127.0.0.1:8/fixture-lcd-path?key=fixture-lcd-query",
    ]
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"SAI_HOME": str(tmp_path), "PATH": os.defpath},
        text=True,
    ) as process:
        assert process.stdin is not None and process.stdout is not None

        def ask(message_id: int, method: str, params: dict[str, Any]) -> Any:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(rpc(message_id, method, params))
            process.stdin.flush()
            reply = json.loads(process.stdout.readline())
            assert reply["jsonrpc"] == "2.0" and reply["id"] == message_id
            return reply["result"]

        started = ask(1, "initialize", initialize)
        process.stdin.write(rpc(None, "notifications/initialized", {}))
        listed = ask(2, "tools/list", {})
        called = ask(3, "tools/call", {"name": "mcp_info", "arguments": {}})
        process.stdin.close()
        rest = process.stdout.read()
        stderr = process.stderr.read() if process.stderr else ""
        assert process.wait(timeout=30) == 0, stderr
    assert rest == ""
    assert started["serverInfo"]["name"] == "sai"
    assert "sai auth login" in started["instructions"]
    assert {tool["name"] for tool in listed["tools"]} == READ_TOOLS | TRADE_TOOLS
    info = json.loads(called["content"][0]["text"])
    assert info["endpoints"]["keeper"] == "http://127.0.0.1:9"
    assert info["endpoints"]["websocket"] == "ws://127.0.0.1:9"
    assert info["endpoints"]["lcd"] == "http://127.0.0.1:8"
    assert info["trading"]["reason"] == "signed-out"
    assert "agent client should launch it" not in stderr
    logs = "".join(path.read_text() for path in (tmp_path / "logs").glob("*.jsonl"))
    assert '"event":"mcp.server_start"' in logs
    for secret in ("fixture-lcd-path", "fixture-lcd-query"):
        assert secret not in json.dumps(called) + stderr + logs


def test_stdio_client_lists_every_tool_from_sai_mcp_serve(tmp_path):
    command, *args = sai_command()

    async def listing(errlog: TextIO) -> tuple[set[str], dict[str, Any]]:
        params = StdioServerParameters(
            command=command,
            args=[*args, "mcp", "serve"],
            env={"SAI_HOME": str(tmp_path)},
        )
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            listed = await client.list_tools()
            called = await client.call_tool("mcp_info", {})
        return {tool.name for tool in listed.tools}, json.loads(text_of(called.content))

    with open(tmp_path / "stderr.log", "w") as errlog:
        names, info = asyncio.run(listing(errlog))
        assert names == READ_TOOLS | TRADE_TOOLS
        assert info["trading"]["available"] is False
        storage.save_session(stored_session())
        signed_in, info = asyncio.run(listing(errlog))
        assert signed_in == names
        assert info["trading"] == {
            "available": True,
            "reason": "signed-in",
            "addr": ADDR,
        }


def stdio_auth_use(
    tmp_path: Path, addr: object, *, endpoint: str | None = None
) -> tuple[CallToolResult, str]:
    command, *args = sai_command()
    env = {"SAI_HOME": str(tmp_path), "SAI_LOG": "1"}
    if endpoint is not None:
        env["SAI_ENDPOINT"] = endpoint
    params = StdioServerParameters(
        command=command,
        args=[*args, "mcp", "serve"],
        env=env,
    )

    async def switch(errlog: TextIO) -> CallToolResult:
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            return await client.call_tool("auth_use", {"addr": addr})

    stderr = tmp_path / "stderr.log"
    with stderr.open("w") as errlog:
        result = asyncio.run(switch(errlog))
    logs = "".join(
        file.read_text() for _, _, file in sai_log.log_files(tmp_path / "logs")
    )
    return result, logs + stderr.read_text()


def test_stdio_auth_use_reports_success_when_legacy_cleanup_fails(
    tmp_path: Path,
) -> None:
    marker = "synthetic-mcp-legacy-cleanup-credential"
    records = {
        ADDR: {**stored_session(), "token": marker},
        OTHER: {**stored_session(), "evmAddress": OTHER, "token": OTHER_TOKEN},
    }
    storage._save_accounts({"active": OTHER, "sessions": records})
    legacy = tmp_path / "trader" / "session.json"
    legacy.mkdir()

    result, logs = stdio_auth_use(tmp_path, ADDR)

    assert not result.isError
    response = json.loads(text_of(result.content))
    assert [row["addr"] for row in response["accounts"] if row["active"]] == [ADDR]
    assert "Could not remove legacy session file trader/session.json" in logs
    assert "session.active" in logs
    assert marker not in result.model_dump_json() + logs
    assert storage.load_accounts()["active"] == ADDR
    assert legacy.is_dir()


@pytest.mark.parametrize("same_account", [True, False])
def test_stdio_auth_use_redacts_known_credentials_in_session_fields(
    tmp_path: Path, same_account: bool
) -> None:
    marker = "synthetic-mcp-origin-credential"
    endpoint = f"https://{marker}.example.invalid"
    selected = {
        **stored_session(endpoint=endpoint),
        "token": marker if same_account else TOKEN,
    }
    other = {
        **stored_session(),
        "evmAddress": OTHER,
        "token": selected["expiresAt"] if same_account else marker,
    }
    storage._save_accounts(
        {"active": OTHER, "sessions": {ADDR: selected, OTHER: other}}
    )

    result, logs = stdio_auth_use(tmp_path, ADDR, endpoint=endpoint)

    assert not result.isError
    response = json.loads(text_of(result.content))
    active = next(row for row in response["accounts"] if row["active"])
    assert active["addr"] == ADDR
    assert active["endpoint"] == sai_log.REDACTED
    assert active["expiresAt"] == (
        sai_log.REDACTED if same_account else selected["expiresAt"]
    )
    assert "SAI_RECONCILE_ENDPOINT" in active["renewalCommand"]
    assert marker not in result.model_dump_json() + logs
    if same_account:
        assert selected["expiresAt"] not in result.model_dump_json()
    assert storage.load_accounts()["active"] == ADDR


@pytest.mark.parametrize("as_object", [False, True])
def test_stdio_auth_use_keeps_unregistered_input_private(
    tmp_path: Path, as_object: bool
) -> None:
    marker = "synthetic-mcp-unregistered-credential-marker"
    result, logs = stdio_auth_use(tmp_path, {"value": marker} if as_object else marker)

    assert marker not in result.model_dump_json() + logs
    text = text_of(result.content)
    assert "must be a string" in text if as_object else "is not connected" in text
    assert "mcp.tool_result" in logs and "INVALID_REQUEST" in logs
    assert "session.active" not in logs


@pytest.mark.parametrize(
    "corruption",
    ["selected-expiry", "other-expiry", "endpoint", "address", "other-address"],
)
def test_stdio_auth_use_refuses_malformed_response_before_switching(
    tmp_path: Path, corruption: str
) -> None:
    secret = "synthetic-mcp-switch-credential-marker"
    third = "0x" + "d" * 40
    records: dict[str, dict[str, Any]] = {
        ADDR: stored_session(),
        OTHER: {**stored_session(), "evmAddress": OTHER, "token": OTHER_TOKEN},
        third: {**stored_session(), "evmAddress": third},
    }
    if corruption == "endpoint":
        records[ADDR]["endpoint"] = {"value": secret}
        field = "endpoint"
    elif corruption in {"address", "other-address"}:
        records[third if corruption == "other-address" else ADDR]["evmAddress"] = secret
        field = "address"
    else:
        records[third if corruption == "other-expiry" else ADDR]["expiresAt"] = secret
        field = "expiry"
    storage._save_accounts({"active": OTHER, "sessions": records})
    path = tmp_path / "trader" / "sessions.json"
    before = path.read_bytes()
    result, logs = stdio_auth_use(tmp_path, ADDR)

    assert json.loads(text_of(result.content)) == {
        "error": f"Stored session has an invalid {field}; {storage.SIGN_IN_AGAIN}",
        "code": "SESSION_UNAVAILABLE",
    }
    assert "mcp.tool_result" in logs and "SESSION_UNAVAILABLE" in logs
    assert "session.active" not in logs
    assert secret not in result.model_dump_json() + logs
    assert path.read_bytes() == before
    assert storage.load_accounts()["active"] == OTHER


def test_bare_sai_mcp_prints_help():
    result = CliRunner().invoke(main, ["mcp"])
    assert result.exit_code == 0
    assert "serve" in result.output
    assert "sai mcp install claude" in result.output


def test_sai_mcp_serve_takes_the_root_endpoint(monkeypatch):
    """`sai --endpoint X mcp serve` serves X; serve's own flags win."""
    seen: list[dict[str, str | None] | None] = []
    monkeypatch.setattr(server, "serve", seen.append)
    runner = CliRunner()
    url = "http://127.0.0.1:9/query"
    assert runner.invoke(main, ["--endpoint", url, "mcp", "serve"]).exit_code == 0
    assert runner.invoke(main, ["mcp", "serve"]).exit_code == 0
    own = ["--endpoint", url, "mcp", "serve", "--endpoint", "http://127.0.0.1:5/q"]
    assert runner.invoke(main, own).exit_code == 0
    keys = ("keeper.endpoint", "keeper.ws_endpoint", "lcd.endpoint")
    unset = dict.fromkeys(keys)
    assert seen == [
        {**unset, "keeper.endpoint": url},
        unset,
        {**unset, "keeper.endpoint": "http://127.0.0.1:5/q"},
    ]
