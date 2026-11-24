"""Replay Keeper intent outcomes through HTTP and the real intent ledger."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
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
    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)


Reply = str | dict[str, Any] | Callable[[], dict[str, Any]] | SlowBody


@pytest.fixture
def keeper_replay() -> Iterator[tuple[str, deque[Reply]]]:
    replies: deque[Reply] = deque()
    active_key = KEY
    offset = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal active_key, offset
            length = int(self.headers["Content-Length"])
            try:
                payload = self.rfile.read(length)
            except ConnectionResetError:
                return
            if len(payload) != length:
                return
            request = json.loads(payload)
            replay = replies.popleft()
            slow = replay if isinstance(replay, SlowBody) else None
            if isinstance(replay, SlowBody):
                replay = "filled"
            if request["query"] == keeper.SUBMIT:
                active_key = request["variables"]["input"]["intentKey"]
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
                assert request["query"] == keeper.INTENTS
                assert request["variables"]["user"].lower() in (ADDR, NIBI_ADDR)
                assert request["variables"]["key"] in (active_key, None)
                if active_key != KEY:
                    for row in data["userIntents"]:
                        if row["intentKey"] == KEY:
                            row["intentKey"] = active_key
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
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if slow is None:
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)
                return
            slow.started.set()
            try:
                with suppress(BrokenPipeError, ConnectionResetError):
                    chunk = max(1, len(body) // 8)
                    for start in range(0, len(body), chunk):
                        slow.release.wait(0.08)
                        self.wfile.write(body[start : start + chunk])
            finally:
                slow.finished.set()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
        thread = threading.Thread(target=http.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{http.server_port}/query", replies
            assert not replies
        finally:
            http.shutdown()
            thread.join()


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
    keeper_replay, provisional
):
    endpoint, replies = keeper_replay
    storage.save_session(
        {
            "evmAddress": ADDR,
            "endpoint": endpoint,
            "token": "fixture-token",
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
                    },
                ]
            }
        }
    }
    trade = [
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
    cli = CliRunner()
    options = ["--endpoint", endpoint, "--json"]
    replies.extend((market, "admission_ack"))
    submitted = cli.invoke(main, [*options, *trade])
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

    blocked = cli.invoke(main, [*options, *trade])
    assert blocked.exit_code != 0
    assert f"Reconcile uncertain intent {key}" in blocked.output

    with storage.db() as connection:
        connection.execute("UPDATE intents SET created_at=0 WHERE id=?", (key,))
    mcp = server.create_server(settings.load(flags={"keeper.endpoint": endpoint}))
    args = {
        "market_id": 0,
        "side": "long",
        "order_type": "market",
        "amount": "10",
        "leverage": "2",
        "slippage": "1",
    }
    replies.append(market)
    preview = call_mcp(mcp, "preview_open", args)
    args.update(
        preview_digest=preview["previewDigest"],
        reference_price=preview["referencePrice"],
    )
    for allow in (False, True):
        result = call_mcp(mcp, "trade_open", {**args, "allow_concurrent_open": allow})
        assert result["code"] == "RECENT_OPEN_PENDING"
        assert result["intentKeys"] == [key]
        assert "next oracle price" in result["error"]
        assert "allow_concurrent_open" not in result["error"]


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


def test_wait_reports_fill_when_optional_history_uses_remaining_budget(keeper_replay):
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
    assert result["outcome"] == "filled"
    assert result["receipt"]["execution"]["block"] == 50109596
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


def test_cli_retry_with_wait_reports_acknowledged_open_key(keeper_replay):
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
    args = [
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
    ]
    runner = CliRunner()
    replies.extend((market, "admission_ack"))
    first = runner.invoke(main, args)
    assert first.exit_code == 0, first.output
    key = json.loads(first.output)["intentKey"]

    clock = FakeClock()
    replies.append("accepted")
    result = wait.for_intent(
        endpoint, ADDR, key, timeout=0.25, clock=wait.Clock(clock.now, clock.sleep)
    )
    assert result["outcome"] == "unknown"
    assert status(endpoint) == "accepted"

    retry = runner.invoke(main, [*args, "--wait"])
    assert retry.exit_code == 1, retry.output
    error = json.loads(retry.output)["error"]
    assert error["intentKey"] == key
    assert error["code"] == "RECENT_OPEN_PENDING"
    assert endpoint in error["hint"]
    assert len(storage.list_pending(endpoint, ADDR)) == 1

    replies.extend((market, "admission_ack"))
    allowed = runner.invoke(main, [*args, "--allow-concurrent-open"])
    assert allowed.exit_code == 0, allowed.output
    assert json.loads(allowed.output)["intentKey"] != key


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
    assert receipt["requestTxHash"] == "A" * 64
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
    assert (start["event"], end["event"], end["outcome"]) == (
        "intent.wait.start",
        "intent.wait.end",
        "filled",
    )
    assert start["intentKey"] == end["intentKey"] == KEY


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
    assert log.tail(1, name="intent.wait.end")[0]["outcome"] == "unknown"


def test_intent_wait_cli_returns_confirmed_failure(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    replies.extend(("pending_exec", "market_order_fill_failed"))
    result = CliRunner().invoke(
        main, ["--endpoint", endpoint, "--json", "intent", "wait", KEY]
    )
    assert result.exit_code == 1, result.output
    value = json.loads(result.output)
    assert value["outcome"] == "failed"
    assert value["keeperErrorCode"] == "market_order_fill_failed"
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
        log.tail(1, name="intent.wait.start")[0]["timeoutSeconds"]
        == wait.MCP_MAX_TIMEOUT
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
        assert f"intent wait {KEY}" in result["hint"]
        assert endpoint in result["hint"]
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
