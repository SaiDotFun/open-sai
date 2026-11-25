"""Trade parity with command-specific Python reference commits."""

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
from sai_cli.core.confirmation import preview_digest
from scenarios import (
    ADDRESS,
    OTHER_ADDRESS,
    PARTIAL_INTENT,
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
CANCEL_READS = ["oneClickTrading", "node_info", "get_trade_raw"]
CLOSE_READS = ["trade", *CANCEL_READS]


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


@pytest.fixture
def current_trade_pair(cli_pair: tuple[CLI, CLI]) -> tuple[CLI, CLI]:
    _, candidate = cli_pair
    reference = os.environ.get("SAI_PARITY_TRADE_MAIN_PYTHON")
    if reference is None or not Path(reference).is_file():
        pytest.fail(
            "Install the pinned Python main reference and set SAI_PARITY_TRADE_MAIN_PYTHON"
        )
    return (reference,), candidate


@pytest.mark.parametrize(
    ("args", "scenario", "confirmable"),
    [
        (OPEN, "quote-market", True),
        (OPEN[:-2], "quote-market", True),
        ((*OPEN, "--type", "limit"), "quote-trigger-fee", True),
        ((*OPEN, "--type", "stop"), "quote-trigger-fee", True),
        (OPEN, "quote-close-only", False),
        (CLOSE, "quote-queued", True),
        (CLOSE, "quote-no-publication", False),
        ((*CLOSE, "--cancel-order"), "pending-order", True),
        (("trade", "tpsl", "7", "--take-profit", "110"), "quote-close", True),
        (
            ("trade", "leverage", "7", "--leverage", "1.818181818181818182"),
            "quote-close",
            True,
        ),
        (
            ("trade", "margin", "7", "--direction", "add", "--amount", "1"),
            "position-default",
            True,
        ),
        (("trade", "partial-close", "7", "--percent", "50"), "partial-preview", True),
    ],
    ids=[
        "market-explicit",
        "market-keeper",
        "limit",
        "stop",
        "rejected-open",
        "close",
        "close-unknown-oracle",
        "pending-cancel",
        "tpsl",
        "leverage",
        "margin",
        "partial-close",
    ],
)
def test_cli_dry_run_digest_matches_python_main(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    args: CLI,
    scenario: str,
    confirmable: bool,
) -> None:
    reference = str(Path(sys.executable).with_name("sai"))
    confirmations = []
    for index, cli in enumerate(((reference,), cli_pair[1])):
        with loopback(scenario) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            code, preview, stderr = run(
                cli, home, endpoints, (*args, "--dry-run"), python=index == 0
            )
            assert (code, stderr) == (0, ""), preview
            digest = preview.get("previewDigest")
            if confirmable:
                assert digest == preview_digest(preview)
            else:
                assert digest is None
            confirmations.append((preview["positionRoute"], digest))
            assert not endpoints.errors
            assert not rows(home)
    assert confirmations[0] == confirmations[1]


def run(
    cli: CLI,
    root: Path,
    endpoints: Endpoints,
    args: CLI,
    *,
    python: bool,
    account: str | None = None,
    lcd_endpoint: str | None = None,
) -> tuple[int, dict[str, Any], str]:
    if python and args[:2] == CANCEL[:2]:
        args = ("trade", "close", args[2], "--cancel-order", *args[3:])
    env = {**child_env(root, endpoints), "SAI_LOG": "1", "SAI_UPDATE_AUTO": "0"}
    if lcd_endpoint is not None:
        env["SAI_LCD_ENDPOINT"] = lcd_endpoint
    result = subprocess.run(
        [*cli, "--json", *(("--as", account) if account else ()), *args],
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        # Keeper may wait 20 seconds for a response after dispatch.
        timeout=30,
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


def test_market_open_refuses_logout_during_price_refresh(
    trade_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    _, rust = trade_pair
    with loopback("trade-market-session") as endpoints:
        home = tmp_path / "rust"
        seed_home(home, endpoints, "session")
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(run, rust, home, endpoints, OPEN, python=False)
            try:
                assert endpoints.admission.arrived.wait(5)
                assert (
                    run(rust, home, endpoints, ("auth", "logout"), python=False)[0] == 0
                )
            finally:
                endpoints.admission.release.set()
            code, output, _ = result.result(timeout=10)
        assert (code, output.get("error", {}).get("code")) == (6, "SIGNED_OUT")
        assert [request.label for request in endpoints.wire] == ["borrowings"]
        assert not endpoints.errors
        assert not (home / "sai" / "trader" / "intents.sqlite3").exists()


def test_python_reads_rust_trade_events_offline(
    trade_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    python, rust = trade_pair
    with loopback("trade-accepted", close_target="verified") as endpoints:
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
        assert [request.label for request in endpoints.wire] == [
            *CLOSE_READS,
            "recordUserIntent",
        ]
        events = {event["event"]: event for event in report["events"]}
        trade_events = [
            events[name]
            for name in ("intent.reserved", "intent.submitted", "intent.status")
        ]
        assert len({event["endpointId"] for event in trade_events}) == 1
        endpoint_key = home / "sai" / "logs" / ".endpoint-key"
        assert endpoint_key.stat().st_size == 32
        assert endpoint_key.stat().st_mode & 0o777 == 0o600


def test_python_reads_rust_reconciliation_evidence_offline(
    trade_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    python, rust = trade_pair
    with loopback("trade-reconcile-offline", close_target="verified") as endpoints:
        home = tmp_path / "shared"
        seed_home(home, endpoints, "session")
        code, result, _ = run(rust, home, endpoints, CLOSE, python=False)
        assert code == 5
        key = result["error"]["intentKey"]
        assert rows(home)[0][2] == "unknown"

        status = ("intent", "status", ADDRESS, "--key", key)
        assert run(rust, home, endpoints, status, python=False)[0] == 0
        assert rows(home)[0][2] == "terminal"
        code, report, _ = run(
            python, home, endpoints, ("logs", "intent", key, "--offline"), python=True
        )
        assert code == 0
        assert report["intentKey"] == key
        assert report["txHash"] == TRADE_REQUEST_HASH.lower()
        assert "scopeWarning" not in report
        assert {entry["event"] for entry in report["events"]} >= {
            "intent.reserved",
            "intent.keeper",
            "intent.status",
        }
        assert [request.label for request in endpoints.wire] == [
            *CLOSE_READS,
            "recordUserIntent",
            "userIntents",
        ]
        assert not endpoints.errors
        events = [
            json.loads(line)
            for path in (home / "sai" / "logs").glob("cli-*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        scoped = [
            row for row in events if row["event"].startswith(("intent.", "keeper."))
        ]
        assert len(scoped) >= 5
        assert len({row["endpointId"] for row in scoped}) == 1


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


@pytest.mark.parametrize("args", [OPEN, CLOSE, CANCEL, (*CANCEL, "--dry-run")])
def test_signed_out_submits_nothing(
    trade_pair: tuple[CLI, CLI],
    tmp_path: Path,
    args: CLI,
    request: pytest.FixtureRequest,
) -> None:
    if args[:2] in (CANCEL[:2], CLOSE[:2]):
        trade_pair = request.getfixturevalue("current_trade_pair")
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
    ("target", "message", "wire"),
    [
        ("keeper-owner", "Keeper position belongs to a different account", ["trade"]),
        ("keeper-index", "Keeper returned a different position index", ["trade"]),
        (
            "contract-owner",
            "Contract position belongs to a different account",
            CLOSE_READS,
        ),
        ("contract-index", "Unable to verify contract position 7", CLOSE_READS),
    ],
)
def test_close_verifies_position_owner_before_reservation(
    current_trade_pair: tuple[CLI, CLI],
    tmp_path: Path,
    target: str,
    message: str,
    wire: list[str],
) -> None:
    for index, cli in enumerate(current_trade_pair):
        with loopback("trade-accepted", close_target=target) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            assert run(cli, home, endpoints, CLOSE, python=index == 0) == (
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
            assert [request.label for request in endpoints.wire] == wire
            assert not endpoints.errors
            assert not (home / "sai" / "trader" / "intents.sqlite3").exists()


@pytest.mark.parametrize("args", [CLOSE, CANCEL])
def test_position_trade_preserves_lcd_query(
    current_trade_pair: tuple[CLI, CLI],
    tmp_path: Path,
    args: CLI,
) -> None:
    cancel = args == CANCEL
    for index, cli in enumerate(current_trade_pair):
        with loopback(
            "trade-accepted",
            cancel_target="pending" if cancel else None,
            close_target=None if cancel else "verified",
            lcd_query=True,
        ) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            code, output, stderr = run(
                cli,
                home,
                endpoints,
                args,
                python=index == 0,
                lcd_endpoint=f"{endpoints.http}/?project=fixture",
            )
            assert (code, output.get("status"), stderr) == (0, "accepted", "")
            assert [request.label for request in endpoints.wire] == [
                *(CANCEL_READS if cancel else CLOSE_READS),
                "recordUserIntent",
            ]
            assert not endpoints.errors
            assert len(rows(home)) == 1


@pytest.mark.parametrize("args", [CANCEL, (*CLOSE, "--cancel-order")])
def test_cancel_dry_run_checks_pending_order(
    current_trade_pair: tuple[CLI, CLI], tmp_path: Path, args: CLI
) -> None:
    unavailable = {
        "status": "unavailable",
        "reason": "A pending order can fill before cancellation executes. Perp may close a filled position at market.",
    }
    for index, cli in enumerate(current_trade_pair):
        with loopback("trade-accepted", cancel_target="pending") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            code, preview, stderr = run(
                cli, home, endpoints, (*args, "--dry-run"), python=index == 0
            )
            if index == 1:
                assert preview.pop("previewDigest") == preview_digest(preview)
                assert preview.pop("positionRoute")["chainId"]
            assert (code, preview, stderr) == (
                0,
                {
                    "addr": ADDRESS,
                    "dryRun": True,
                    "intent": intent_for("CANCEL_PENDING_TRADE", {}),
                    "estimates": dict.fromkeys(
                        ("fees", "liquidationPrice", "worstCaseFill"), unavailable
                    ),
                },
                "",
            )
            assert [request.label for request in endpoints.wire] == CANCEL_READS
            assert not endpoints.errors
            assert not rows(home)


@pytest.mark.parametrize("dry_run", [False, True], ids=["submit", "preview"])
@pytest.mark.parametrize(
    ("target", "message"),
    [
        (
            "filled",
            "Target is not an open pending order. Preview a market close for a filled position.",
        ),
        ("missing", "Trade 7 was not found for this account."),
        ("other-account", "Trade 7 was not found for this account."),
    ],
)
def test_cancel_refuses_non_pending_target(
    current_trade_pair: tuple[CLI, CLI],
    tmp_path: Path,
    target: str,
    message: str,
    dry_run: bool,
) -> None:
    args = (*CANCEL, "--dry-run") if dry_run else CANCEL
    for index, cli in enumerate(current_trade_pair):
        with loopback("trade-accepted", cancel_target=target) as endpoints:
            home = tmp_path / str(index)
            other = target == "other-account"
            seed_home(home, endpoints, "accounts" if other else "session")
            assert run(
                cli,
                home,
                endpoints,
                args,
                python=index == 0,
                account=OTHER_ADDRESS if other else None,
            ) == (
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
            assert [request.label for request in endpoints.wire] == CANCEL_READS
            assert endpoints.wire[-1].variables["get_trade_raw"]["trader"] == (
                OTHER_ADDRESS if other else ADDRESS
            )
            assert not endpoints.errors
            assert not (home / "sai" / "trader" / "intents.sqlite3").exists()


@pytest.mark.parametrize(
    ("target", "message", "wire"),
    [
        (
            "foreign-owner",
            "Contract pending order belongs to a different account",
            CANCEL_READS,
        ),
        (
            "filled-foreign-owner",
            "Target is not an open pending order. Preview a market close for a filled position.",
            CANCEL_READS,
        ),
        (
            "unknown-chain",
            "Keeper trading route and LCD network do not match",
            CANCEL_READS[:2],
        ),
        (
            "empty-chain",
            "Keeper trading route and LCD network do not match",
            CANCEL_READS[:2],
        ),
        (
            "non-boolean-route",
            "Keeper trading route and LCD network do not match",
            CANCEL_READS[:2],
        ),
        (
            "missing-contract",
            "Keeper trading route has no contract address",
            CANCEL_READS[:2],
        ),
    ],
)
def test_cancel_validates_route_and_target_in_python_order(
    current_trade_pair: tuple[CLI, CLI],
    tmp_path: Path,
    target: str,
    message: str,
    wire: list[str],
) -> None:
    for index, cli in enumerate(current_trade_pair):
        with loopback("trade-accepted", cancel_target=target) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            assert run(cli, home, endpoints, CANCEL, python=index == 0) == (
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
            assert [request.label for request in endpoints.wire] == wire
            assert not endpoints.errors
            assert not (home / "sai" / "trader" / "intents.sqlite3").exists()


def test_cancel_rechecks_the_pinned_account_after_reading(
    current_trade_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    for index, cli in enumerate(current_trade_pair):
        with loopback("trade-cancel-session", cancel_target="pending") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    run, cli, home, endpoints, CANCEL, python=index == 0
                )
                try:
                    assert endpoints.admission.arrived.wait(5)
                    assert (
                        run(
                            cli, home, endpoints, ("auth", "logout"), python=index == 0
                        )[0]
                        == 0
                    )
                finally:
                    endpoints.admission.release.set()
                code, output, stderr = result.result(timeout=10)
            assert (code, output["error"]["code"], stderr) == (6, "SIGNED_OUT", "")
            assert [request.label for request in endpoints.wire] == CANCEL_READS
            assert not endpoints.errors
            assert not (home / "sai" / "trader" / "intents.sqlite3").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ("--leverage", "340282366920938463463.374607431768211456"),
            "Leverage must be a positive finite decimal",
        ),
        (
            ("--amount", "100000000000000000000.000000000000000001"),
            "Amount must have at most six decimal places",
        ),
    ],
)
def test_exact_decimal_checks_intentionally_differ_from_python(
    trade_pair: tuple[CLI, CLI], tmp_path: Path, args: CLI, message: str
) -> None:
    for index, cli in enumerate(trade_pair):
        with loopback("trade-accepted") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "empty")
            exact_max = ("--leverage", "340282366920938463463.374607431768211455")
            assert (
                run(
                    cli,
                    home,
                    endpoints,
                    (*OPEN, *exact_max, "--dry-run"),
                    python=index == 0,
                )[0]
                == 0
            )
            # The valid Rust dry run reads estimates. The rejected request
            # below must still stop before any Keeper or LCD reads.
            reads = endpoints.wire.copy()
            if index == 0:
                assert not reads
            else:
                assert reads and reads[0].label == "borrowings"
            code, result, stderr = run(
                cli, home, endpoints, (*OPEN, *args, "--dry-run"), python=index == 0
            )
            assert stderr == ""
            if index == 0:
                assert code == 0 and result["dryRun"]
            else:
                assert code == 1 and result["error"]["message"] == message
            assert endpoints.wire == reads
            assert not rows(home)


def test_non_ascii_market_id_is_an_accepted_parity_divergence(
    trade_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    for index, cli in enumerate(trade_pair):
        with loopback("trade-accepted") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "empty")
            code, result, stderr = run(
                cli,
                home,
                endpoints,
                (*OPEN, "--market", "٣", "--dry-run"),
                python=index == 0,
            )
            assert stderr == ""
            if index == 0:
                assert code == 0
                assert (
                    result["intent"]["body"]["market"]["marketIndex"]
                    == "MarketIndex(3)"
                )
            else:
                assert code == 2
                assert (
                    result["error"]["message"]
                    == "Invalid value for '--market': expected an integer."
                )
            assert not endpoints.wire
            assert not rows(home)


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
    request: pytest.FixtureRequest,
) -> None:
    observations = []
    markets = args == OPEN
    cancel = args == CANCEL
    close = args == CLOSE
    if close or cancel:
        trade_pair = request.getfixturevalue("current_trade_pair")
    for index, cli in enumerate(trade_pair):
        with loopback(
            f"trade-{verdict}",
            cancel_target="pending" if cancel else None,
            close_target="verified" if close else None,
        ) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            exit_status, output, stderr = run(
                cli, home, endpoints, args, python=index == 0
            )
            assert exit_status == exit_code
            assert not endpoints.errors
            assert [request.label for request in endpoints.wire] == (
                (["borrowings"] if markets else CANCEL_READS if cancel else CLOSE_READS)
                + ["recordUserIntent"]
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
            keeper_reads = 2 if close else int(markets or cancel)
            expected_events = [("keeper.request", "ok")] * keeper_reads
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


@pytest.mark.parametrize(
    "args",
    [
        ("trade", "margin", "7", "--direction", "remove", "--amount", "1"),
        ("trade", "partial-close", "7", "--percent", "50"),
    ],
    ids=["margin-remove", "partial-close"],
)
def test_stale_index_is_retryable_before_submission(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    args: CLI,
) -> None:
    monkeypatch.delenv("SAI_PARITY_PYTHON", raising=False)
    clis: tuple[CLI, CLI] = request.getfixturevalue("cli_pair")
    for index, cli in enumerate(clis):
        with loopback("position-index-lag") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            code, result, stderr = run(cli, home, endpoints, args, python=index == 0)
            assert (code, stderr) == (1, "")
            assert result["error"]["code"] == "INVALID_REQUEST"
            assert result["error"]["retryable"] is True, (cli, result["error"])
            assert not rows(home)
            assert "recordUserIntent" not in [entry.label for entry in endpoints.wire]
            assert not endpoints.errors


def test_open_retry_is_in_the_blocking_intent_trail(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The released Python reference predates scoped open-refusal events.
    monkeypatch.delenv("SAI_PARITY_PYTHON", raising=False)
    clis: tuple[CLI, CLI] = request.getfixturevalue("cli_pair")
    args = (*OPEN, "--type", "limit")
    for index, cli in enumerate(clis):
        with loopback("trade-accepted") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            code, first, stderr = run(cli, home, endpoints, args, python=index == 0)
            assert (code, stderr) == (0, "")
            key = first["intentKey"]
            code, blocked, stderr = run(cli, home, endpoints, args, python=index == 0)
            assert (code, stderr) == (1, "")
            assert blocked["error"]["code"] == "RECENT_OPEN_PENDING"
            code, trail, stderr = run(
                cli,
                home,
                endpoints,
                ("logs", "intent", key, "--offline"),
                python=index == 0,
            )
            assert (code, stderr) == (0, "")
            [refusal] = [
                event
                for event in trail["events"]
                if event["event"] == "intent.open_blocked"
            ]
            assert refusal["intentKey"] == key and refusal["addr"] == ADDRESS
            assert refusal["code"] == blocked["error"]["code"]
            assert (
                sum(entry.label == "recordUserIntent" for entry in endpoints.wire) == 1
            )
            assert len(rows(home)) == 1
            assert not endpoints.errors


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
                    cli, home, endpoints, (*OPEN, "--type", "limit"), python=index == 0
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


@pytest.mark.parametrize(
    "scenario", ["partial-price-changed", "partial-price-recovers"]
)
def test_confirmed_partial_close_binds_collateral_price_availability(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, scenario: str
) -> None:
    # Both implementations at this checkout use the reduction digest.
    _, candidate = cli_pair
    reference = (str(Path(sys.executable).with_name("sai")),)
    args = ("trade", "partial-close", "7", "--percent", "50")
    digests = []
    for index, cli in enumerate((reference, candidate)):
        with loopback(scenario) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            code, preview, _ = run(
                cli, home, endpoints, (*args, "--dry-run"), python=index == 0
            )
            assert code == 0, preview
            digest = preview["previewDigest"]
            code, fresh, _ = run(
                cli, home, endpoints, (*args, "--dry-run"), python=index == 0
            )
            assert code == 0, fresh
            assert fresh["intent"] == preview["intent"] == PARTIAL_INTENT
            for field in ("collateralPriceUsd", "closedNotionalUsd"):
                assert fresh["quote"][field] != preview["quote"][field]
            assert rows(home) == []

            if scenario == "partial-price-recovers":
                assert preview["quote"]["closedNotionalUsd"] is None
                assert preview["quote"]["closedNotionalIsEstimate"] is True
                assert fresh["quote"]["closedNotionalIsEstimate"] is False
                code, refused, _ = run(
                    cli,
                    home,
                    endpoints,
                    (*args, "--preview-digest", digest),
                    python=index == 0,
                )
                assert code == 4, refused
                assert refused["error"]["code"] == "PREVIEW_MISMATCH"
                assert rows(home) == []
                assert not any(
                    entry.label == "recordUserIntent" for entry in endpoints.wire
                )
                assert fresh["previewDigest"] != digest
            else:
                assert fresh["previewDigest"] == digest

            code, submitted, _ = run(
                cli,
                home,
                endpoints,
                (*args, "--preview-digest", fresh["previewDigest"]),
                python=index == 0,
            )
            assert code == 0, submitted
            assert submitted["status"] == "accepted"
            assert [record[2] for record in rows(home)] == ["accepted"]
            assert [entry.label for entry in endpoints.wire].count(
                "recordUserIntent"
            ) == 1
            assert not endpoints.errors
            digests.append((digest, fresh["previewDigest"]))
    assert digests[0] == digests[1]


def test_concurrent_close_sends_once(
    current_trade_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    for index, cli in enumerate(current_trade_pair):
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
                assert [entry.label for entry in endpoints.wire] == [
                    *CLOSE_READS,
                    "recordUserIntent",
                    *CLOSE_READS,
                ]
            finally:
                endpoints.admission.release.set()
            code, accepted, stderr = first.result(timeout=10)
            assert (code, accepted["intentKey"], accepted["status"], stderr) == (
                0,
                key,
                "accepted",
                "",
            )
            assert [entry.label for entry in endpoints.wire] == [
                *CLOSE_READS,
                "recordUserIntent",
                *CLOSE_READS,
            ]
            assert [row[2] for row in rows(home)] == ["accepted"]
            assert not endpoints.errors


@pytest.mark.parametrize(
    ("scenario", "args", "kind"),
    [
        ("concurrent-close", CLOSE, "CLOSE_POSITION"),
        (
            "margin-interrupted",
            ("trade", "margin", "7", "--direction", "add", "--amount", "1"),
            "UPDATE_TRADE",
        ),
        (
            "partial-interrupted",
            ("trade", "partial-close", "7", "--percent", "50"),
            "DECREASE_POSITION_SIZE",
        ),
    ],
)
def test_interrupt_after_dispatch_reports_reserved_key(
    current_trade_pair: tuple[CLI, CLI],
    tmp_path: Path,
    scenario: str,
    args: CLI,
    kind: str,
) -> None:
    for index, cli in enumerate(current_trade_pair):
        with loopback(scenario) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            with subprocess.Popen(
                [*cli, "--json", *args],
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
                    [(key, intent_kind, status, entity, *_)] = rows(home)
                    assert (intent_kind, status, entity) == (kind, "unknown", "7")
                    assert key == endpoints.admission.key
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
            code, result, _ = run(cli, home, endpoints, args, python=index == 0)
            assert code == 1
            assert result["error"]["intentKey"] == key
            assert [request.label for request in endpoints.wire].count(
                "recordUserIntent"
            ) == 1
            assert not endpoints.errors
            if scenario == "concurrent-close":
                assert [entry.label for entry in endpoints.wire] == [
                    *CLOSE_READS,
                    "recordUserIntent",
                    *CLOSE_READS,
                ]


@pytest.mark.parametrize("first", [0, 1], ids=["python-sends", "rust-sends"])
def test_mixed_clients_reconcile_a_post_dispatch_timeout(
    trade_pair: tuple[CLI, CLI], tmp_path: Path, first: int
) -> None:
    sender, peer = trade_pair[first], trade_pair[1 - first]
    args = (*OPEN, "--type", "limit")
    with loopback("trade-timeout") as endpoints:
        home = tmp_path / "shared"
        seed_home(home, endpoints, "session")
        try:
            code, result, stderr = run(sender, home, endpoints, args, python=first == 0)
        finally:
            endpoints.admission.release.set()
        assert (code, stderr) == (5, "")
        key = result["error"]["intentKey"]
        assert result["error"]["code"] == "OUTCOME_UNKNOWN"
        assert key == endpoints.admission.key
        assert [row[2] for row in rows(home)] == ["unknown"]

        code, result, _ = run(peer, home, endpoints, args, python=first == 1)
        assert code == 1
        assert result["error"]["code"] == "RECENT_OPEN_PENDING"
        assert result["error"]["intentKey"] == key
        assert [request.label for request in endpoints.wire] == ["recordUserIntent"]

        code, _, _ = run(
            peer,
            home,
            endpoints,
            ("intent", "status", ADDRESS, "--key", key),
            python=first == 1,
        )
        assert code == 0
        assert [row[2] for row in rows(home)] == ["terminal"]
        assert run(sender, home, endpoints, args, python=first == 0)[0] == 0
        assert [request.label for request in endpoints.wire] == [
            "recordUserIntent",
            "userIntents",
            "recordUserIntent",
        ]
        assert [row[2] for row in rows(home)] == ["terminal", "accepted"]
        assert not endpoints.errors
