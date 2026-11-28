"""Compare Rust intent receipts with the pinned Python CLI on loopback."""

from __future__ import annotations

import json
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sai_cli.core import keeper, storage
from test_intent_status import (
    ADDR,
    CLI,
    KEY,
    OTHER,
    RESPONSES,
    Reply,
    Request,
    cli_environment,
    invoke,
    read_events,
    replay,
    response,
    seed,
)

# Rust adds a dollar field beside each receipt collateral amount in base
# units. The pinned Python reports base units only (#231). Rust also reports
# a close's realized PnL (#268) and a placed order's ID (#271).
RUST_RECEIPT_FIELDS = re.compile(
    rb', "(?:sizeCollateralUsd|escrowedCollateralUsd|collateralUsd|openCollateralUsd)"'
    rb': (?:null|"[0-9.]+")'
    rb'|, "realizedPnl": null|, "orderId": (?:null|[0-9]+)'
)
REQUEST_BLOCK = 50109595


def usd(units: int) -> str:
    return format(Decimal(units).scaleb(-6), "f").rstrip("0").rstrip(".")


FILL_BLOCK = 50109596
OPAQUE = "fixture-upstream-private-text"
PRIVATE_KEEPER_HINT = (
    '"${SAI_RECONCILE_ENDPOINT:?Set SAI_RECONCILE_ENDPOINT to the original Keeper URL}"'
)


def with_timeout_warning(reference: dict[str, Any]) -> dict[str, Any]:
    """Apply the current CLI warning to a receipt from the older Python pin."""
    message = "The intent outcome is still unknown. Do not resubmit."
    return {
        **reference,
        "message": message,
        "error": {**reference["error"], "message": message},
    }


def intent(name: str, **fields: object) -> dict[str, Any]:
    result = deepcopy(RESPONSES[name])
    result["data"]["userIntents"][0].update(fields)
    return result


def history(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"perp": {"tradeHistory": list(rows)}}}


def indexed(change: str = "position_opened", **fields: object) -> dict[str, Any]:
    row = deepcopy(RESPONSES["filled_history"]["data"]["perp"]["tradeHistory"][0])
    row.update(
        tradeChangeType=change,
        marketOrder={
            "orderReqBlock": REQUEST_BLOCK,
            "orderReqTxHash": "A" * 64,
            "orderExecBlock": FILL_BLOCK,
            "sequence": 49,
            "execPrice": "81000.50",
            "collateralAmount": 4_000_000,
        },
        openingFeeUsd=0.05,
        closingFeeUsd=0,
        borrowingFeeUsd=OPAQUE,
        ignored=OPAQUE,
    )
    row["trade"].update(
        isOpen=True,
        tradeType="trade",
        openBlock={"block": FILL_BLOCK},
        openPrice=87_000,
        openCollateralAmount=6_000_000,
        collateralAmount=6_000_000,
        leverage=2,
        tp=OPAQUE,
    )
    row.update(fields)
    return row


@dataclass(frozen=True)
class Case:
    name: str
    replies: tuple[str | dict[str, Any] | Reply, ...]
    outcome: str
    status: str
    requests: tuple[int | None, ...]
    exit: int = 0
    timeout: str = "3"
    start: str = "unknown"
    code: str | None = None
    explicit_addr: bool = False


CASES = (
    Case(
        "provisional-then-fill",
        (
            "pending_exec",
            "outcome_not_indexed",
            "not_yet_in_history",
            "not_yet_in_history",
            intent("filled", body={"orderType": "market"}),
            history(indexed()),
        ),
        "filled",
        "terminal",
        (None, None, 0, 0, None, 0),
    ),
    Case(
        "provisional-corroborated",
        ("outcome_not_indexed", history(indexed())),
        "filled",
        "terminal",
        (None, 0),
    ),
    Case(
        "numeric-fee-and-quoted-decimal",
        (
            "filled",
            Reply(
                body=response(
                    history(
                        indexed(
                            openingFeeUsd=1.23,
                            closingFeeUsd="1.2300",
                        )
                    )
                ).body.replace(b'"openingFeeUsd": 1.23,', b'"openingFeeUsd": 1.2300,')
            ),
        ),
        "filled",
        "terminal",
        (None, 0),
    ),
    Case(
        "market-close",
        (
            intent("filled", kind="CLOSE_POSITION"),
            history(indexed("position_closed_user")),
        ),
        "filled",
        "terminal",
        (None, 0),
    ),
    Case(
        "confirmed-failure",
        ("pending_exec", intent("market_order_fill_failed", errorMsg=OPAQUE)),
        "failed",
        "terminal",
        (None, None),
        exit=1,
        code="KEEPER_ERROR",
    ),
    Case(
        "placement-without-history",
        (intent("filled", body={"orderType": "limit"}), history(), history()),
        "placed",
        "terminal",
        (None, 0, 0),
    ),
    Case(
        "liquidation-on-next-page",
        (
            intent("filled", body={"orderType": "market"}),
            history(
                indexed(),
                *(indexed(id=index, intentKey="other") for index in range(24)),
            ),
            history(
                indexed(),
                indexed("position_liquidated", id=1008),
                indexed("after_hours_risk_trigger", id=1009),
            ),
        ),
        "liquidated",
        "terminal",
        (None, 0, 25),
        exit=1,
        code="KEEPER_ERROR",
    ),
    Case(
        "action-mismatch",
        (
            intent("filled", kind="UPDATE_TRADE", body={"requestedLeverage": "2"}),
            history(indexed("tp_updated")),
        ),
        "unexpected",
        "terminal",
        (None, 0),
        exit=1,
        code="KEEPER_ERROR",
    ),
    Case(
        "action-confirmed",
        (
            intent("filled", kind="UPDATE_OPEN_ORDER"),
            history(indexed("order_updated")),
        ),
        "succeeded",
        "terminal",
        (None, 0),
    ),
    Case(
        "pending-times-out",
        ("pending_exec",),
        "unknown",
        "pending",
        (None,),
        exit=5,
        timeout="0.2",
        code="OUTCOME_UNKNOWN",
    ),
    Case(
        "slow-final-never-commits",
        (
            Reply(
                body=response("filled").body,
                body_delay=1.25,
            ),
        ),
        "unknown",
        "unknown",
        (None,),
        exit=5,
        timeout="0.45",
        code="OUTCOME_UNKNOWN",
        explicit_addr=True,
    ),
    Case("absent-after-grace", ("absent",), "absent", "absent", (None,), exit=1),
    Case(
        "recorded-terminal",
        (Reply(disconnect=True),),
        "terminal",
        "terminal",
        (None,),
        exit=1,
        timeout="0.2",
        start="terminal",
    ),
    Case(
        "recorded-rejected",
        (),
        "rejected",
        "rejected",
        (),
        exit=4,
        start="rejected",
        code="ADMISSION_REJECTED",
    ),
    Case(
        "recorded-not-sent",
        (),
        "not_sent",
        "not_sent",
        (),
        exit=4,
        start="not_sent",
        code="NOT_SENT",
    ),
    Case(
        "different-account",
        (intent("filled", evmAddress=OTHER, nibiAddress=None),),
        "unknown",
        "unknown",
        (None,),
        exit=5,
        timeout="0.2",
        code="OUTCOME_UNKNOWN",
    ),
)


def ledger_state(home: Path) -> list[tuple[str, str, str | None]]:
    path = home / "trader/intents.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        return connection.execute(
            "SELECT id, status, keeper_status FROM intents ORDER BY id"
        ).fetchall()


def assert_wait_keeps_interrupted_fill(
    cli: CLI, home: Path, monkeypatch: pytest.MonkeyPatch, *, native: bool = False
) -> None:
    lookup_started = threading.Event()
    release_lookup = threading.Event()

    def hold_lookup(request: Request) -> Reply:
        assert "lastUpdatedBlock { block }" in json.loads(request.body)["query"]
        lookup_started.set()
        release_lookup.wait(5)
        return Reply(disconnect=True)

    fill = indexed()
    fill["trade"].update(
        openPrice=81001.25, openCollateralAmount=3_950_000, collateralAmount=3_950_000
    )
    with replay() as (endpoint, replies, requests):
        seed(home, endpoint, monkeypatch, "unknown")
        replies.extend(
            (
                response(
                    intent("filled", body={"orderType": "market", "leverage": "2"})
                ),
                response(history(fill)),
                hold_lookup,
            )
        )
        with subprocess.Popen(
            [*cli, "--endpoint", endpoint, "--json", "intent", "wait", KEY],
            env=cli_environment(home),
            cwd=home.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                assert lookup_started.wait(5), "CLI did not start the fill lookup"
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=1.5)
            finally:
                release_lookup.set()
                if process.poll() is None:
                    process.kill()
            assert (process.returncode, stderr) == (0, b""), stdout

        document = json.loads(stdout)
        assert document["outcome"] == "filled"
        assert document["intentKey"] == KEY
        receipt = document["receipt"]
        assert receipt["execution"]["block"] == FILL_BLOCK
        assert receipt["execution"]["historyId"] == fill["id"]
        if native:
            assert receipt["fill"].pop("sizeCollateralUsd") is None
            assert receipt["fill"].pop("escrowedCollateralUsd") == "4"
        assert receipt["fill"] == {
            "price": None,
            "sizeCollateralBaseUnits": None,
            "oraclePrice": "81000.50",
            "escrowedCollateralBaseUnits": 4_000_000,
        }
        assert ledger_state(home)[0] == (KEY, "terminal", "terminal")
        assert len(requests) == 3
        assert not replies


def test_python_wait_keeps_interrupted_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_wait_keeps_interrupted_fill(
        (str(Path(sys.executable).with_name("sai")),), tmp_path / "sai", monkeypatch
    )


def test_rust_wait_keeps_interrupted_fill(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_wait_keeps_interrupted_fill(
        cli_pair[1], tmp_path / "sai", monkeypatch, native=True
    )


def test_wait_scrubs_a_stored_token_shaped_like_a_history_hash(
    wait_cli_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "e1" * 32
    with replay() as (endpoint, replies, requests):
        for index, cli in enumerate(wait_cli_pair):
            home = tmp_path / str(index) / "sai"
            seed(home, endpoint, monkeypatch, "unknown")
            storage.save_session(
                {
                    "evmAddress": ADDR,
                    "endpoint": endpoint,
                    "expiresAt": "2099-01-01T00:00:00Z",
                    "token": token,
                }
            )
            requests.clear()
            replies.extend(
                map(
                    response,
                    (
                        "user_finality",
                        history(
                            indexed(
                                "position_closed_user",
                                txHash=token,
                                evmTxHash="b" * 64,
                                marketOrder=None,
                            )
                        ),
                    ),
                )
            )
            result = invoke(
                cli, home, "--endpoint", endpoint, "--json", "intent", "wait", KEY
            )
            assert token.encode() not in result.stdout + result.stderr, (
                "receipt disclosed a stored token"
            )
            assert result.returncode == 0
            receipt = json.loads(result.stdout)["receipt"]
            assert receipt["requestTxHash"] == "[redacted session token]"
            assert len(requests) == 2
            assert all("authorization" not in request.headers for request in requests)


@pytest.mark.parametrize(
    ("snapshot", "requested_leverage", "price", "size"),
    [
        ({}, "2", 81001.25, 7_900_000),
        ({}, "1.999999999999999999", 81001.25, 7_899_999),
        ({"lastUpdatedBlock": {"block": FILL_BLOCK + 1}}, "2", None, None),
        ({"trader": OTHER}, "2", None, None),
        ({"openPrice": OPAQUE}, "2", None, 7_900_000),
        ({}, None, 81001.25, None),
        ({}, "3", 81001.25, None),
        (None, "2", None, None),
        (response({"errors": [{"message": OPAQUE}]}), "2", None, None),
    ],
    ids=[
        "opening-fill",
        "floor-exact-leverage",
        "updated-since-fill",
        "wrong-owner",
        "missing-price",
        "missing-exact-leverage",
        "inconsistent-leverage",
        "missing-position",
        "lookup-error",
    ],
)
def test_wait_requires_execution_block_position_for_fill(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    snapshot: dict[str, Any] | Reply | None,
    requested_leverage: str | None,
    price: float | None,
    size: int | None,
) -> None:
    monkeypatch.delenv("SAI_PARITY_PYTHON", raising=False)
    clis: tuple[CLI, CLI] = request.getfixturevalue("cli_pair")
    fill = indexed()
    fill["trade"].update(
        openPrice=81001.25, openCollateralAmount=3_950_000, collateralAmount=3_950_000
    )
    if isinstance(snapshot, Reply):
        lookup = snapshot
    else:
        position = (
            None
            if snapshot is None
            else {
                **fill["trade"],
                "lastUpdatedBlock": {"block": FILL_BLOCK},
                **snapshot,
            }
        )
        lookup = response({"data": {"perp": {"trade": position}}})
    for index, cli in enumerate(clis):
        with replay() as (endpoint, replies, requests):
            home = tmp_path / str(index) / "sai"
            seed(home, endpoint, monkeypatch, "unknown")
            replies.extend(
                map(
                    response,
                    (
                        intent(
                            "filled",
                            body={
                                "orderType": "market",
                                "leverage": requested_leverage,
                            },
                        ),
                        history(fill),
                        lookup,
                    ),
                )
            )
            result = invoke(
                cli, home, "--endpoint", endpoint, "--json", "intent", "wait", KEY
            )
            assert (result.returncode, result.stderr) == (0, b"")
            assert OPAQUE.encode() not in result.stdout
            document = json.loads(result.stdout)
            assert document["outcome"] == "filled"
            expected = {
                "price": price,
                "sizeCollateralBaseUnits": size,
                "oraclePrice": "81000.50",
                "escrowedCollateralBaseUnits": 4_000_000,
            }
            if index:
                expected |= {
                    "sizeCollateralUsd": None if size is None else usd(size),
                    "escrowedCollateralUsd": "4",
                }
            assert document["receipt"]["fill"] == expected
            assert len(requests) == 3
            query = json.loads(requests[-1].body)
            assert "lastUpdatedBlock { block }" in query["query"]
            assert query["variables"] == {"trader": ADDR, "id": fill["trade"]["id"]}
            assert "authorization" not in requests[-1].headers
            assert not replies


def closed(change: str = "position_closed_user", **trade: object) -> dict[str, Any]:
    row = indexed(change)
    row["trade"].update(
        {
            "isOpen": False,
            "closeBlock": {"block": FILL_BLOCK},
            "closePrice": 81001.25,
            "collateralAmount": 3_950_000,
            **trade,
        }
    )
    return row


def pnl_page(row_id: int = 1007, **fields: object) -> dict[str, Any]:
    return {
        "data": {
            "perp": {
                "tradeHistory": [
                    {
                        "id": 1008,
                        "tradeChangeType": "position_opened",
                        "grossPricePnlPct": None,
                        "grossPricePnlCollateral": None,
                    },
                    {
                        "id": row_id,
                        "tradeChangeType": "position_closed_user",
                        "grossPricePnlPct": -0.002531144239360719,
                        "grossPricePnlCollateral": -5057.985533514525,
                        **fields,
                    },
                ]
            }
        }
    }


REALIZED = {
    "basis": "grossBeforeFees",
    "collateralBaseUnits": -5057.985533514525,
    "usd": "-0.005058",
    "percent": "-0.2531144239360719",
}


@pytest.mark.parametrize(
    ("kind", "row", "lookup", "price", "realized"),
    [
        ("CLOSE_POSITION", closed(), pnl_page(), 81001.25, REALIZED),
        (
            "CLOSE_POSITION",
            closed(
                perpBorrowing={"marketId": 0, "collateralToken": {"symbol": "stNIBI"}}
            ),
            pnl_page(),
            81001.25,
            {**REALIZED, "usd": None},
        ),
        ("CLOSE_POSITION", closed(), pnl_page(row_id=999), 81001.25, None),
        (
            "CLOSE_POSITION",
            closed(),
            pnl_page(grossPricePnlCollateral=None),
            81001.25,
            None,
        ),
        (
            "CLOSE_POSITION",
            closed(),
            response({"errors": [{"message": OPAQUE}]}),
            81001.25,
            None,
        ),
        ("CLOSE_POSITION", closed(isOpen=True), None, None, None),
        (
            "CLOSE_POSITION",
            closed(closeBlock={"block": FILL_BLOCK + 1}),
            None,
            None,
            None,
        ),
        ("CLOSE_POSITION", closed(closePrice=0), None, None, None),
        ("DECREASE_POSITION_SIZE", closed("trade_size_decrease"), None, None, None),
    ],
    ids=[
        "full-close",
        "non-usdc-collateral",
        "pnl-row-missing",
        "pnl-unreported",
        "pnl-lookup-error",
        "position-still-open",
        "closed-in-later-block",
        "missing-close-price",
        "partial-close",
    ],
)
def test_rust_close_receipt_fills_price_and_realized_pnl(
    cli_pair: tuple[CLI, CLI],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
    row: dict[str, Any],
    lookup: dict[str, Any] | Reply | None,
    price: float | None,
    realized: dict[str, Any] | None,
) -> None:
    """Fill a close receipt only when Keeper ties it to the closing fill (#268)."""
    with replay() as (endpoint, replies, requests):
        home = tmp_path / "sai"
        seed(home, endpoint, monkeypatch, "unknown")
        replies.extend(map(response, (intent("filled", kind=kind), history(row))))
        if lookup is not None:
            replies.append(response(lookup))
        result = invoke(
            cli_pair[1], home, "--endpoint", endpoint, "--json", "intent", "wait", KEY
        )
        assert (result.returncode, result.stderr) == (0, b"")
        assert OPAQUE.encode() not in result.stdout
        document = json.loads(result.stdout)
        assert document["outcome"] == "filled"
        fill = document["receipt"]["fill"]
        assert fill["price"] == price
        assert fill["realizedPnl"] == realized
        assert fill["oraclePrice"] == "81000.50"
        assert not replies
        assert len(requests) == 2 + (lookup is not None)
        if lookup is not None:
            query = json.loads(requests[-1].body)
            assert "grossPricePnlCollateral" in query["query"]
            assert query["variables"] == {"trader": ADDR, "limit": 25, "offset": 0}
            assert "authorization" not in requests[-1].headers


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_intent_wait(
    wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: Case,
) -> None:
    observed = []
    with replay() as (endpoint, replies, requests):
        for index, cli in enumerate(wait_cli_pair):
            home = tmp_path / str(index) / "sai"
            seed(home, endpoint, monkeypatch, case.start)
            if case.start in {"rejected", "not_sent"}:
                with (
                    closing(sqlite3.connect(home / "trader/intents.sqlite3")) as db,
                    db,
                ):
                    db.execute(
                        "UPDATE intents SET status=? WHERE id=?", (case.start, KEY)
                    )
            requests.clear()
            replies.extend(map(response, case.replies))
            args = ["intent", "wait", KEY, "--timeout", case.timeout]
            if case.explicit_addr:
                args.extend(["--addr", ADDR])
            started = time.monotonic()
            result = invoke(cli, home, "--endpoint", endpoint, "--json", *args)
            elapsed = time.monotonic() - started
            assert result.returncode == case.exit, result
            assert result.stderr == b""
            assert OPAQUE.encode() not in result.stdout
            document = json.loads(result.stdout)
            assert document["intentKey"] == KEY
            assert document["addr"] == ADDR
            assert document["outcome"] == case.outcome
            assert document.get("code") == case.code
            assert ("error" in document) == (case.exit != 0)
            if case.exit:
                assert document["error"]["code"] == (case.code or "KEEPER_ERROR")
                assert document["error"]["retryable"] == (case.exit == 4)
            assert not replies
            assert len(requests) == len(case.requests)
            wire = [json.loads(request.body) for request in requests]
            for request, offset in zip(wire, case.requests, strict=True):
                assert request["query"].startswith("query")
                if offset is None:
                    assert request["variables"] == {
                        "user": ADDR,
                        "key": KEY,
                        "includeAction": True,
                    }
                else:
                    assert request["variables"] == {
                        "trader": ADDR,
                        "limit": 25,
                        "offset": offset,
                    }
            assert all("authorization" not in request.headers for request in requests)
            status = "unknown" if case.status == "pending" else case.status
            keeper_status = (
                case.status if case.status in {"pending", "terminal"} else None
            )
            ledger = ledger_state(home)
            assert ledger == [
                (KEY, status, keeper_status),
                ("foreign", "unknown", None),
            ]
            waits = [
                event
                for event in read_events(home, cli)
                if event["event"].startswith("intent.wait.")
            ]
            assert [event["event"] for event in waits] == [
                "intent.wait.start",
                "intent.wait.end",
            ]
            assert all(
                event["intentKey"] == KEY and event["addr"] == ADDR for event in waits
            )
            assert waits[0]["timeoutMs"] == round(float(case.timeout) * 1000)
            assert waits[1]["status"] == case.outcome
            assert waits[1].get("code") == case.code
            if case.name == "slow-final-never-commits":
                assert elapsed < 1.1
            if case.outcome == "filled":
                assert document["receipt"]["requestBlock"] == REQUEST_BLOCK
                assert document["receipt"]["requestTxHash"] == "a" * 64
                assert document["receipt"]["execution"]["block"] == FILL_BLOCK
                assert document["receipt"]["positionNow"]["openPrice"] == 87_000
                fill = document["receipt"]["fill"]
                if index:
                    assert fill.pop("sizeCollateralUsd") is None
                    assert fill.pop("escrowedCollateralUsd") == "4"
                    if case.name == "market-close":
                        assert fill.pop("realizedPnl") is None
                assert fill == {
                    "price": None,
                    "sizeCollateralBaseUnits": None,
                    "oraclePrice": "81000.50",
                    "escrowedCollateralBaseUnits": 4_000_000,
                }
            output = (
                RUST_RECEIPT_FIELDS.sub(b"", result.stdout) if index else result.stdout
            )
            if case.outcome == "unknown":
                output = document if index else with_timeout_warning(document)
            observed.append((output, wire, ledger))
    assert observed[0] == observed[1]


@pytest.mark.parametrize(
    ("args", "exit_code"),
    [
        (("intent", "--help"), 0),
        (("--json", "intent"), 2),
        (("intent", "wait", "--help"), 0),
        (("intent", "wait"), 2),
    ],
)
def test_wait_help(
    wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    args: tuple[str, ...],
    exit_code: int,
) -> None:
    outputs = []
    for index, cli in enumerate(wait_cli_pair):
        home = tmp_path / str(index)
        result = invoke(cli, home, *args)
        stdout_help = exit_code == 0 or args[0] == "--json"
        assert result.returncode == exit_code
        assert bool(result.stdout) == stdout_help
        assert bool(result.stderr) != stdout_help
        if args[0] == "--json":
            assert json.loads(result.stdout)["error"]["code"] == "INVALID_REQUEST"
        if exit_code == 0:
            assert not home.exists()
        outputs.append((result.stdout, result.stderr))
    if args[0] == "intent" and args[1:2] == ("wait",):
        # Rust documents the account-first form and the shared 60 s default (#271).
        outputs = [
            tuple(
                output.replace(WAIT_HELP_RUST, WAIT_HELP_PYTHON) if index else output
                for output in pair
            )
            for index, pair in enumerate(outputs)
        ]
    assert outputs[0] == outputs[1]


WAIT_HELP_PYTHON = b"""  Wait for an intent's final outcome and print its receipt.

Options:
  --addr TEXT      EVM or Nibiru address that submitted the intent.
  --timeout FLOAT  Maximum wait in seconds.  [default: 30.0]
"""
WAIT_HELP_RUST = b"""  Wait for an intent's final outcome and print its receipt.

  Pass the account and --key, as intent status does: ADDR --key KEY. With
  only KEY, the account comes from the local ledger or the active session.
  The older form KEY --addr ADDR still works.

Options:
  --key TEXT       Specific intent key.
  --addr TEXT      EVM or Nibiru address that submitted the intent.
  --timeout FLOAT  Maximum wait in seconds.  [default: 60.0]
"""


def test_rust_wait_documents_account_first_form_and_shared_default(
    cli_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    result = invoke(cli_pair[1], tmp_path / "sai", "intent", "wait", "--help")
    assert result.returncode == 0
    assert WAIT_HELP_RUST in result.stdout
    assert result.stdout.startswith(b"Usage: sai intent wait [OPTIONS] KEY")


@pytest.mark.parametrize(
    ("order_type", "rows", "order_id"),
    [
        ("limit", (indexed("limit_order_created"),), 7),
        ("stop", (indexed("stop_order_created"),), 7),
        ("limit", (), None),
        ("market", (indexed(),), "absent"),
    ],
    ids=["limit", "stop", "limit-not-indexed", "market-fill"],
)
def test_rust_placed_receipt_names_the_order_id(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order_type: str,
    rows: tuple[dict[str, Any], ...],
    order_id: int | str | None,
) -> None:
    """`sai trade cancel ID` takes the ID a placed order receipt names (#271)."""
    with replay() as (endpoint, replies, _):
        home = tmp_path / "sai"
        seed(home, endpoint, monkeypatch, "unknown")
        replies.extend(
            map(
                response,
                (
                    intent("filled", body={"orderType": order_type}),
                    history(*rows),
                    *([history()] if not rows else []),
                ),
            )
        )
        result = invoke(
            cli_pair[1], home, "--endpoint", endpoint, "--json", "intent", "wait", KEY
        )
        assert result.returncode == 0, result
        receipt = json.loads(result.stdout)["receipt"]
        if order_id == "absent":
            assert "orderId" not in receipt
        else:
            assert receipt["orderId"] == order_id
            assert list(receipt)[:4] == [
                "requestTxHash",
                "requestBlock",
                "action",
                "orderId",
            ]


def test_rust_wait_defaults_to_trade_wait_budget(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with replay() as (endpoint, replies, _):
        home = tmp_path / "sai"
        seed(home, endpoint, monkeypatch, "unknown")
        replies.extend(map(response, ("filled", history(indexed()))))
        result = invoke(
            cli_pair[1],
            home,
            "--endpoint",
            endpoint,
            "intent",
            "wait",
            ADDR,
            "--key",
            KEY,
        )
        assert result.returncode == 0, result
        start = next(
            event
            for event in read_events(home, cli_pair[1])
            if event["event"] == "intent.wait.start"
        )
        assert start["timeoutMs"] == 60_000


def test_wait_account_first_matches_python(
    wait_cli_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    reference, candidate = wait_cli_pair
    with replay() as (endpoint, replies, requests):
        results = []
        for index, (cli, args) in enumerate(
            ((reference, (KEY, "--addr", ADDR)), (candidate, (ADDR, "--key", KEY)))
        ):
            home = tmp_path / str(index) / "sai"
            home.mkdir(parents=True)
            requests.clear()
            replies.extend(map(response, ("filled", history(indexed()))))
            result = invoke(cli, home, "--endpoint", endpoint, "intent", "wait", *args)
            assert result.returncode == 0, result
            assert result.stderr == b""
            assert len(requests) == 2
            document = json.loads(result.stdout)
            if index:
                receipt = document["receipt"]
                assert receipt["fill"].pop("sizeCollateralUsd") is None
                assert receipt["fill"].pop("escrowedCollateralUsd") == "4"
                for field in ("collateralUsd", "openCollateralUsd"):
                    receipt["positionNow"].pop(field, None)
            results.append(document)
    assert results[0] == results[1]


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--help",), 0),
        ((), 2),
        ((KEY, "--timeout", "0"), 1),
        ((KEY, "--timeout", "-1"), 1),
        ((KEY, "--timeout", "nan"), 1),
        ((KEY, "--timeout", "inf"), 1),
        ((KEY, "--timeout", "none"), 2),
        ((KEY,), 1),
        ((KEY, "--addr", "invalid"), 5),
    ],
)
def test_wait_usage(
    wait_cli_pair: tuple[CLI, CLI], tmp_path: Path, args: tuple[str, ...], code: int
) -> None:
    observed = []
    for index, cli in enumerate(wait_cli_pair):
        home = tmp_path / str(index) / "sai"
        home.mkdir(parents=True)
        result = invoke(cli, home, "--json", "intent", "wait", *args)
        assert result.returncode == code, result
        assert result.stderr == b""
        if "--help" in args:
            # Rust documents the account-first form and the 60 s default (#271).
            stdout = result.stdout.replace(WAIT_HELP_RUST, WAIT_HELP_PYTHON)
            observed.append(stdout if index else result.stdout)
        else:
            observed.append(json.loads(result.stdout))
    if code == 5:
        # The pinned CLI treated its own /query route as private.
        reference = observed[0]
        old = f"sai --endpoint {PRIVATE_KEEPER_HINT} "
        for item in (reference, reference["error"]):
            assert item["hint"].startswith(old)
            item["hint"] = f"sai --endpoint {keeper.MAINNET_HTTP} " + item[
                "hint"
            ].removeprefix(old)
    assert observed[0] == observed[1]


def test_wait_bounds_account_lookup_when_ledger_is_locked(
    wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = []
    with replay() as (endpoint, _, requests):
        for index, cli in enumerate(wait_cli_pair):
            home = tmp_path / str(index) / "sai"
            seed(home, endpoint, monkeypatch, "unknown")
            with closing(sqlite3.connect(home / "trader/intents.sqlite3")) as db:
                db.execute("BEGIN EXCLUSIVE")
                started = time.monotonic()
                result = invoke(
                    cli,
                    home,
                    "--endpoint",
                    endpoint,
                    "--json",
                    "intent",
                    "wait",
                    KEY,
                    "--timeout",
                    "0.05",
                )
                assert time.monotonic() - started < 1.0
            assert result.returncode == 5, result
            assert result.stderr == b""
            assert not requests
            document = json.loads(result.stdout)
            assert document["outcome"] == "unknown"
            assert document["addr"] is None
            assert document["error"]["intentKey"] == KEY
            assert ledger_state(home) == [
                (KEY, "unknown", None),
                ("foreign", "unknown", None),
            ]
            observed.append(document)
    assert with_timeout_warning(observed[0]) == observed[1]


@pytest.mark.parametrize(
    ("endpoint", "public"),
    [
        (keeper.MAINNET_HTTP, True),
        (keeper.MAINNET_HTTP + "?key=private-fixture", False),
    ],
    ids=["public-keeper", "private-query"],
)
def test_wait_recovery_for_the_public_keeper_route(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    endpoint: str,
    public: bool,
) -> None:
    # These recovery hints compare against the current Python CLI.
    monkeypatch.delenv("SAI_PARITY_PYTHON", raising=False)
    clis: tuple[CLI, CLI] = request.getfixturevalue("cli_pair")
    for index, cli in enumerate(clis):
        home = tmp_path / str(index) / "sai"
        seed(home, endpoint, monkeypatch, "unknown")
        # The account lookup exhausts the deadline before any Keeper request.
        with closing(sqlite3.connect(home / "trader/intents.sqlite3")) as db:
            db.execute("BEGIN EXCLUSIVE")
            result = invoke(
                cli,
                home,
                "--endpoint",
                endpoint,
                "--json",
                "intent",
                "wait",
                KEY,
                "--timeout",
                "0.05",
            )
        assert result.returncode == 5 and not result.stderr, result
        document = json.loads(result.stdout)
        assert document["addr"] is None
        expected = endpoint if public else PRIVATE_KEEPER_HINT
        assert document["hint"] == f"sai --endpoint {expected} intent wait {KEY}"
        assert document["error"]["hint"] == document["hint"]


def test_wait_interrupts_locked_account_lookup(
    wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with replay() as (endpoint, _, requests):
        for index, cli in enumerate(wait_cli_pair):
            home = tmp_path / str(index) / "sai"
            seed(home, endpoint, monkeypatch, "unknown")
            with closing(sqlite3.connect(home / "trader/intents.sqlite3")) as db:
                db.execute("BEGIN EXCLUSIVE")
                with subprocess.Popen(
                    [
                        *cli,
                        "--endpoint",
                        endpoint,
                        "--json",
                        "intent",
                        "wait",
                        KEY,
                        "--timeout",
                        "1.5",
                    ],
                    env=cli_environment(home),
                    cwd=home.parent,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ) as process:
                    try:
                        end = time.monotonic() + 3
                        while (
                            not list((home / "logs").glob("*.jsonl"))
                            and time.monotonic() < end
                        ):
                            time.sleep(0.01)
                        assert list((home / "logs").glob("*.jsonl"))
                        time.sleep(0.15)
                        assert process.poll() is None
                        process.send_signal(signal.SIGINT)
                        stdout, stderr = process.communicate(timeout=3)
                    finally:
                        if process.poll() is None:
                            process.kill()
                    assert process.returncode == 1
            assert stderr == b"\n"
            assert json.loads(stdout) == {
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Aborted",
                    "retryable": False,
                },
            }
            assert not requests
            assert ledger_state(home)[0] == (KEY, "unknown", None)
