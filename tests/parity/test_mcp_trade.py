"""Exercise the Python v0.1.3 and native MCP trade tools over stdio."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from scenarios import ADDRESS, OTHER_ADDRESS, loopback
from test_mcp_stdio import server
from test_trade import rows
from trade_replay import TradeReplay

OPEN = {
    "market_id": "003",
    "side": "long",
    "order_type": "market",
    "amount": "20",
    "leverage": "2",
    "slippage": "1",
}
POSITION = {"trade_id": 7}


@pytest.fixture
def mcp_pair(cli_pair: tuple[tuple[str, ...], tuple[str, ...]]) -> tuple[Path, Path]:
    _, (candidate,) = cli_pair
    reference = Path(
        os.getenv("SAI_PARITY_MCP_TRADE_PYTHON", Path(sys.executable).with_name("sai"))
    )
    assert reference.is_file(), "Install the Python v0.1.3 MCP reference"
    return reference, Path(candidate)


def result(frame: dict[str, Any]) -> dict[str, Any]:
    assert frame["isError"] is False
    text = frame["content"][0]["text"]
    assert frame["structuredContent"] == {"result": text}
    return json.loads(text)


def binding(preview: dict[str, Any]) -> dict[str, Any]:
    return {
        key: preview[key]
        for key in (
            "addr",
            "intent",
            "positionRoute",
            "previewDigest",
            "quote",
            "referencePrice",
        )
        if key in preview
    }


def trade_args(preview: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    result = {**args, "preview_digest": preview["previewDigest"]}
    if "referencePrice" in preview:
        result["reference_price"] = preview["referencePrice"]
    return result


@pytest.mark.parametrize(
    ("action", "args", "kind", "scenario"),
    [
        ("open", OPEN, "OPEN_TRADE", "quote-market"),
        ("close", POSITION, "CLOSE_POSITION", "quote-close"),
        (
            "close",
            {**POSITION, "cancel_order": True},
            "CANCEL_PENDING_TRADE",
            "pending-order",
        ),
        (
            "tpsl",
            {**POSITION, "take_profit": "110", "clear_stop_loss": True},
            "UPDATE_TRADE",
            "quote-close",
        ),
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
            {**POSITION, "percent": "050"},
            "DECREASE_POSITION_SIZE",
            "partial-price-changed",
        ),
    ],
    ids=[
        "open",
        "close",
        "cancel",
        "tpsl",
        "leverage",
        "margin",
        "partial-close-price-tick",
    ],
)
def test_mcp_preview_and_confirm(
    mcp_pair: tuple[Path, Path],
    tmp_path: Path,
    action: str,
    args: dict[str, Any],
    kind: str,
    scenario: str,
) -> None:
    observations = []
    for i, executable in enumerate(mcp_pair):
        home = tmp_path / str(i)
        replay = TradeReplay(kind, "" if action == "open" else "7")
        with (
            loopback(scenario, trade_replay=replay) as endpoints,
            server(
                executable, home, endpoints, seed="session", call_timeout=10
            ) as stdio,
        ):
            args = {**args, "addr": ADDRESS}
            preview = result(stdio.call(2, f"preview_{action}", **args))
            assert preview["dryRun"] is True
            assert preview["addr"] == ADDRESS
            assert preview["intent"]["kind"] == kind
            assert not replay.attempts()
            assert not rows(home)

            confirmed = trade_args(preview, args)
            refused = result(
                stdio.call(
                    3, f"trade_{action}", **{**confirmed, "preview_digest": "wrong"}
                )
            )
            assert refused["code"] == "PREVIEW_MISMATCH"
            assert refused["quoteStatus"] == "available"
            assert refused["recovery"]["action"] == "confirm_preview"
            assert refused["preview"]["previewDigest"] == preview["previewDigest"]
            assert not replay.attempts()
            assert not rows(home)

            accepted = result(stdio.call(4, f"trade_{action}", **confirmed))
            key = accepted.pop("intentKey")
            assert accepted["status"] == "accepted"
            assert accepted["addr"] == ADDRESS
            assert replay.attempts() == {key: 1}
            intent = replay.intent(key)
            semantic = {k: intent[k] for k in ("kind", "body", "target") if k in intent}
            expected = preview["intent"]
            if action == "open":
                expected = {
                    **expected,
                    "body": {
                        **expected["body"],
                        "intentPrice": accepted["referencePrice"],
                    },
                }
            assert semantic == expected
            assert [row[:6] for row in rows(home)] == [
                (key, kind, "accepted", "" if action == "open" else "7", ADDRESS, None)
            ]
            assert not endpoints.errors
            observations.append((binding(preview), accepted, semantic))
    assert observations[0] == observations[1]


@pytest.mark.parametrize(
    ("action", "args", "ready", "changed", "code", "execution"),
    [
        ("open", OPEN, "quote-market", "quote-market", "PRICE_MOVED", None),
        (
            "open",
            OPEN,
            "quote-market",
            "quote-close-only",
            "PREVIEW_MISMATCH",
            "MarketClosed",
        ),
        (
            "close",
            POSITION,
            "quote-close",
            "quote-stale",
            "PREVIEW_MISMATCH",
            "OraclePriceStale",
        ),
        (
            "leverage",
            {**POSITION, "leverage": "1.818181818181818182"},
            "quote-close",
            "quote-close-only",
            "PREVIEW_MISMATCH",
            "MarketClosed",
        ),
    ],
    ids=["price-moved", "market-close-only", "close-stale", "leverage-close-only"],
)
def test_mcp_refuses_changed_execution(
    mcp_pair: tuple[Path, Path],
    tmp_path: Path,
    action: str,
    args: dict[str, Any],
    ready: str,
    changed: str,
    code: str,
    execution: str | None,
) -> None:
    with (
        loopback(ready) as endpoints,
        server(mcp_pair[0], tmp_path / "preview", endpoints, seed="session") as stdio,
    ):
        preview = result(stdio.call(2, f"preview_{action}", **args))
        confirmed = trade_args(preview, args)
    replacements = []
    for i, executable in enumerate(mcp_pair):
        home = tmp_path / str(i)
        with (
            loopback(
                changed, market_price=102 if code == "PRICE_MOVED" else None
            ) as endpoints,
            server(executable, home, endpoints, seed="session") as stdio,
        ):
            refused = result(stdio.call(2, f"trade_{action}", **confirmed))
            assert refused["code"] == code
            fresh = refused["preview"]
            if execution is None:
                assert fresh["previewDigest"] != preview["previewDigest"]
                assert fresh["referencePrice"] == "102"
            else:
                assert fresh["estimates"]["execution"]["code"] == execution
                assert "previewDigest" not in fresh
                assert refused["quoteStatus"] == "unavailable"
                assert refused["recovery"]["action"] == "request_preview"
            assert not any(item.label == "recordUserIntent" for item in endpoints.wire)
            assert not rows(home)
            assert not endpoints.errors
            replacements.append(binding(fresh))
    assert replacements[0] == replacements[1]


def test_mcp_open_binds_the_selected_account(
    mcp_pair: tuple[Path, Path], tmp_path: Path
) -> None:
    for i, executable in enumerate(mcp_pair):
        home = tmp_path / str(i)
        with (
            loopback("quote-market") as endpoints,
            server(executable, home, endpoints, seed="accounts") as stdio,
        ):
            preview = result(stdio.call(2, "preview_open", **OPEN, addr=ADDRESS))
            assert preview["addr"] == ADDRESS
            confirmed = trade_args(preview, OPEN)
            refused = result(stdio.call(3, "trade_open", **confirmed))
            assert refused["code"] == "PREVIEW_MISMATCH"
            assert refused["preview"]["addr"] == OTHER_ADDRESS
            assert refused["preview"]["previewDigest"] != preview["previewDigest"]
            active_terms = trade_args(refused["preview"], OPEN)
            missing = result(stdio.call(4, "trade_open", **active_terms, addr=""))
            assert missing["code"] == "SESSION_UNAVAILABLE"
            assert not any(item.label == "recordUserIntent" for item in endpoints.wire)
            assert not rows(home)
            assert not endpoints.errors


def test_mcp_signed_out_open_is_informational(
    mcp_pair: tuple[Path, Path], tmp_path: Path
) -> None:
    for i, executable in enumerate(mcp_pair):
        home = tmp_path / str(i)
        with (
            loopback("quote-market") as endpoints,
            server(executable, home, endpoints) as stdio,
        ):
            preview = result(stdio.call(2, "preview_open", **OPEN))
            assert preview["addr"] is None
            assert preview["confirmationStatus"] == "informational"
            assert preview["recovery"]["action"] == "connect_and_preview"
            assert "previewDigest" not in preview
            refused = result(
                stdio.call(
                    3,
                    "trade_open",
                    **OPEN,
                    preview_digest="wrong",
                    reference_price="100",
                )
            )
            assert refused["code"] == "SIGNED_OUT"
            assert not any(item.label == "recordUserIntent" for item in endpoints.wire)
            assert not rows(home)
            assert not endpoints.errors


@pytest.mark.parametrize("lose_reply", [False, True], ids=["accepted", "unknown"])
def test_mcp_open_confirmation_reserves_once_until_overridden(
    mcp_pair: tuple[Path, Path], tmp_path: Path, lose_reply: bool
) -> None:
    for i, executable in enumerate(mcp_pair):
        home = tmp_path / str(i)
        replay = TradeReplay(
            "OPEN_TRADE", "", hold_admission=True, lose_reply=lose_reply
        )
        with (
            loopback("quote-market", trade_replay=replay) as endpoints,
            server(executable, home, endpoints, seed="session") as stdio,
        ):
            preview = result(stdio.call(2, "preview_open", **OPEN))
            confirmed = trade_args(preview, OPEN)
            stdio.send("tools/call", id_=3, name="trade_open", arguments=confirmed)
            try:
                assert replay.arrived.wait(8)
                [(key, _, status, *_)] = rows(home)
                assert status == "unknown"
                blocked = result(stdio.call(4, "trade_open", **confirmed))
                assert (blocked["code"], blocked["intentKeys"]) == (
                    "RECENT_OPEN_PENDING",
                    [key],
                )
                assert replay.attempts() == {key: 1}
                assert len(rows(home)) == 1
            finally:
                replay.release.set()
            reply = stdio.receive()
            assert reply["id"] == 3
            first = result(reply["result"])
            assert first["intentKey"] == key
            if lose_reply:
                assert first["code"] == "OUTCOME_UNKNOWN"
                assert "Do not resubmit" in first["next"]
            else:
                assert first["status"] == "accepted"

            override = result(
                stdio.call(5, "trade_open", **confirmed, allow_concurrent_open=True)
            )
            second = override["intentKey"]
            assert replay.attempts() == {key: 1, second: 1}
            blocked = result(stdio.call(6, "trade_open", **confirmed))
            assert blocked["intentKeys"] == [second, key]

            # Pending Keeper evidence blocks even an explicitly confirmed override.
            with (
                closing(sqlite3.connect(home / "sai/trader/intents.sqlite3")) as ledger,
                ledger,
            ):
                ledger.execute(
                    "UPDATE intents SET keeper_status='pending' WHERE id=?", (key,)
                )
            blocked = result(
                stdio.call(7, "trade_open", **confirmed, allow_concurrent_open=True)
            )
            assert (blocked["code"], blocked["intentKeys"]) == (
                "RECENT_OPEN_PENDING",
                [key],
            )
            assert replay.attempts() == {key: 1, second: 1}
            assert len(rows(home)) == 2
            assert not endpoints.errors
