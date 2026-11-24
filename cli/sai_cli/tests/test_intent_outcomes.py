"""Replay Keeper intent outcomes through HTTP and the real intent ledger."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
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


@pytest.fixture
def keeper_replay() -> Iterator[tuple[str, deque[str]]]:
    replies: deque[str] = deque()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = RESPONSES[replies.popleft()]
            if "userIntents" in reply["data"]:
                assert request["query"] == keeper.INTENTS
                assert request["variables"]["user"] == ADDR
                assert request["variables"]["key"] in (KEY, None)
            else:
                assert request["query"] == keeper.HISTORY
                assert request["variables"] == {"trader": ADDR}
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
    response = asyncio.run(mcp.call_tool("intent_status", {"addr": ADDR, "key": KEY}))
    content = response[0] if isinstance(response, tuple) else response
    assert isinstance(content, list)
    block = content[0]
    assert isinstance(block, TextContent)
    result = json.loads(block.text)
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


def test_unkeyed_status_reports_next_oracle_wait_without_writing(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint)
    replies.append("pending_exec")
    result = reconcile.reconcile(endpoint, ADDR)
    assert result["userIntents"][0]["outcome"] == "pending"
    assert "Do not resubmit" in result["userIntents"][0]["hint"]
    assert status(endpoint) == "unknown"


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


def test_late_admission_restores_block_after_an_empty_keeper_read(keeper_replay):
    endpoint, replies = keeper_replay
    reserve(endpoint, expired=True)
    replies.extend(("absent", "accepted", "pending_exec"))
    assert reconcile.reconcile(endpoint, ADDR, KEY)["localStatus"] == "absent"
    assert reconcile.reconcile(endpoint, ADDR, KEY)["localStatus"] == "pending"
    with pytest.raises(ValueError, match=KEY):
        storage.pending("another-open", endpoint, ADDR, "OPEN_TRADE", "")
    assert reconcile.reconcile(endpoint, ADDR, KEY)["localStatus"] == "pending"


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
