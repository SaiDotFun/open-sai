"""Replay Keeper intent outcomes through HTTP and the real intent ledger."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from multiprocessing.queues import Queue
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from loopback_keeper import KeeperHandler, keeper_server
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent
from sai_cli.core import keeper, reconcile, settings, storage
from sai_cli.mcp_server import server
from sai_cli.sai_cli import main

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "intent_outcomes.json").read_text(
        encoding="utf-8"
    )
)
ADDR = FIXTURE["address"]
KEY = FIXTURE["key"]
RESPONSES = {**FIXTURE["recorded"], **FIXTURE["replays"]}
NIBI_ADDR = RESPONSES["pending_exec"]["data"]["userIntents"][0]["nibiAddress"]
OTHER_ADDR = "0x" + "b" * 40
OTHER_NIBI_ADDR = "nibi1hwamhwamhwamhwamhwamhwamhwamhwamc9gs8a"
Reply = str | dict[str, Any] | Callable[[], dict[str, Any]]
MARKET = {
    "data": {
        "perp": {
            "borrowings": [
                {
                    "marketId": 0,
                    "collateralToken": {"symbol": "USDC"},
                    "isOpen": True,
                    "price": 100,
                },
            ]
        }
    }
}
OPEN_ARGS = {
    "market_id": 0,
    "side": "long",
    "order_type": "market",
    "amount": "10",
    "leverage": "2",
    "slippage": "1",
}
CLI_OPEN = [
    "trade",
    "open",
    "--market",
    "0",
    "--side",
    "long",
    "--type",
    "market",
    "--amount",
    "10",
    "--leverage",
    "2",
    "--slippage",
    "1",
]


@pytest.fixture
def submitted_keys() -> list[str]:
    return []


@pytest.fixture
def keeper_replay(submitted_keys: list[str]) -> Iterator[tuple[str, deque[Reply]]]:
    replies: deque[Reply] = deque()

    class Handler(KeeperHandler):
        def do_POST(self) -> None:
            request = self.read_json()
            if request["query"] == keeper.SUBMIT:
                submitted_keys.append(request["variables"]["input"]["intentKey"])
            queued = replies.popleft()
            if queued == "admission_ack":
                assert request["query"] == keeper.SUBMIT
                submitted = request["variables"]["input"]
                assert submitted["intent"]["kind"] == "OPEN_TRADE"
                assert submitted["intent"]["body"]["orderType"] == "market"
                reply = {
                    "data": {
                        "recordUserIntent": {
                            "intentKey": submitted["intentKey"],
                            "evmAddress": ADDR,
                            "lifecycleStep": "jwt_pass",
                            "execActor": "server",
                        }
                    }
                }
            elif isinstance(queued, str):
                reply = RESPONSES[queued]
            elif isinstance(queued, dict):
                reply = queued
            else:
                reply = queued()
            if "userIntents" in reply["data"]:
                assert request["query"] == keeper.INTENTS
                assert request["variables"]["user"].lower() in (ADDR, NIBI_ADDR)
                assert request["variables"]["key"] in (KEY, None, *submitted_keys)
            elif "tradeHistory" in reply["data"].get("perp", {}):
                assert request["query"] == keeper.HISTORY
                assert request["variables"] == {"trader": ADDR}
            elif "borrowings" in reply["data"].get("perp", {}):
                assert request["query"] == keeper.MARKETS
            self.reply(reply)

    with keeper_server(Handler) as base:
        yield f"{base}/query", replies
        assert not replies


def status(endpoint: str) -> str:
    return storage.list_pending(endpoint, ADDR)[0]["status"]


def call_mcp(mcp: FastMCP, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    response = asyncio.run(mcp.call_tool(tool, args))
    content = response[0] if isinstance(response, tuple) else response
    assert isinstance(content, list)
    block = content[0]
    assert isinstance(block, TextContent)
    return json.loads(block.text)


def reserve(endpoint: str, *, expired: bool = False) -> None:
    storage.pending(KEY, endpoint, ADDR, "OPEN_TRADE", "")
    if expired:
        with storage.db() as connection:
            connection.execute(
                "UPDATE intents SET created_at=? WHERE id=?",
                (time.time() - keeper.ABSENT_AFTER_SECONDS - 1, KEY),
            )


@pytest.fixture
def connected_replay(
    keeper_replay: tuple[str, deque[Reply]],
) -> tuple[str, deque[Reply]]:
    endpoint, _ = keeper_replay
    storage.save_session(
        {
            "evmAddress": ADDR,
            "endpoint": endpoint,
            "token": "fixture-token",
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
    )
    return keeper_replay


@pytest.mark.parametrize(
    ("response", "local"),
    [
        ("started", "unknown"),
        ("jwt_failed", "unknown"),
        ("user_rejected", "unknown"),
        ("user_sign_failed", "unknown"),
        ("user_submitted", "unknown"),
        ("accepted", "accepted"),
        ("submitted", "accepted"),
        ("pending_exec", "pending"),
        ("chain_execution_failed", "terminal"),
        ("market_order_expired", "terminal"),
        ("market_order_fill_failed", "terminal"),
        ("market_order_trading_paused", "terminal"),
        ("market_order_market_closed", "terminal"),
        ("market_order_trade_not_open", "terminal"),
        ("simulation_failed", "terminal"),
        ("admission_expired", "terminal"),
        ("broadcast_rejected", "terminal"),
        ("absent", "absent"),
    ],
)
def test_lifecycle_mapping(keeper_replay, response, local):
    endpoint, replies = keeper_replay
    reserve(endpoint, expired=True)
    replies.append(response)
    reconcile.reconcile(endpoint, ADDR, KEY)
    assert status(endpoint) == local


@pytest.mark.parametrize(
    ("intent_reply", "history_reply", "expected"),
    [
        ("server_finality", "open_history", "terminal"),
        ("user_finality", "not_yet_in_history", "pending"),
        ("other_trade_finality", "other_trade_history", "terminal"),
        ("other_trade_executed", "other_trade_history", "terminal"),
    ],
)
def test_history_corroborates_execution(
    keeper_replay, intent_reply, history_reply, expected
):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    replies.extend((intent_reply, history_reply))
    if expected == "pending":
        replies.append(history_reply)
    result = reconcile.reconcile(endpoint, ADDR, KEY)
    assert status(endpoint) == expected
    assert bool(result["matchedTradeHistory"]) == (expected == "terminal")


def test_canonical_fill_is_final_while_trade_history_lags(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    replies.extend(("filled", "not_yet_in_history", "not_yet_in_history"))
    assert reconcile.reconcile(endpoint, ADDR, KEY)["matchedTradeHistory"] == []
    assert status(endpoint) == "terminal"


def test_limit_placement_is_final_without_a_fill(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    replies.extend(("limit_placement_exec_pass", "limit_placement_history"))
    result = reconcile.reconcile(endpoint, ADDR, KEY)
    assert [row["tradeChangeType"] for row in result["matchedTradeHistory"]] == [
        "limit_order_created"
    ]
    assert status(endpoint) == "terminal"


def test_unindexed_sweep_then_filled_through_cli_and_mcp(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint, expired=True)
    mcp = server.create_server(settings.load(flags={"keeper.endpoint": endpoint}))

    def cli() -> dict:
        result = CliRunner().invoke(
            main,
            ["--endpoint", endpoint, "--json", "intent", "status", ADDR, "--key", KEY],
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    replies.append("pending_exec")
    result = cli()
    assert result["localStatus"] == result["outcome"] == "pending"
    assert "next oracle price" in result["hint"]
    assert "Do not resubmit" in result["hint"]

    replies.extend(("outcome_not_indexed", "not_yet_in_history", "not_yet_in_history"))
    result = call_mcp(mcp, "intent_status", {"addr": ADDR, "key": KEY})
    assert result["localStatus"] == result["outcome"] == "pending"
    assert "may have filled" in result["hint"]
    assert "Do not resubmit" in result["hint"]

    replies.extend(("absent", "submitted"))
    assert cli()["localStatus"] == "pending"
    assert cli()["localStatus"] == "pending"
    with pytest.raises(ValueError, match=KEY):
        storage.pending("unrelated-trade", endpoint, ADDR, "CLOSE_POSITION", "99")

    replies.extend(("filled", "not_yet_in_history", "filled_history"))
    result = cli()
    assert result["userIntents"][0]["lifecycleStep"] == "exec_pass"
    assert (
        result["matchedTradeHistory"]
        == RESPONSES["filled_history"]["data"]["perp"]["tradeHistory"]
    )
    assert status(endpoint) == "terminal"

    replies.append("pending_exec")
    result = cli()
    assert result["localStatus"] == "terminal"
    assert "hint" not in result
    storage.pending("different-trade", endpoint, ADDR, "OPEN_TRADE", "")


def test_unindexed_sweep_can_be_resolved_by_matching_history(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    replies.extend(("outcome_not_indexed", "not_yet_in_history", "filled_history"))
    result = reconcile.reconcile(endpoint, ADDR, KEY)
    assert result["matchedTradeHistory"]
    assert result["outcome"] == "terminal-filled"
    assert "hint" not in result
    assert status(endpoint) == "terminal"


@pytest.mark.parametrize("provisional", ["pending_exec", "outcome_not_indexed"])
def test_unkeyed_cli_status_blocks_a_second_acknowledged_open(
    connected_replay, provisional
):
    endpoint, replies = connected_replay
    cli = CliRunner()
    options = ["--endpoint", endpoint, "--json"]
    replies.extend((MARKET, "admission_ack"))
    submitted = cli.invoke(main, [*options, *CLI_OPEN])
    assert submitted.exit_code == 0, submitted.output
    key = json.loads(submitted.output)["intentKey"]
    assert status(endpoint) == "accepted"

    pending = RESPONSES[provisional]["data"]["userIntents"][0]
    replies.append({"data": {"userIntents": [{**pending, "intentKey": key}]}})
    result = cli.invoke(main, [*options, "intent", "status", ADDR])
    assert result.exit_code == 0, result.output
    row = json.loads(result.output)["userIntents"][0]
    assert row["outcome"] == "pending"
    assert "Do not resubmit" in row["hint"]
    assert status(endpoint) == "pending"

    replies.append(MARKET)
    blocked = cli.invoke(main, [*options, *CLI_OPEN])
    assert blocked.exit_code != 0
    assert f"Earlier open intent {key}" in blocked.output

    with storage.db() as connection:
        connection.execute("UPDATE intents SET created_at=0 WHERE id=?", (key,))
    mcp = server.create_server(settings.load(flags={"keeper.endpoint": endpoint}))
    replies.append(MARKET)
    preview = call_mcp(mcp, "preview_open", OPEN_ARGS)
    args = {
        **OPEN_ARGS,
        "preview_digest": preview["previewDigest"],
        "reference_price": preview["referencePrice"],
    }
    for allow in (False, True):
        result = call_mcp(mcp, "trade_open", {**args, "allow_concurrent_open": allow})
        assert result["code"] == "RECENT_OPEN_PENDING"
        assert result["intentKeys"] == [key]
        assert "intent_status" in result["error"]
        assert "allow_concurrent_open" not in result["error"]


@pytest.mark.parametrize("retry", ["same_process", "restart", "mcp"])
def test_acknowledged_open_blocks_retry(connected_replay, submitted_keys, retry):
    endpoint, replies = connected_replay
    cli = CliRunner()
    options = ["--endpoint", endpoint, "--json"]

    def open_cli(*flags: str) -> tuple[int, dict[str, Any]]:
        args = [*options, *CLI_OPEN, *flags]
        if retry == "restart":
            process = subprocess.run(
                [sys.executable, "-m", "sai_cli", *args],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
            return process.returncode, json.loads(process.stdout)
        result = cli.invoke(main, args)
        return result.exit_code, json.loads(result.stdout)

    replies.extend((MARKET, "admission_ack"))
    exit_code, first = open_cli()
    assert exit_code == 0
    assert first["status"] == status(endpoint) == "accepted"
    key = first["intentKey"]
    if retry == "mcp":
        mcp = server.create_server(settings.load(flags={"keeper.endpoint": endpoint}))
        replies.append(MARKET)
        preview = call_mcp(mcp, "preview_open", OPEN_ARGS)
        args = {
            **OPEN_ARGS,
            "preview_digest": preview["previewDigest"],
            "reference_price": preview["referencePrice"],
        }
        # Keeper now quotes 103, outside the 1% slippage of the confirmed 100.
        moved = {
            "data": {
                "perp": {
                    "borrowings": [
                        {**MARKET["data"]["perp"]["borrowings"][0], "price": 103}
                    ]
                }
            }
        }
        replies.append(moved)
        error = call_mcp(mcp, "trade_open", args)
        assert error["code"] == "RECENT_OPEN_PENDING"
        assert replies.pop() == moved
        assert error["intentKeys"] == [key]
        assert f"intent {key}" in error["error"]
        assert "intent_status" in error["error"]
    else:
        replies.append(MARKET)
        exit_code, blocked = open_cli()
        assert exit_code == 1
        error = blocked["error"]
        assert error["intentKey"] == key and error["retryable"] is False
        assert error["hint"].endswith(f"intent status {ADDR} --key {key}")
        assert endpoint not in error["hint"]
    assert error["code"] == "RECENT_OPEN_PENDING"
    assert submitted_keys == [key]

    if retry == "same_process":
        failure = RESPONSES["simulation_failed"]["data"]["userIntents"][0]
        replies.append({"data": {"userIntents": [{**failure, "intentKey": key}]}})
        checked = cli.invoke(main, [*options, "intent", "status", ADDR, "--key", key])
        assert checked.exit_code == 0, checked.output
        assert status(endpoint) == "terminal"
        replies.extend((MARKET, "admission_ack"))
        exit_code, allowed = open_cli()
        assert exit_code == 0
    elif retry == "restart":
        replies.extend((MARKET, "admission_ack"))
        exit_code, allowed = open_cli("--allow-concurrent-open")
        assert exit_code == 0
    else:
        replies.extend((MARKET, "admission_ack"))
        allowed = call_mcp(mcp, "trade_open", {**args, "allow_concurrent_open": True})
    assert allowed["status"] == "accepted"
    assert allowed["intentKey"] != key
    assert submitted_keys == [key, allowed["intentKey"]]


def _open_with_mcp(
    endpoint: str, args: dict[str, Any], results: Queue[dict[str, Any]]
) -> None:
    mcp = server.create_server(settings.load(flags={"keeper.endpoint": endpoint}))
    results.put(call_mcp(mcp, "trade_open", args))


def test_mcp_open_reservation_rechecks_after_concurrent_ack(
    connected_replay, submitted_keys
):
    endpoint, replies = connected_replay
    mcp = server.create_server(settings.load(flags={"keeper.endpoint": endpoint}))
    replies.append(MARKET)
    preview = call_mcp(mcp, "preview_open", OPEN_ARGS)
    args = {
        **OPEN_ARGS,
        "preview_digest": preview["previewDigest"],
        "reference_price": preview["referencePrice"],
    }

    # Both clients pass the ledger precheck before either gets a market price.
    # One price reply waits until the winning process has recorded its ack.
    both_refreshing = threading.Barrier(2, timeout=10)
    winner_accepted = threading.Event()

    def first_quote() -> dict[str, Any]:
        both_refreshing.wait()
        return MARKET

    def second_quote() -> dict[str, Any]:
        both_refreshing.wait()
        assert winner_accepted.wait(timeout=10)
        return MARKET

    replies.extend((first_quote, second_quote, "admission_ack", "admission_ack"))
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    workers = [
        context.Process(target=_open_with_mcp, args=(endpoint, args, results))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    try:
        winner = results.get(timeout=15)
        assert winner["status"] == status(endpoint) == "accepted"
        key = winner["intentKey"]
        winner_accepted.set()
        loser = results.get(timeout=15)
        assert submitted_keys == [key]
        assert replies.pop() == "admission_ack"
        assert loser["code"] == "RECENT_OPEN_PENDING"
        assert loser["intentKeys"] == [key]
        for worker in workers:
            worker.join(timeout=5)
            assert worker.exitcode == 0
    finally:
        winner_accepted.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)
        results.close()


@pytest.mark.parametrize(
    "foreign_trader,foreign_hash", [(True, True), (True, False), (False, True)]
)
def test_misattributed_history_keeps_blocking(
    keeper_replay, foreign_trader, foreign_hash
):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    fill = RESPONSES["filled_history"]["data"]["perp"]["tradeHistory"][0]
    row = {
        **fill,
        "trade": {**fill["trade"], "trader": "0x" + "b" * 40}
        if foreign_trader
        else fill["trade"],
        "txHash": "C" * 64 if foreign_hash else fill["txHash"],
    }
    history = {"data": {"perp": {"tradeHistory": [row]}}}
    replies.extend(("outcome_not_indexed", history, history))
    result = reconcile.reconcile(endpoint, ADDR, KEY)
    assert result["matchedTradeHistory"] == []
    assert status(endpoint) == "pending"
    with pytest.raises(ValueError, match=KEY):
        storage.pending("second", endpoint, ADDR, "OPEN_TRADE", "")


@pytest.mark.parametrize("key", [KEY, None])
def test_status_ignores_foreign_rows_and_finds_the_matching_intent(keeper_replay, key):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    other_addr = "0x" + "b" * 40
    storage.pending("foreign", endpoint, other_addr, "OPEN_TRADE", "")
    match = RESPONSES["pending_exec"]["data"]["userIntents"][0]
    foreign = {
        **match,
        "evmAddress": other_addr,
        "nibiAddress": None,
        "intentKey": "foreign",
    }
    replies.append({"data": {"userIntents": [foreign, match]}})
    rows = reconcile.reconcile(endpoint, ADDR, key)["userIntents"]
    assert [row["intentKey"] for row in rows] == [KEY]
    assert status(endpoint) == "pending"
    assert storage.list_pending(endpoint, other_addr)[0]["status"] == "unknown"


def test_terminal_ledger_ignores_late_admission_and_provisional_results():
    endpoint = "http://127.0.0.1:1/query"
    reserve(endpoint, expired=True)
    assert storage.set_status(KEY, endpoint, ADDR, "pending") == "pending"
    for stale_status in ("unknown", "accepted", "rejected", "not_sent"):
        assert storage.set_status(KEY, endpoint, ADDR, stale_status) == "pending"
    assert storage.mark_absent(KEY, endpoint, ADDR, 0) == "pending"

    assert storage.set_status(KEY, endpoint, ADDR, "terminal") == "terminal"
    for stale_status in ("unknown", "accepted", "pending", "rejected", "not_sent"):
        assert storage.set_status(KEY, endpoint, ADDR, stale_status) == "terminal"
    assert storage.mark_absent(KEY, endpoint, ADDR, 0) == "terminal"


@pytest.mark.parametrize("lookup_key", [KEY, None])
def test_late_admission_restores_block_after_an_empty_keeper_read(
    keeper_replay, lookup_key
):
    endpoint, replies = keeper_replay
    reserve(endpoint, expired=True)
    replies.extend(("absent", "accepted", "pending_exec"))
    assert reconcile.reconcile(endpoint, ADDR, KEY)["localStatus"] == "absent"
    recovered = reconcile.reconcile(endpoint, ADDR, lookup_key)
    outcome = recovered if lookup_key else recovered["userIntents"][0]
    assert outcome["outcome"] == "pending"
    assert status(endpoint) == "pending"
    with pytest.raises(ValueError, match=KEY):
        storage.pending("another-open", endpoint, ADDR, "OPEN_TRADE", "")
    assert reconcile.reconcile(endpoint, ADDR, KEY)["localStatus"] == "pending"


def test_unkeyed_admission_preserves_an_unknown_block(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    replies.append("accepted")
    reconcile.reconcile(endpoint, ADDR)
    assert status(endpoint) == "unknown"
    with pytest.raises(ValueError, match=KEY):
        storage.pending("another-open", endpoint, ADDR, "OPEN_TRADE", "")


@pytest.mark.parametrize(
    ("lookup_addr", "evm", "nibi"),
    [
        (NIBI_ADDR, OTHER_ADDR, NIBI_ADDR),
        (ADDR, ADDR, OTHER_NIBI_ADDR),
        (ADDR, ADDR, NIBI_ADDR[:-1] + "q"),
    ],
)
def test_conflicting_or_invalid_account_alias_cannot_clear_a_block(
    keeper_replay, lookup_addr, evm, nibi
):
    endpoint, replies = keeper_replay
    storage.pending(KEY, endpoint, evm, "OPEN_TRADE", "")
    storage.set_status(KEY, endpoint, evm, "pending")
    failure = RESPONSES["market_order_expired"]["data"]["userIntents"][0]
    replies.append(
        {"data": {"userIntents": [{**failure, "evmAddress": evm, "nibiAddress": nibi}]}}
    )

    assert reconcile.reconcile(endpoint, lookup_addr, KEY)["userIntents"] == []
    assert storage.list_pending(endpoint, evm)[0]["status"] == "pending"
    with pytest.raises(ValueError, match=KEY):
        storage.pending("another-open", endpoint, evm, "OPEN_TRADE", "")


def test_nibiru_lookup_updates_the_canonical_evm_ledger(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    replies.append("market_order_expired")
    data = reconcile.reconcile(endpoint, NIBI_ADDR.upper(), KEY)
    assert data["userIntents"][0]["lifecycleStep"] == "exec_fail"
    assert status(endpoint) == "terminal"


@pytest.mark.parametrize("provisional", ["pending_exec", "outcome_not_indexed"])
def test_market_pending_repairs_an_old_terminal_row(keeper_replay, provisional):
    endpoint, replies = keeper_replay
    reserve(endpoint, expired=True)
    with storage.db() as connection:
        # v0.1.0 wrote only status when classifying a server failure.
        connection.execute("UPDATE intents SET status='terminal' WHERE id=?", (KEY,))

    replies.append(provisional)
    if provisional == "outcome_not_indexed":
        replies.extend(("not_yet_in_history", "not_yet_in_history"))
    assert reconcile.reconcile(endpoint, ADDR, KEY)["localStatus"] == "pending"
    assert status(endpoint) == "pending"
    with pytest.raises(ValueError, match=KEY):
        storage.pending("another-open", endpoint, ADDR, "OPEN_TRADE", "")

    replies.append("market_order_expired")
    reconcile.reconcile(endpoint, ADDR, KEY)
    assert status(endpoint) == "terminal"
    replies.append("pending_exec")
    assert reconcile.reconcile(endpoint, ADDR, KEY)["localStatus"] == "terminal"
