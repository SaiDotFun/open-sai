"""Check native MCP results against the output schemas the server lists."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from scenarios import ADDRESS, INTENT, loopback
from test_mcp_stdio import Stdio, native, server, tool_text
from trade_replay import TradeReplay

__all__ = ["native"]

POSITION = {"trade_id": 7, "addr": ADDRESS}
TRADES = (
    (
        "open",
        {
            "market_id": 3,
            "side": "long",
            "order_type": "market",
            "amount": "20",
            "leverage": "2",
            "slippage": "1",
            "addr": ADDRESS,
        },
        "OPEN_TRADE",
        "quote-market",
    ),
    ("close", POSITION, "CLOSE_POSITION", "quote-close"),
    ("tpsl", {**POSITION, "take_profit": "110"}, "UPDATE_TRADE", "quote-close"),
    (
        "leverage",
        {**POSITION, "leverage": "1.818181818181818182"},
        "UPDATE_TRADE",
        "quote-close",
    ),
    (
        "margin",
        {**POSITION, "direction": "add", "amount": "1"},
        "UPDATE_TRADE",
        "position-default",
    ),
    (
        "partial_close",
        {**POSITION, "percent": "50"},
        "DECREASE_POSITION_SIZE",
        "partial-price-changed",
    ),
)


class Checked:
    """Call native tools and validate each result against its listed schema."""

    def __init__(self, stdio: Stdio, covered: set[str]) -> None:
        stdio.send("tools/list", id_=2)
        tools = stdio.receive()["result"]["tools"]
        self.validators = {}
        for tool in tools:
            schema = tool["outputSchema"]
            Draft202012Validator.check_schema(schema)
            self.validators[tool["name"]] = Draft202012Validator(schema)
        self.stdio = stdio
        self.covered = covered
        self.next_id = 3

    def __call__(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = self.stdio.call(self.next_id, name, **arguments)
        self.next_id += 1
        value = json.loads(tool_text(result))
        assert result["structuredContent"] == value
        self.validators[name].validate(value)
        self.covered.add(name)
        return value


@contextmanager
def checked(
    native: Path,
    home: Path,
    covered: set[str],
    scenario: str,
    seed: str,
    replay: TradeReplay | None = None,
) -> Iterator[Checked]:
    with (
        loopback(scenario, trade_replay=replay) as endpoints,
        server(native, home, endpoints, seed=seed, receive_timeout=15) as stdio,
    ):
        yield Checked(stdio, covered)


def test_native_results_match_their_output_schemas(
    native: Path, tmp_path: Path
) -> None:
    covered: set[str] = set()
    home = iter(tmp_path / str(index) for index in range(100))

    with checked(native, next(home), covered, "normal", "empty") as call:
        tools = set(call.validators)
        assert call("mcp_info")["latestVersion"] is None
        assert call("auth_status")["code"] == "SIGNED_OUT"
        assert call("auth_list") == {"accounts": []}
        assert call("trade_close", trade_id=7, preview_digest="x")["code"] == (
            "SIGNED_OUT"
        )

    with checked(native, next(home), covered, "normal", "session") as call:
        assert call("auth_status")["addr"] == ADDRESS
        assert call("auth_use", addr=ADDRESS)["accounts"]
        assert "perp" in call("market_list")
        assert "oracle" in call("market_prices")
        assert call("account_overview")["addrSource"] == "session"
        assert call("logs_intent", key=INTENT, offline=True)["keeper"] is None

    with checked(native, next(home), covered, "mcp-reads", "ledger") as call:
        for tool in ("account_positions", "account_history", "account_balances"):
            assert call(tool, addr=ADDRESS)["addr"] == ADDRESS
        assert call("intent_local", addr=ADDRESS)["intents"]
        assert "userIntents" in call("intent_status", addr=ADDRESS, key=INTENT)

    with checked(native, next(home), covered, "mcp-wait-final", "ledger") as call:
        assert call("intent_wait", key=INTENT)["outcome"] == "filled"

    locked = next(home)
    with (
        checked(native, locked, covered, "normal", "ledger") as call,
        closing(sqlite3.connect(locked / "sai/trader/intents.sqlite3")) as database,
    ):
        # A busy ledger ends the wait before the account resolves.
        database.execute("BEGIN EXCLUSIVE")
        unresolved = call("intent_wait", key=INTENT)
        assert unresolved["code"] == "OUTCOME_UNKNOWN"
        assert unresolved["addr"] is None

    with checked(native, next(home), covered, "mcp-wait-pending", "ledger") as call:
        unknown = call("intent_wait", key=INTENT, addr=ADDRESS, timeout=0.7)
        assert unknown["code"] == "OUTCOME_UNKNOWN"

    for action, args, kind, scenario in TRADES:
        replay = TradeReplay(kind, "" if action == "open" else "7")
        with checked(native, next(home), covered, scenario, "session", replay) as call:
            preview = call(f"preview_{action}", **args)
            confirmed = {**args, "preview_digest": preview["previewDigest"]}
            if "referencePrice" in preview:
                confirmed["reference_price"] = preview["referencePrice"]
            refused = call(f"trade_{action}", **{**confirmed, "preview_digest": "x"})
            assert refused["code"] == "PREVIEW_MISMATCH"
            assert call(f"trade_{action}", **confirmed)["status"] == "accepted"

    assert covered == tools
