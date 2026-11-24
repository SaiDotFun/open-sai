"""C7a compares trading with Python ea09f0d417a53a71079bb8c5e426005fa29051a4."""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from compare import INTENT_ID, timestamp
from keeper_wire import same_variables
from scenarios import (
    ADDRESS,
    TRADE_REQUEST_HASH,
    Endpoints,
    child_env,
    loopback,
    seed_home,
)

CLI = tuple[str, ...]
OPEN = (
    "trade",
    "open",
    "--market",
    "3",
    "--side",
    "long",
    "--type",
    "market",
    "--amount",
    "20.12345600",
    "--leverage",
    "2",
    "--slippage",
    "1",
    "--price",
    "100",
)
CLOSE = ("trade", "close", "7")
CANCEL = ("trade", "cancel", "7")


@pytest.fixture
def trade_pair(cli_pair: tuple[CLI, CLI]) -> tuple[CLI, CLI]:
    # CI's C4 reference predates the accepted-open guard. Use the Python
    # version in the workspace for C7, or a binary installed from the pin.
    _, candidate = cli_pair
    reference = Path(
        os.environ.get("SAI_PARITY_C7_PYTHON", Path(sys.executable).with_name("sai"))
    )
    assert reference.is_file(), "Install the C7 Python reference with uv sync"
    return (str(reference),), candidate


def run(
    cli: CLI, root: Path, endpoints: Endpoints, args: CLI, *, python: bool
) -> tuple[int, dict[str, Any], str]:
    if python and args[:2] == CANCEL[:2]:
        args = ("trade", "close", args[2], "--cancel-order", *args[3:])
    result = subprocess.run(
        [*cli, "--json", *args],
        cwd=root,
        env={**child_env(root, endpoints), "SAI_LOG": "1", "SAI_UPDATE_AUTO": "0"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    assert result.stdout, f"Exit {result.returncode}: {result.stderr}"
    return result.returncode, json.loads(result.stdout), result.stderr


def rows(home: Path) -> list[tuple[Any, ...]]:
    path = home / "sai" / "trader" / "intents.sqlite3"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as ledger:
        return ledger.execute(
            "SELECT id, kind, status, entity, account, keeper_status, created_at"
            " FROM intents ORDER BY rowid"
        ).fetchall()


def test_numeric_keeper_price_rounds_to_even(
    trade_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    for index, cli in enumerate(trade_pair):
        with loopback("trade-accepted", market_price=68669.000244140625) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            code, result, _ = run(cli, home, endpoints, OPEN, python=index == 0)
            assert code == 0
            assert [request.label for request in endpoints.wire] == [
                "borrowings",
                "recordUserIntent",
            ]
            body = endpoints.wire[-1].variables["input"]["intent"]["body"]
            assert (
                body["intentPrice"] == result["referencePrice"] == "68669.00024414062"
            )


def test_python_reads_rust_trade_events_offline(
    trade_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    python, rust = trade_pair
    with loopback("trade-accepted") as endpoints:
        home = tmp_path / "shared"
        seed_home(home, endpoints, "session")
        code, result, _ = run(rust, home, endpoints, CLOSE, python=False)
        assert code == 0
        key = result["intentKey"]
        code, report, _ = run(
            python, home, endpoints, ("logs", "intent", key, "--offline"), python=True
        )
        assert code == 0
        assert report["intentKey"] == key
        assert report["txHash"] == TRADE_REQUEST_HASH.lower()
        assert len(endpoints.wire) == 1
        events = {event["event"]: event for event in report["events"]}
        trade_events = [
            events[name]
            for name in ("intent.reserved", "intent.submitted", "intent.status")
        ]
        assert len({event["endpointId"] for event in trade_events}) == 1
        endpoint_key = home / "sai" / "logs" / ".endpoint-key"
        assert endpoint_key.stat().st_size == 32
        assert endpoint_key.stat().st_mode & 0o777 == 0o600


def trade_events(home: Path, key: str, endpoint: str) -> list[tuple[str, str | None]]:
    events = [
        json.loads(line)
        for path in sorted((home / "sai" / "logs").glob("cli-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    [run_id] = [
        record["runId"] for record in events if record["event"] == "command.start"
    ]
    events = [record for record in events if record["runId"] == run_id]
    assert [record["seq"] for record in events] == list(range(1, len(events) + 1))
    relevant = []
    for record in events:
        name = record["event"]
        if name.startswith("intent."):
            assert record["intentKey"] == key
            assert record["endpoint"] == endpoint
            if name == "intent.submitted":
                assert record["txHash"] == TRADE_REQUEST_HASH.lower()
        elif name != "keeper.request":
            continue
        relevant.append((name, record.get("status")))
    return relevant


def intent_for(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        **({} if kind == "OPEN_TRADE" else {"target": {"userTradeId": "7"}}),
        "body": body,
    }


MARKET_PREVIEW = intent_for(
    "OPEN_TRADE",
    {
        "market": {"marketIndex": "MarketIndex(3)"},
        "side": "long",
        "orderType": "market",
        "amountUsd": "20.12345600",
        "leverage": "2",
        "maxSlippagePct": "1",
        "intentPrice": "100",
    },
)


@pytest.mark.parametrize(
    ("args", "intent", "market"),
    [
        (OPEN, MARKET_PREVIEW, True),
        (
            (*OPEN, "--type", "limit"),
            intent_for("OPEN_TRADE", {**MARKET_PREVIEW["body"], "orderType": "limit"}),
            False,
        ),
        (
            (*OPEN, "--type", "stop"),
            intent_for("OPEN_TRADE", {**MARKET_PREVIEW["body"], "orderType": "stop"}),
            False,
        ),
        (CLOSE, intent_for("CLOSE_POSITION", {}), False),
        ((*CLOSE, "--cancel-order"), intent_for("CANCEL_PENDING_TRADE", {}), False),
        (CANCEL, intent_for("CANCEL_PENDING_TRADE", {}), False),
    ],
)
def test_trade_dry_run(
    trade_pair: tuple[CLI, CLI],
    tmp_path: Path,
    args: CLI,
    intent: dict[str, Any],
    market: bool,
) -> None:
    preview = {"addr": None, "dryRun": True, "intent": intent}
    if market:
        preview["requiresMarketPriceRefresh"] = True
    for index, cli in enumerate(trade_pair):
        with loopback("trade-accepted") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "empty")
            assert run(
                cli, home, endpoints, (*args, "--dry-run"), python=index == 0
            ) == (0, preview, "")
            assert not endpoints.wire
            assert not (home / "sai" / "trader" / "intents.sqlite3").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            (*OPEN, "--amount", "1.0000001"),
            "Amount must have at most six decimal places",
        ),
        ((*OPEN, "--amount", "1e2"), "Amount must be a positive finite decimal"),
        (
            (*OPEN, "--slippage", "0.00000000000000001"),
            "Slippage must be between 0 and 100 with at most 16 decimal places",
        ),
        ((*OPEN, "--slippage", "0.0"), "Slippage must be a positive finite decimal"),
        (("trade", "close", "--", "-1"), "Trade id must not be negative"),
    ],
)
def test_validate_before_loading_session(
    trade_pair: tuple[CLI, CLI], tmp_path: Path, args: CLI, message: str
) -> None:
    for index, cli in enumerate(trade_pair):
        with loopback("trade-accepted") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "empty")
            assert run(cli, home, endpoints, args, python=index == 0) == (
                1,
                {
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": message,
                        "retryable": False,
                    }
                },
                "",
            )
            assert not endpoints.wire
            assert not rows(home)


@pytest.mark.parametrize("args", [OPEN, CLOSE, CANCEL])
def test_signed_out_submits_nothing(
    trade_pair: tuple[CLI, CLI], tmp_path: Path, args: CLI
) -> None:
    observations = []
    for index, cli in enumerate(trade_pair):
        with loopback("trade-accepted") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "empty")
            observations.append(run(cli, home, endpoints, args, python=index == 0))
            assert not endpoints.wire
            assert not rows(home)
    assert observations[0] == observations[1]
    assert observations[0][0] == 6
    assert observations[0][1]["error"]["code"] == "SIGNED_OUT"


@pytest.mark.parametrize(
    ("args", "intent"),
    [
        (OPEN, MARKET_PREVIEW),
        (CLOSE, intent_for("CLOSE_POSITION", {})),
        (CANCEL, intent_for("CANCEL_PENDING_TRADE", {})),
    ],
)
@pytest.mark.parametrize(
    ("verdict", "exit_code", "status", "code"),
    [
        ("accepted", 0, "accepted", None),
        ("rejected", 4, "rejected", "ADMISSION_REJECTED"),
        ("bad-ack", 5, "unknown", "OUTCOME_UNKNOWN"),
        ("disconnect", 5, "unknown", "OUTCOME_UNKNOWN"),
    ],
)
def test_trade_submission(
    trade_pair: tuple[CLI, CLI],
    tmp_path: Path,
    args: CLI,
    intent: dict[str, Any],
    verdict: str,
    exit_code: int,
    status: str,
    code: str | None,
) -> None:
    observations = []
    markets = args == OPEN
    for index, cli in enumerate(trade_pair):
        with loopback(f"trade-{verdict}") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            exit_status, output, stderr = run(
                cli, home, endpoints, args, python=index == 0
            )
            assert exit_status == exit_code
            assert not endpoints.errors
            assert [request.label for request in endpoints.wire] == (
                ["borrowings", "recordUserIntent"] if markets else ["recordUserIntent"]
            )
            variables = endpoints.wire[-1].variables
            envelope = variables["input"]
            key = envelope["intentKey"]
            stamp = envelope["intent"]["tsIntentInit"]
            assert INTENT_ID.fullmatch(key)
            assert timestamp(stamp)
            expected = intent
            if markets:
                price = envelope["intent"]["body"]["intentPrice"]
                assert isinstance(price, str) and price != "100"
                expected = {**intent, "body": {**intent["body"], "intentPrice": price}}
                if code is None:
                    assert output["referencePrice"] == price
            assert same_variables(
                variables,
                {
                    "input": {
                        "intentKey": key,
                        "intent": {
                            "intentSchemaVersion": "v0",
                            "tsIntentInit": stamp,
                            **expected,
                        },
                        "lifecycle": {
                            "sequence": 1,
                            "lifecycleStep": "jwt_pass",
                            "tsLifecycleStep": stamp,
                        },
                    }
                },
            )
            [(row_key, kind, local_status, entity, account, keeper_status, created)] = (
                rows(home)
            )
            assert (row_key, kind, local_status, entity, account, keeper_status) == (
                key,
                intent["kind"],
                status,
                "" if markets else "7",
                ADDRESS.lower(),
                None,
            )
            assert isinstance(created, float)
            if code is None:
                assert output["intentKey"] == key
                assert output["addr"] == ADDRESS
                assert output["status"] == "accepted"
            else:
                assert output["error"]["intentKey"] == key
                assert output["error"]["code"] == code
            expected_events = []
            if markets:
                expected_events.append(("keeper.request", "ok"))
            expected_events.extend(
                [
                    ("intent.reserved", "unknown"),
                    (
                        "keeper.request",
                        {"disconnect": "error", "rejected": "rejected"}.get(
                            verdict, "ok"
                        ),
                    ),
                ]
            )
            if status == "accepted":
                expected_events.append(("intent.submitted", None))
            if status != "unknown":
                expected_events.append(("intent.status", status))
            endpoint = urlsplit(endpoints.http)
            assert (
                trade_events(home, key, f"{endpoint.scheme}://{endpoint.netloc}")
                == expected_events
            )
            observations.append(
                (
                    json.dumps(output, sort_keys=True).replace(key, "<intent>"),
                    stderr,
                    expected,
                )
            )
    assert observations[0] == observations[1]


@pytest.mark.parametrize("verdict", ["accepted", "bad-ack"])
def test_open_retry_preserves_first_reservation(
    trade_pair: tuple[CLI, CLI], tmp_path: Path, verdict: str
) -> None:
    # Use a limit order so a blocked retry cannot be mistaken for a price read.
    args = (*OPEN, "--type", "limit")
    observations = []
    for index, cli in enumerate(trade_pair):
        with loopback(f"trade-{verdict}") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            first = run(cli, home, endpoints, args, python=index == 0)
            assert first[0] == (0 if verdict == "accepted" else 5)
            [(key, *_)] = rows(home)
            code, error, stderr = run(cli, home, endpoints, args, python=index == 0)
            assert code == 1
            assert error["error"]["code"] == "RECENT_OPEN_PENDING"
            assert error["error"]["intentKey"] == key
            assert len(rows(home)) == 1
            assert len(endpoints.wire) == 1
            observations.append(
                (json.dumps(error, sort_keys=True).replace(key, "<intent>"), stderr)
            )

            run(
                cli,
                home,
                endpoints,
                (*args, "--allow-concurrent-open"),
                python=index == 0,
            )
            assert len(endpoints.wire) == 2
            assert len(rows(home)) == 2
            assert not endpoints.errors
    assert observations[0] == observations[1]


def test_connect_failure_releases_reservation(
    trade_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    observations = []
    # Hold a loopback port without listening. Both clients get connection refused.
    with socket.socket() as offline:
        offline.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{offline.getsockname()[1]}"
        for index, cli in enumerate(trade_pair):
            home = tmp_path / str(index)
            endpoints = Endpoints(endpoint, endpoint.replace("http:", "ws:") + "/query")
            seed_home(home, endpoints, "session")
            for _ in range(2):
                code, output, stderr = run(
                    cli, home, endpoints, CLOSE, python=index == 0
                )
                assert code == 4
                assert output["error"]["code"] == "NOT_SENT"
                assert output["error"]["retryable"] is True
                key = output["error"]["intentKey"]
                assert INTENT_ID.fullmatch(key)
                observations.append(
                    (
                        json.dumps(output, sort_keys=True).replace(key, "<intent>"),
                        stderr,
                    )
                )
            assert [row[2] for row in rows(home)] == ["not_sent", "not_sent"]
    assert observations[1:] == observations[:1] * 3


def test_concurrent_close_sends_once(
    trade_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    for index, cli in enumerate(trade_pair):
        with loopback("concurrent-close") as endpoints, ThreadPoolExecutor() as tasks:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            first = tasks.submit(run, cli, home, endpoints, CLOSE, python=index == 0)
            try:
                assert endpoints.admission.arrived.wait(5)
                [(key, _, status, *_)] = rows(home)
                assert status == "unknown"
                code, blocked, stderr = run(
                    cli, home, endpoints, CLOSE, python=index == 0
                )
                assert (
                    code,
                    blocked["error"]["code"],
                    blocked["error"]["intentKey"],
                    stderr,
                ) == (
                    1,
                    "INVALID_REQUEST",
                    key,
                    "",
                )
                assert len(endpoints.wire) == 1
            finally:
                endpoints.admission.release.set()
            code, accepted, stderr = first.result(timeout=10)
            assert (code, accepted["intentKey"], accepted["status"], stderr) == (
                0,
                key,
                "accepted",
                "",
            )
            assert len(endpoints.wire) == 1
            assert [row[2] for row in rows(home)] == ["accepted"]
            assert not endpoints.errors


def test_interrupt_after_dispatch_reports_reserved_key(
    trade_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    for index, cli in enumerate(trade_pair):
        with loopback("concurrent-close") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            with subprocess.Popen(
                [*cli, "--json", *CLOSE],
                cwd=home,
                env={
                    **child_env(home, endpoints),
                    "SAI_LOG": "1",
                    "SAI_UPDATE_AUTO": "0",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
            ) as child:
                try:
                    assert endpoints.admission.arrived.wait(5)
                    [(key, _, status, *_)] = rows(home)
                    assert status == "unknown"
                    child.send_signal(signal.SIGINT)
                    stdout, stderr = child.communicate(timeout=5)
                    assert (child.returncode, stderr) == (5, "")
                    error = json.loads(stdout)["error"]
                    assert error["code"] == "OUTCOME_UNKNOWN"
                    assert error["intentKey"] == key
                    assert key in error["hint"]
                finally:
                    if child.poll() is None:
                        child.kill()
                    endpoints.admission.release.set()
            assert rows(home)[0][2] == "unknown"
            code, result, _ = run(cli, home, endpoints, CLOSE, python=index == 0)
            assert code == 1
            assert result["error"]["intentKey"] == key
            assert len(endpoints.wire) == 1
