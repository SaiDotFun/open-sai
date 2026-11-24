"""Replay Keeper intent outcomes through HTTP and the real intent ledger."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import closing, suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from multiprocessing.queues import Queue
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from loopback_keeper import KeeperHandler, keeper_server, position_data
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent
from sai_cli.core import keeper, log, reconcile, settings, storage, wait
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


@dataclass
class SlowBody:
    response: str = "filled"
    hold: bool = False
    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)


Reply = str | dict[str, Any] | Callable[[], dict[str, Any]] | SlowBody
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
    active_key = KEY
    offset = 0

    class Handler(KeeperHandler):
        def do_POST(self) -> None:
            nonlocal active_key, offset
            try:
                request = self.read_json()
            except (ConnectionResetError, json.JSONDecodeError):
                return
            if request["query"] == keeper.POSITION:
                self.reply({"data": position_data(request["variables"])})
                return
            if request["query"] == keeper.SUBMIT:
                active_key = request["variables"]["input"]["intentKey"]
                submitted_keys.append(active_key)
            replay = replies.popleft()
            slow = replay if isinstance(replay, SlowBody) else None
            if isinstance(replay, SlowBody):
                replay = replay.response
            if replay == "drop_ack":
                self.close_connection = True
                return
            reply: dict[str, Any]
            if replay == "reject":
                assert request["query"] == keeper.SUBMIT
                reply = {"errors": [{"extensions": {"code": "INTENT_NOT_ACCEPTED"}}]}
            elif replay in ("ack", "admission_ack"):
                assert request["query"] == keeper.SUBMIT
                if replay == "admission_ack":
                    submitted = request["variables"]["input"]["intent"]
                    assert submitted["kind"] == "OPEN_TRADE"
                    assert submitted["body"]["orderType"] == "market"
                row = RESPONSES["accepted"]["data"]["userIntents"][0]
                reply = {"data": {"recordUserIntent": {**row, "intentKey": active_key}}}
            else:
                if isinstance(replay, str):
                    reply = deepcopy(RESPONSES[replay])
                elif isinstance(replay, dict):
                    reply = deepcopy(replay)
                else:
                    reply = deepcopy(replay())
            data: dict[str, Any] = reply.get("data", {})
            if "userIntents" in data:
                assert request["query"] in (keeper.INTENTS, keeper.WAIT_INTENTS)
                assert request["variables"]["user"].lower() in (ADDR, NIBI_ADDR)
                key = request["variables"]["key"]
                assert key in (KEY, None, *submitted_keys)
                for row in data["userIntents"]:
                    if row["intentKey"] == KEY:
                        row["intentKey"] = key or active_key
                offset = 0
            elif "tradeHistory" in data.get("perp", {}):
                assert request["query"] == keeper.INTENT_HISTORY
                assert request["variables"]["trader"].lower() in (ADDR, NIBI_ADDR)
                assert request["variables"]["limit"] == reconcile.HISTORY_PAGE_SIZE
                assert request["variables"]["offset"] == offset
                rows = data["perp"]["tradeHistory"]
                for row in rows:
                    if row.get("intentKey") == KEY:
                        row["intentKey"] = active_key
                offset = (
                    offset + reconcile.HISTORY_PAGE_SIZE
                    if len(rows) == reconcile.HISTORY_PAGE_SIZE
                    else 0
                )
                if offset == reconcile.HISTORY_PAGE_SIZE * reconcile.HISTORY_MAX_PAGES:
                    offset = 0
            elif "borrowings" in data.get("perp", {}):
                assert request["query"] == keeper.MARKETS
            if slow is None:
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.reply(reply)
                return
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            slow.started.set()
            try:
                with suppress(BrokenPipeError, ConnectionResetError):
                    if slow.hold:
                        slow.release.wait(10)
                    chunk = max(1, len(body) // 8)
                    for start in range(0, len(body), chunk):
                        slow.release.wait(0.08)
                        self.wfile.write(body[start : start + chunk])
            finally:
                slow.finished.set()

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
    assert result["matchedTradeHistory"] == [
        {
            "id": 1007,
            "intentKey": KEY,
            "tradeChangeType": "position_opened",
            "txHash": "a" * 64,
            "block": {"block": 50109596, "block_ts": "2026-11-20T15:43:31.000Z"},
            "trade": {"id": 7, "trader": NIBI_ADDR},
        }
    ]
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


@pytest.mark.parametrize(
    ("surface", "lock"),
    [
        ("cli", "IMMEDIATE"),
        ("cli-wait", "IMMEDIATE"),
        ("cli-wait", "DEFERRED"),
        ("mcp", "IMMEDIATE"),
        ("mcp", "EXCLUSIVE"),
    ],
)
def test_locked_ledger_reports_open_was_not_sent(
    connected_replay: tuple[str, deque[Reply]],
    submitted_keys: list[str],
    surface: str,
    lock: str,
) -> None:
    endpoint, replies = connected_replay
    mcp = server.create_server(settings.load(flags={"keeper.endpoint": endpoint}))
    if surface == "mcp":
        replies.append(MARKET)
        preview = call_mcp(mcp, "preview_open", OPEN_ARGS)
        args = {
            **OPEN_ARGS,
            "preview_digest": preview["previewDigest"],
            "reference_price": preview["referencePrice"],
        }

    # IMMEDIATE lets MCP's precheck read the ledger, then blocks reservation.
    # DEFERRED with a read blocks the commit. EXCLUSIVE blocks the precheck.
    if lock != "EXCLUSIVE":
        replies.append(MARKET)
    with closing(storage.db()) as connection, connection, storage.busy_timeout(0):
        connection.execute(f"BEGIN {lock}")
        if lock == "DEFERRED":
            connection.execute("SELECT count(*) FROM intents").fetchone()
        if surface == "mcp":
            response = call_mcp(mcp, "trade_open", args)
            error, message = response, response["error"]
        else:
            flags = ["--wait"] if surface == "cli-wait" else []
            result = CliRunner().invoke(
                main, ["--endpoint", endpoint, "--json", *CLI_OPEN, *flags]
            )
            assert result.exit_code == 1, result.output
            response = json.loads(result.stdout)
            error, message = response["error"], response["error"]["message"]

    assert not submitted_keys
    assert not storage.list_pending(endpoint, ADDR)
    assert error["code"] == "INTERNAL_ERROR"
    assert "database is locked" in message
    assert "Nothing was sent" in message
    assert "intentKey" not in response and "intentKey" not in error
    assert "hint" not in error and "next" not in error


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


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.delays: list[float] = []

    def now(self) -> float:
        return self.elapsed

    def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.elapsed += delay


def test_wait_keeps_confirmation_when_optional_history_uses_remaining_budget(
    keeper_replay,
):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    clock = FakeClock()

    def confirmed():
        clock.elapsed = 0.9
        response = deepcopy(RESPONSES["filled"])
        response["data"]["userIntents"][0]["body"] = {"orderType": "market"}
        return response

    def late_history():
        clock.elapsed = 1.1
        return RESPONSES["not_yet_in_history"]

    replies.extend((confirmed, late_history))
    result = wait.for_intent(
        endpoint, ADDR, KEY, timeout=1, clock=wait.Clock(clock.now, clock.sleep)
    )
    assert result["outcome"] == "succeeded"
    assert result["receipt"]["execution"]["block"] == 50109596
    assert result["receipt"]["fill"] is None
    assert status(endpoint) == "terminal"


def test_wait_reports_aged_absence_when_ledger_releases_block(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint, expired=True)
    clock = FakeClock()
    replies.append("absent")
    result = wait.for_intent(
        endpoint, ADDR, KEY, clock=wait.Clock(clock.now, clock.sleep)
    )
    assert result["outcome"] == status(endpoint) == "absent"
    assert "local block was released" in result["message"]
    storage.pending("next-open", endpoint, ADDR, "OPEN_TRADE", "")


@pytest.mark.parametrize(
    ("disposition", "code"),
    [
        ("rejected", "ADMISSION_REJECTED"),
        ("not_sent", "NOT_SENT"),
    ],
)
@pytest.mark.parametrize("during_wait", [False, True])
def test_wait_reports_definitive_submit_disposition(
    keeper_replay, disposition, code, during_wait
):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    clock = FakeClock()

    def submitter_finishes():
        storage.set_status(KEY, endpoint, ADDR, disposition)
        return RESPONSES["started"]

    if during_wait:
        replies.append(submitter_finishes)
    else:
        storage.set_status(KEY, endpoint, ADDR, disposition)
    result = wait.for_intent(
        endpoint, ADDR, KEY, timeout=0.25, clock=wait.Clock(clock.now, clock.sleep)
    )
    assert result["outcome"] == status(endpoint) == disposition
    assert result["intentKey"] == KEY
    assert result["code"] == code
    storage.pending("next-open", endpoint, ADDR, "OPEN_TRADE", "")


@pytest.mark.parametrize("recorded", ["terminal", "absent"])
def test_wait_reports_a_released_local_block_when_keeper_is_unreachable(
    keeper_replay, recorded
):
    endpoint, replies = keeper_replay
    reserve(endpoint, expired=True)
    if recorded == "terminal":
        storage.set_status(KEY, endpoint, ADDR, "terminal")
    else:
        storage.mark_absent(KEY, endpoint, ADDR, keeper.ABSENT_AFTER_SECONDS)
    replies.append("drop_ack")
    clock = FakeClock()
    result = wait.for_intent(
        endpoint, ADDR, KEY, timeout=0.25, clock=wait.Clock(clock.now, clock.sleep)
    )
    assert result["outcome"] == recorded
    assert result.get("code") != "OUTCOME_UNKNOWN"


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess signals")
@pytest.mark.parametrize(
    ("admission", "before_interrupt", "held_response", "outcome", "exit_code"),
    [
        ("admission_ack", ["filled"], "filled_history", "terminal", 1),
        ("drop_ack", [], "accepted", "unknown", 5),
    ],
)
def test_sigint_during_trade_wait_preserves_original_key(
    keeper_replay, admission, before_interrupt, held_response, outcome, exit_code
):
    endpoint, replies = keeper_replay
    storage.save_session(
        {
            "token": "fake-token",
            "evmAddress": ADDR,
            "endpoint": endpoint,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
    )
    market = {
        "data": {
            "perp": {
                "borrowings": [
                    {
                        "marketId": 0,
                        "collateralToken": {"symbol": "USDC"},
                        "isOpen": True,
                        "price": 100,
                    }
                ]
            }
        }
    }
    slow = SlowBody(response=held_response, hold=True)
    replies.extend((market, admission, *before_interrupt, slow))
    command = [
        sys.executable,
        "-c",
        "from sai_cli.sai_cli import main; main()",
        "--endpoint",
        endpoint,
        "--json",
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
        "--wait",
    ]
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            assert slow.started.wait(5)
            [entry] = storage.list_pending(endpoint, ADDR)
            assert entry["status"] == outcome
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=5)
        finally:
            slow.release.set()
            if process.poll() is None:
                process.kill()
    assert process.returncode == exit_code, (stdout, stderr)
    result = json.loads(stdout)
    assert result["interrupted"] is True
    assert result["outcome"] == outcome
    assert result["error"]["intentKey"] == result["intentKey"] == entry["intentKey"]
    assert result["error"]["hint"] == storage.intent_status_hint(
        endpoint, ADDR, entry["intentKey"]
    )
    if outcome == "unknown":
        assert result["error"]["code"] == "OUTCOME_UNKNOWN"


# Inject in the child at function entry or return. Keeper traffic and SQLite
# writes still run through the installed CLI code with a disposable SAI_HOME.
WAIT_FAULT_DRIVER = """
import signal
import sqlite3
import sys

from sai_cli.core import keeper, storage
from sai_cli.sai_cli import main

module, name, event, selector, failure = sys.argv[1:6]
sys.argv[1:6] = []
ledger_lock = None

def inject(frame, what, arg):
    global ledger_lock
    if what != event or frame.f_code.co_name != name:
        return
    if frame.f_globals.get("__name__") != module:
        return
    if selector == "history" and frame.f_locals.get("query") != keeper.INTENT_HISTORY:
        return
    if selector.startswith("caller:") and frame.f_back.f_code.co_name != selector[7:]:
        return
    if selector == "reserved" and frame.f_locals.get("name") != "intent.reserved":
        return
    if selector == "submit" and frame.f_locals.get("query") != keeper.SUBMIT:
        return
    sys.setprofile(None)
    if failure == "sigint":
        signal.raise_signal(signal.SIGINT)
        return
    if failure == "commit-result-lost-locked":
        ledger_lock = storage.db()
        ledger_lock.execute("BEGIN IMMEDIATE")
    if failure.startswith("commit-result-lost"):
        raise sqlite3.OperationalError("Lost reservation result")
    raise RuntimeError("Injected failure")

sys.setprofile(inject)
if failure == "commit-result-lost-locked":
    with storage.busy_timeout(0):
        main()
else:
    main()
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess signals")
@pytest.mark.parametrize(
    ("hook", "fault", "json_output"),
    [
        (("log", "event", "call", "reserved"), "sigint", True),
        (("keeper", "_request", "call", "submit"), "sigint", True),
        (("storage", "pending", "return", ""), "commit-result-lost", True),
        (("storage", "pending", "return", ""), "commit-result-lost-locked", True),
        (("storage", "pending", "return", ""), "commit-result-lost-locked", False),
    ],
)
def test_trade_recovers_reservation_when_stopped_before_transport(
    connected_replay: tuple[str, deque[Reply]],
    submitted_keys: list[str],
    hook: tuple[str, str, str, str],
    fault: str,
    json_output: bool,
) -> None:
    endpoint, replies = connected_replay
    replies.append(MARKET)
    module, *target = hook
    output_flags = ["--json"] if json_output else []
    args = ["--endpoint", endpoint, *output_flags, *CLI_OPEN]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            WAIT_FAULT_DRIVER,
            f"sai_cli.core.{module}",
            *target,
            fault,
            *args,
            "--wait",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert submitted_keys == []
    [entry] = storage.list_pending(endpoint, ADDR)
    key = entry["intentKey"]
    cleanup_locked = fault == "commit-result-lost-locked"
    assert entry["status"] == ("unknown" if cleanup_locked else "not_sent")
    hint = storage.intent_status_hint(endpoint, ADDR, key)
    if not json_output:
        assert "Nothing was sent" in result.stderr
        assert hint in result.stderr
        return

    error = json.loads(result.stdout)["error"]
    assert error["code"] == (
        "INVALID_REQUEST" if fault == "sigint" else "INTERNAL_ERROR"
    )
    assert "Nothing was sent" in error["message"]
    if cleanup_locked:
        assert error["intentKey"] == key
        assert error["hint"] == hint
    else:
        assert "intentKey" not in error and "hint" not in error
        replies.extend((MARKET, "admission_ack"))
        retry = CliRunner().invoke(main, args)
        assert retry.exit_code == 0, retry.output
        assert submitted_keys == [json.loads(retry.stdout)["intentKey"]]
        assert key not in submitted_keys


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess signals")
@pytest.mark.parametrize("failure", ["sigint", "exception"])
@pytest.mark.parametrize(
    ("module", "name", "event", "selector", "phase"),
    [
        ("sai_cli.core.keeper", "request_hash", "call", "", "submit"),
        ("sai_cli.core.intents", "_record_submission_status", "call", "", "submit"),
        (
            "sai_cli.core.intents",
            "_record_submission_status",
            "return",
            "",
            "submit_return",
        ),
        ("sai_cli.core.intents", "submit", "return", "", "submit_return"),
        ("sai_cli.sai_cli", "emit_trade", "call", "", "waiting"),
        ("sai_cli.core.wait", "for_intent", "call", "", "both"),
        ("sai_cli.core.wait", "for_intent_async", "call", "", "both"),
        ("asyncio.tasks", "sleep", "call", "caller:for_intent_async", "waiting"),
        ("sai_cli.core.storage", "intent_record", "call", "", "both"),
        ("httpx._client", "__aenter__", "call", "", "waiting"),
        ("sai_cli.core.wait", "_reconcile", "call", "", "both"),
        ("asyncio.tasks", "sleep", "call", "caller:_reconcile", "waiting"),
        ("sai_cli.core.reconcile", "poll_steps", "call", "", "waiting"),
        ("sai_cli.core.keeper", "read_async", "call", "", "both"),
        ("sai_cli.core.wait", "pause", "call", "", "backoff"),
        ("sai_cli.core.keeper", "read_async", "call", "history", "history"),
        ("sai_cli.core.wait", "pause", "call", "", "history_backoff"),
        ("sai_cli.core.receipt", "success", "call", "", "terminal"),
        ("httpx._client", "__aexit__", "call", "", "terminal"),
        ("sai_cli.core.receipt", "build", "call", "", "terminal"),
        ("sai_cli.core.wait", "for_intent", "return", "", "terminal"),
        ("sai_cli.sai_cli", "emit_wait", "call", "", "terminal"),
        ("sai_cli.sai_cli", "emit_wait", "call", "", "lost_terminal"),
        ("sai_cli.sai_cli", "emit", "call", "", "terminal"),
        ("json", "dumps", "call", "caller:emit", "terminal"),
        ("click.utils", "echo", "call", "caller:emit", "terminal"),
        ("click.utils", "echo", "return", "caller:emit", "terminal"),
    ],
    ids=lambda value: value if value else None,
)
def test_trade_wait_keeps_key_through_final_print(
    keeper_replay, module, name, event, selector, phase, failure
):
    endpoint, replies = keeper_replay
    storage.save_session(
        {
            "token": "fake-token",
            "evmAddress": ADDR,
            "endpoint": endpoint,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
    )
    market = {
        "data": {
            "perp": {
                "borrowings": [
                    {
                        "marketId": 0,
                        "collateralToken": {"symbol": "USDC"},
                        "isOpen": True,
                        "price": 100,
                    }
                ]
            }
        }
    }
    replies.extend(
        (market, "drop_ack" if phase in ("both", "lost_terminal") else "admission_ack")
    )
    if phase == "backoff":
        replies.append("accepted")
    if phase in ("history", "history_backoff", "terminal", "lost_terminal"):
        replies.append("filled")
    if phase == "history_backoff":
        replies.append("not_yet_in_history")
    if phase in ("terminal", "lost_terminal"):
        replies.append(fill_history())
    command = [
        sys.executable,
        "-c",
        WAIT_FAULT_DRIVER,
        module,
        name,
        event,
        selector,
        failure,
        "--endpoint",
        endpoint,
        "--json",
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
        "--wait",
    ]
    process = subprocess.run(
        command, capture_output=True, text=True, timeout=10, check=False
    )
    [entry] = storage.list_pending(endpoint, ADDR)
    recorded = (
        "terminal"
        if phase in ("history", "history_backoff", "terminal", "lost_terminal")
        else ("unknown" if phase in ("submit", "both") else "accepted")
    )
    outcome = "terminal" if recorded == "terminal" else "unknown"
    assert entry["status"] == recorded
    assert process.returncode == (1 if outcome == "terminal" else 5), process
    if name == "echo":
        result = json.loads(process.stderr)
        if event == "return":
            assert json.loads(process.stdout)["intentKey"] == entry["intentKey"]
        else:
            assert process.stdout == ""
    else:
        result = json.loads(process.stdout)
    assert result["outcome"] == outcome
    assert result["localStatus"] == recorded
    assert result["interrupted"] == (failure == "sigint")
    assert result["error"]["intentKey"] == entry["intentKey"]
    assert result["error"]["hint"] == storage.intent_status_hint(
        endpoint, ADDR, entry["intentKey"]
    )


def fill_history() -> dict:
    response = deepcopy(RESPONSES["filled_history"])
    history = response["data"]["perp"]["tradeHistory"][0]
    history["marketOrder"] = {
        "orderReqBlock": 50109595,
        "orderReqTxHash": history["txHash"],
        "orderExecBlock": 50109596,
        "sequence": 49,
        "execPrice": 81000.5,
        "collateralAmount": 4000000,
    }
    history.update(openingFeeUsd=0.05, closingFeeUsd=0, borrowingFeeUsd=0)
    history["trade"].update(
        isOpen=True,
        openBlock={"block": 50109596},
        openPrice=81001.25,
        openCollateralAmount=3950000,
        collateralAmount=3950000,
        leverage=2,
    )
    return response


def test_wait_retries_provisional_sweep_and_returns_fill_receipt(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    replies.extend(
        (
            "pending_exec",
            "outcome_not_indexed",
            "not_yet_in_history",
            "not_yet_in_history",
            "filled",
            fill_history(),
        )
    )
    clock = FakeClock()
    result = wait.for_intent(
        endpoint, ADDR, KEY, clock=wait.Clock(clock.now, clock.sleep)
    )
    assert result["outcome"] == "filled"
    assert "code" not in result
    receipt = result["receipt"]
    assert receipt["requestTxHash"] == "a" * 64
    assert receipt["requestBlock"] == 50109595
    assert receipt["execution"]["block"] == 50109596
    assert receipt["execution"]["historyId"] == 1007
    assert receipt["execution"]["marketOrderSequence"] == 49
    assert receipt["fill"] == {
        "price": None,
        "oraclePrice": 81000.5,
        "sizeCollateralBaseUnits": None,
        "escrowedCollateralBaseUnits": 4000000,
    }
    assert receipt["positionFeesUsd"] == {"opening": 0.05, "closing": 0, "borrowing": 0}
    assert receipt["positionNow"]["id"] == 7
    assert clock.delays == [0.25, 0.25, 0.5]
    assert status(endpoint) == "terminal"
    start, end = log.tail(2, name="intent.wait")
    assert (start["event"], end["event"], end["status"]) == (
        "intent.wait.start",
        "intent.wait.end",
        "filled",
    )
    assert start["intentKey"] == end["intentKey"] == KEY


def test_wait_projects_receipt_fields_from_keeper(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    opaque = "opaque upstream value"
    history = fill_history()
    row = history["data"]["perp"]["tradeHistory"][0]
    row["marketOrder"]["execPrice"] = opaque
    row["trade"].update(openPrice=opaque, unrelated=opaque)
    row["openingFeeUsd"] = opaque
    replies.extend(("filled", history))

    outcome = wait.for_intent(endpoint, ADDR, KEY)
    assert outcome["outcome"] == "filled"
    receipt = outcome["receipt"]
    assert receipt["execution"]["historyId"] == 1007
    assert receipt["fill"]["oraclePrice"] is None
    assert receipt["positionNow"]["openPrice"] is None
    assert opaque not in json.dumps(outcome)


def test_wait_times_out_with_unknown_and_keeps_pending_ledger(keeper_replay):
    endpoint, replies = keeper_replay
    replies.extend(("pending_exec", "pending_exec"))
    reserve(endpoint)
    clock = FakeClock()
    result = wait.for_intent(
        endpoint, ADDR, KEY, timeout=0.75, clock=wait.Clock(clock.now, clock.sleep)
    )
    assert result["outcome"] == "unknown"
    assert result["code"] == "OUTCOME_UNKNOWN"
    assert result["receipt"]["execution"] is None
    assert result["receipt"]["requestBlock"] == 50109595
    assert clock.delays == [0.25, 0.5]
    assert status(endpoint) == "pending"
    assert log.tail(1, name="intent.wait.end")[0]["status"] == "unknown"


def test_intent_wait_cli_returns_confirmed_failure(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    failed = deepcopy(RESPONSES["market_order_fill_failed"])
    failed["data"]["userIntents"][0]["errorMsg"] = "opaque upstream error details"
    replies.extend(("pending_exec", failed))
    result = CliRunner().invoke(
        main, ["--endpoint", endpoint, "--json", "intent", "wait", KEY]
    )
    assert result.exit_code == 1, result.output
    value = json.loads(result.output)
    assert value["outcome"] == "failed"
    assert value["keeperErrorCode"] == "market_order_fill_failed"
    assert "opaque upstream error details" not in result.output
    assert value["receipt"]["fill"] is None
    assert status(endpoint) == "terminal"


def mcp_wait(endpoint: str, **args: Any) -> dict:
    mcp = server.create_server(settings.load(flags={"keeper.endpoint": endpoint}))
    return call_mcp(mcp, "intent_wait", {"key": KEY, **args})


def test_mcp_wait_caps_timeout_and_paginates_history(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    noise = {
        "data": {
            "perp": {
                "tradeHistory": [
                    {"intentKey": "another-intent", "trade": {"id": index}}
                    for index in range(reconcile.HISTORY_PAGE_SIZE)
                ]
            }
        }
    }
    replies.extend(("filled", noise, noise, fill_history()))
    result = mcp_wait(endpoint, timeout=300)
    assert result["addrSource"] == "ledger"
    assert result["outcome"] == "filled"
    assert result["receipt"]["execution"]["historyId"] == 1007
    assert (
        log.tail(1, name="intent.wait.start")[0]["timeoutMs"]
        == wait.MCP_MAX_TIMEOUT * 1000
    )


@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_wait_unknown_returns_call_again_hint(keeper_replay, surface):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    # The server drops the read before a response, so there is no outcome to infer.
    # The wait has only one poll before its first backoff reaches the deadline.
    replies.append("drop_ack")
    if surface == "cli":
        command = CliRunner().invoke(
            main,
            [
                "--endpoint",
                endpoint,
                "--json",
                "intent",
                "wait",
                KEY,
                "--timeout",
                "0.2",
            ],
        )
        assert command.exit_code == 5, command.output
        result = json.loads(command.output)
        assert result["hint"].endswith(f"intent wait {KEY} --addr {ADDR}")
        assert "SAI_RECONCILE_ENDPOINT" in result["hint"]
        assert endpoint not in result["hint"]
        assert result["error"]["intentKey"] == KEY
    else:
        result = mcp_wait(endpoint, timeout=0.2)
        assert "Call intent_wait again" in result["hint"]
    assert result["outcome"] == "unknown"
    assert result["code"] == "OUTCOME_UNKNOWN"
    assert status(endpoint) == "unknown"


def test_wait_times_out_during_a_slow_mcp_response(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    slow = SlowBody()
    replies.append(slow)
    started = time.monotonic()
    try:
        result = mcp_wait(endpoint, timeout=0.25)
        elapsed = time.monotonic() - started
    finally:
        slow.release.set()
    assert slow.finished.wait(2)
    assert slow.started.is_set()
    assert elapsed < 0.5
    assert result["outcome"] == "unknown"
    assert status(endpoint) == "unknown"


def test_mcp_cancelled_read_cannot_commit_a_keeper_outcome(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    slow = SlowBody()
    replies.append(slow)
    mcp = server.create_server(settings.load(flags={"keeper.endpoint": endpoint}))

    async def cancel():
        task = asyncio.create_task(mcp.call_tool("intent_wait", {"key": KEY}))
        try:
            assert await asyncio.to_thread(slow.started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            slow.release.set()
        assert await asyncio.to_thread(slow.finished.wait, 2)
        # Allow the server's remaining bytes to be delivered after cancellation.
        await asyncio.sleep(0.1)

    asyncio.run(cancel())
    assert status(endpoint) == "unknown"


def test_mcp_wait_does_not_block_on_initial_ledger_lookup(keeper_replay):
    endpoint, _ = keeper_replay
    reserve(endpoint)
    with storage.db() as connection:
        connection.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        result = mcp_wait(endpoint, timeout=0.25)
        assert time.monotonic() - started < 0.5
    assert result["outcome"] == "unknown"
    assert status(endpoint) == "unknown"


def test_cli_wait_budgets_initial_locked_ledger_lookup(keeper_replay):
    endpoint, _ = keeper_replay
    reserve(endpoint)
    with storage.db() as connection:
        connection.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with storage.busy_timeout(1):
            command = CliRunner().invoke(
                main,
                [
                    "--endpoint",
                    endpoint,
                    "--json",
                    "intent",
                    "wait",
                    KEY,
                    "--timeout",
                    "0.02",
                ],
            )
        assert time.monotonic() - started < 0.5
    assert command.exit_code == 5, command.output
    result = json.loads(command.stdout)
    assert result["error"]["code"] == "OUTCOME_UNKNOWN"
    assert result["error"]["intentKey"] == KEY
    hint = result["error"]["hint"]
    assert hint.endswith(f"intent wait {KEY}")
    assert "SAI_RECONCILE_ENDPOINT" in hint
    assert endpoint not in hint
    assert status(endpoint) == "unknown"


def test_wait_keeps_resized_position_out_of_original_fill(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    history = fill_history()
    position = history["data"]["perp"]["tradeHistory"][0]["trade"]
    position.update(
        openPrice=87000, openCollateralAmount=6000000, collateralAmount=6000000
    )
    replies.extend(("filled", history))
    result = wait.for_intent(endpoint, ADDR, KEY)
    receipt = result["receipt"]
    assert receipt["execution"]["historyId"] == 1007
    assert receipt["fill"]["price"] is None
    assert receipt["fill"]["sizeCollateralBaseUnits"] is None
    assert receipt["fill"]["oraclePrice"] == 81000.5
    assert receipt["positionNow"]["openPrice"] == 87000
    assert receipt["positionNow"]["openCollateralAmount"] == 6000000


@pytest.mark.parametrize(
    ("change", "outcome", "exit_code"),
    [
        ("position_liquidated", "liquidated", 1),
        ("position_closed_user", "unexpected", 1),
        ("trade_size_increase", "unexpected", 1),
        ("trade_size_decrease", "filled", 0),
    ],
)
def test_partial_close_wait_reports_observed_history(
    keeper_replay, change, outcome, exit_code
):
    endpoint, replies = keeper_replay
    storage.pending(KEY, endpoint, ADDR, "DECREASE_POSITION_SIZE", "7")
    completed = deepcopy(RESPONSES["filled"])
    completed["data"]["userIntents"][0]["kind"] = "DECREASE_POSITION_SIZE"
    history = fill_history()
    rows = history["data"]["perp"]["tradeHistory"]
    rows[0]["tradeChangeType"] = change
    rows[0]["trade"]["isOpen"] = change not in (
        "position_liquidated",
        "position_closed_user",
    )
    if outcome != "filled":
        # A plausible row must not hide another observed effect of this intent.
        rows.insert(
            0, {**rows[0], "id": 1006, "tradeChangeType": "trade_size_decrease"}
        )
    replies.extend((completed, history))
    command = CliRunner().invoke(
        main, ["--endpoint", endpoint, "--json", "intent", "wait", KEY]
    )
    assert command.exit_code == exit_code, command.output
    result = json.loads(command.stdout)
    assert result["outcome"] == outcome
    assert result["receipt"]["execution"]["tradeChangeType"] == change
    assert (result["receipt"]["fill"] is not None) == (outcome == "filled")
    if exit_code:
        assert result["error"]["code"] == "KEEPER_ERROR"
    assert status(endpoint) == "terminal"


def test_leverage_wait_flags_a_take_profit_update(keeper_replay):
    endpoint, replies = keeper_replay
    storage.pending(KEY, endpoint, ADDR, "UPDATE_TRADE", "7")
    completed = deepcopy(RESPONSES["filled"])
    row = completed["data"]["userIntents"][0]
    row.update(kind="UPDATE_TRADE", body={"requestedLeverage": "2"})
    history = fill_history()
    history["data"]["perp"]["tradeHistory"][0]["tradeChangeType"] = "tp_updated"
    replies.extend((completed, history))
    result = wait.for_intent(endpoint, ADDR, KEY)
    assert result["outcome"] == "unexpected"
    assert result["receipt"]["execution"]["tradeChangeType"] == "tp_updated"
    assert status(endpoint) == "terminal"


@pytest.mark.parametrize("pages", ["single", "split", "retry_split"])
@pytest.mark.parametrize(
    ("changes", "selected", "outcome", "exit_code"),
    [
        (
            ["after_hours_risk_trigger", "leverage_updated"],
            "leverage_updated",
            "succeeded",
            0,
        ),
        (
            ["leverage_updated", "after_hours_risk_trigger"],
            "leverage_updated",
            "succeeded",
            0,
        ),
        (
            ["after_hours_risk_trigger", "leverage_updated", "position_liquidated"],
            "position_liquidated",
            "liquidated",
            1,
        ),
        (["after_hours_risk_trigger"], "after_hours_risk_trigger", "unexpected", 1),
    ],
)
def test_leverage_wait_reports_after_hours_adjustment_as_an_extra_effect(
    keeper_replay, changes, selected, outcome, exit_code, pages
):
    endpoint, replies = keeper_replay
    storage.pending(KEY, endpoint, ADDR, "UPDATE_TRADE", "7")
    completed = deepcopy(RESPONSES["filled"])
    completed["data"]["userIntents"][0].update(
        kind="UPDATE_TRADE", body={"requestedLeverage": "2"}
    )
    history = fill_history()
    rows = history["data"]["perp"]["tradeHistory"]
    base = rows[0]
    rows[:] = [
        {**base, "tradeChangeType": change, "id": 2000 + index}
        for index, change in enumerate(changes)
    ]
    rows.append(
        {
            **base,
            "tradeChangeType": "after_hours_risk_trigger",
            "id": 999,
            "block": {"block": 50109520},
        }
    )
    replies.append(completed)
    if pages == "retry_split":
        replies.append("not_yet_in_history")
    if pages != "single":
        first = rows.pop(0)
        noise = [
            {**base, "intentKey": "unrelated", "id": 3000 + index}
            for index in range(reconcile.HISTORY_PAGE_SIZE - 1)
        ]
        replies.append({"data": {"perp": {"tradeHistory": [*noise, first]}}})
    replies.append(history)
    command = CliRunner().invoke(
        main, ["--endpoint", endpoint, "--json", "intent", "wait", KEY]
    )
    assert command.exit_code == exit_code, command.output
    result = json.loads(command.stdout)
    assert result["outcome"] == outcome
    execution = result["receipt"]["execution"]
    assert execution["tradeChangeType"] == selected
    assert execution["historyId"] == 2000 + changes.index(selected)
    assert execution["extraEffects"] == (
        []
        if selected == "after_hours_risk_trigger"
        else [
            {
                "tradeChangeType": "after_hours_risk_trigger",
                "historyId": 2000 + changes.index("after_hours_risk_trigger"),
                "block": 50109596,
            }
        ]
    )
    assert status(endpoint) == "terminal"


def test_wait_returns_conditional_order_placement_without_fill(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    replies.extend(("limit_placement_exec_pass", "limit_placement_history"))
    clock = FakeClock()
    result = wait.for_intent(
        endpoint, ADDR, KEY, clock=wait.Clock(clock.now, clock.sleep)
    )
    assert result["outcome"] == "placed"
    assert result["receipt"]["action"] == {"kind": "OPEN_TRADE", "orderType": "limit"}
    assert result["receipt"]["execution"]["tradeChangeType"] == "limit_order_created"
    assert result["receipt"]["fill"] is None
    assert status(endpoint) == "terminal"


@pytest.mark.parametrize("order_type", ["limit", "stop"])
def test_wait_reports_placement_even_when_history_is_empty(keeper_replay, order_type):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    completed = deepcopy(RESPONSES["limit_placement_exec_pass"])
    completed["data"]["userIntents"][0]["body"] = {"orderType": order_type}
    replies.extend((completed, "not_yet_in_history", "not_yet_in_history"))
    clock = FakeClock()
    result = wait.for_intent(
        endpoint, ADDR, KEY, clock=wait.Clock(clock.now, clock.sleep)
    )
    assert result["outcome"] == "placed"
    assert result["receipt"]["action"] == {
        "kind": "OPEN_TRADE",
        "orderType": order_type,
    }
    assert result["receipt"]["fill"] is None
    assert status(endpoint) == "terminal"


@pytest.mark.parametrize(
    ("admission", "exit_code", "outcome"),
    [
        ("ack", 0, "filled"),
        ("drop_ack", 0, "filled"),
        ("reject", 4, "rejected"),
    ],
)
def test_trade_wait_uses_submission_key(keeper_replay, admission, exit_code, outcome):
    endpoint, replies = keeper_replay
    storage.save_session(
        {
            "token": "fake-token",
            "evmAddress": ADDR,
            "endpoint": endpoint,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
    )
    replies.append(admission)
    if outcome == "filled":
        completed = deepcopy(RESPONSES["filled"])
        completed["data"]["userIntents"][0]["kind"] = "CLOSE_POSITION"
        history = fill_history()
        row = history["data"]["perp"]["tradeHistory"][0]
        row["tradeChangeType"] = "position_closed_user"
        row["trade"].update(
            isOpen=False, closeBlock={"block": 50109596}, closePrice=81001.25
        )
        replies.extend((completed, history))
    result = CliRunner().invoke(
        main, ["--endpoint", endpoint, "--json", "trade", "close", "7", "--wait"]
    )
    assert result.exit_code == exit_code, result.output
    value = json.loads(result.output)
    assert value["outcome"] == value["status"] == outcome
    local = storage.list_pending(endpoint, ADDR)
    assert [row["intentKey"] for row in local] == [value["intentKey"]]
    assert local[0]["status"] == ("terminal" if outcome == "filled" else "rejected")
    if outcome == "filled":
        assert value["receipt"]["fill"]["price"] is None
        assert value["receipt"]["positionNow"]["isOpen"] is False
