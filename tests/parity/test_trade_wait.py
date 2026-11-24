"""Compare trade --wait through CLI subprocesses and a loopback Keeper."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sai_cli.core import log, storage
from test_intent_status import (
    ADDR,
    CLI,
    Reply,
    Request,
    cli_environment,
    invoke,
    replay,
    response,
)
from test_intent_wait import (
    FILL_BLOCK,
    REQUEST_BLOCK,
    history,
    indexed,
    intent,
    ledger_state,
)

OPEN = (
    "open",
    "--market",
    "1",
    "--side",
    "long",
    "--type",
    "market",
    "--amount",
    "4",
    "--leverage",
    "2",
    "--slippage",
    "1",
)
CLOSE = ("close", "17")
CANCEL = (*CLOSE, "--cancel-order")
MARKETS = {
    "data": {
        "perp": {
            "borrowings": [
                {
                    "marketId": 1,
                    "collateralToken": {"symbol": "USDC"},
                    "isOpen": True,
                    "price": "81000.50",
                }
            ]
        }
    }
}


@dataclass(frozen=True)
class Case:
    name: str
    args: tuple[str, ...]
    kind: str
    change: str
    outcome: str = "filled"
    admission: str = "accepted"
    alternate_rust_args: tuple[str, ...] = ()


CASES = (
    Case("open", OPEN, "OPEN_TRADE", "position_opened"),
    Case("close", CLOSE, "CLOSE_POSITION", "position_closed_user"),
    Case(
        "close-cancel", CANCEL, "CANCEL_PENDING_TRADE", "order_closed_user", "succeeded"
    ),
    Case(
        "cancel-alias",
        CANCEL,
        "CANCEL_PENDING_TRADE",
        "order_closed_user",
        "succeeded",
        alternate_rust_args=("cancel", "17"),
    ),
    Case(
        "lost-ack-reconciles",
        CLOSE,
        "CLOSE_POSITION",
        "position_closed_user",
        admission="lost",
    ),
    Case(
        "rejected",
        CLOSE,
        "CLOSE_POSITION",
        "position_closed_user",
        "rejected",
        admission="rejected",
    ),
    Case(
        "failed-after-admission",
        CLOSE,
        "CLOSE_POSITION",
        "position_closed_user",
        "failed",
    ),
)


def connect(home: Path, endpoint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAI_HOME", str(home))
    with log.silenced():
        storage.save_session(
            {
                "evmAddress": ADDR,
                "endpoint": endpoint,
                "token": "fixture-token",
                "expiresAt": "2099-01-01T00:00:00Z",
            }
        )


def submitted_key(request: Request, kind: str) -> str:
    wire = json.loads(request.body)
    assert wire["query"].startswith("mutation")
    assert request.headers["authorization"] == "Bearer fixture-token"
    submission = wire["variables"]["input"]
    assert submission["intent"]["kind"] == kind
    return submission["intentKey"]


def accepted(key: str, kind: str) -> Reply:
    return response(
        {
            "data": {
                "recordUserIntent": {
                    "intentKey": key,
                    "kind": kind,
                    "execActor": "server",
                    "lifecycleStep": "jwt_pass",
                    "evmAddress": ADDR,
                }
            }
        }
    )


def normalized(document: Any, key: str) -> Any:
    if isinstance(document, dict):
        return {name: normalized(value, key) for name, value in document.items()}
    if isinstance(document, (list, tuple)):
        return [normalized(value, key) for value in document]
    return document.replace(key, "<intent>") if isinstance(document, str) else document


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_trade_wait(
    wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: Case,
) -> None:
    documents = []
    keys: list[str] = []
    with replay() as (endpoint, replies, requests):
        for index, cli in enumerate(wait_cli_pair):
            home = tmp_path / str(index) / "sai"
            connect(home, endpoint, monkeypatch)
            requests.clear()
            keys.clear()

            def admit(request: Request) -> Reply:
                key = submitted_key(request, case.kind)
                keys.append(key)
                if case.args == OPEN:
                    assert (
                        json.loads(request.body)["variables"]["input"]["intent"][
                            "body"
                        ]["intentPrice"]
                        == "81000.50"
                    )
                if case.admission == "rejected":
                    return response(
                        {"errors": [{"extensions": {"code": "INTENT_NOT_ACCEPTED"}}]}
                    )
                if case.admission == "lost":
                    return Reply(disconnect=True)
                return accepted(key, case.kind)

            def poll(request: Request, name: str) -> Reply:
                assert "authorization" not in request.headers
                assert json.loads(request.body)["variables"] == {
                    "user": ADDR,
                    "key": keys[0],
                    "includeAction": True,
                }
                body = {"orderType": "market"} if case.kind == "OPEN_TRADE" else {}
                return response(
                    intent(name, intentKey=keys[0], kind=case.kind, body=body)
                )

            def indexed_outcome(request: Request) -> Reply:
                assert "authorization" not in request.headers
                assert json.loads(request.body)["variables"] == {
                    "trader": ADDR,
                    "limit": 25,
                    "offset": 0,
                }
                return response(history(indexed(case.change, intentKey=keys[0])))

            if case.args == OPEN:
                replies.append(response(MARKETS))
            replies.append(admit)
            if case.admission != "rejected":
                replies.append(lambda request: poll(request, "pending_exec"))
                if case.outcome == "failed":
                    replies.append(
                        lambda request: poll(request, "market_order_fill_failed")
                    )
                else:
                    replies.extend(
                        (lambda request: poll(request, "filled"), indexed_outcome)
                    )
            args = (
                case.alternate_rust_args
                if index and case.alternate_rust_args
                else case.args
            )
            result = invoke(
                cli, home, "--endpoint", endpoint, "--json", "trade", *args, "--wait"
            )
            expected_exit = {"rejected": 4, "failed": 1}.get(case.outcome, 0)
            assert result.returncode == expected_exit, result
            assert result.stderr == b""
            document = json.loads(result.stdout)
            key = document["intentKey"]
            assert keys == [key]
            assert not replies
            wire = [json.loads(request.body) for request in requests]
            assert sum(item["query"].startswith("mutation") for item in wire) == 1
            assert document["outcome"] == document["status"] == case.outcome
            assert document["addr"] == ADDR
            if case.args == OPEN:
                assert document["referencePrice"] == "81000.50"
            if case.outcome != "rejected":
                receipt = document["receipt"]
                assert receipt["requestBlock"] == REQUEST_BLOCK
                assert receipt["requestTxHash"] == "a" * 64
                if case.outcome != "failed":
                    assert receipt["execution"]["block"] == FILL_BLOCK
            ledger = ledger_state(home)
            statuses = (
                ("rejected", None)
                if case.outcome == "rejected"
                else ("terminal", "terminal")
            )
            assert ledger == [(key, *statuses)]
            for item in wire:
                if item["query"].startswith("mutation"):
                    submission = item["variables"]["input"]
                    stamp = submission["intent"].pop("tsIntentInit")
                    assert submission["lifecycle"].pop("tsLifecycleStep") == stamp
            documents.append(normalized([document, wire, ledger], key))
    assert documents[0] == documents[1]


def test_trade_wait_dry_run(wait_cli_pair: tuple[CLI, CLI], tmp_path: Path) -> None:
    documents = []
    with replay() as (endpoint, _, requests):
        for index, cli in enumerate(wait_cli_pair):
            home = tmp_path / str(index) / "sai"
            home.mkdir(parents=True)
            result = invoke(
                cli,
                home,
                "--endpoint",
                endpoint,
                "--json",
                "trade",
                *CLOSE,
                "--wait",
                "--dry-run",
            )
            assert result.returncode == 0, result
            assert result.stderr == b""
            assert not requests
            assert not (home / "trader/intents.sqlite3").exists()
            document = json.loads(result.stdout)
            assert document["dryRun"] is True
            assert "receipt" not in document
            documents.append(document)
    assert documents[0] == documents[1]


@pytest.mark.parametrize("phase", ["submit", "poll"])
def test_trade_wait_interrupt_retains_reservation(
    wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    documents = []
    with replay() as (endpoint, replies, requests):
        for index, cli in enumerate(wait_cli_pair):
            home = tmp_path / str(index) / "sai"
            connect(home, endpoint, monkeypatch)
            requests.clear()
            replies.append(
                Reply(body_delay=4.0) if phase == "submit" else Reply(disconnect=True)
            )
            if phase == "poll":
                replies.append(Reply(body_delay=4.0))
            with subprocess.Popen(
                [*cli, "--endpoint", endpoint, "--json", "trade", *CLOSE, "--wait"],
                env=cli_environment(home),
                cwd=home.parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as process:
                try:
                    end = time.monotonic() + 2
                    expected_requests = 1 if phase == "submit" else 2
                    while len(requests) < expected_requests and time.monotonic() < end:
                        time.sleep(0.01)
                    assert len(requests) == expected_requests
                    process.send_signal(signal.SIGINT)
                    stdout, stderr = process.communicate(timeout=2)
                finally:
                    if process.poll() is None:
                        process.kill()
                assert process.returncode == 5, (stdout, stderr)
            key = submitted_key(requests[0], "CLOSE_POSITION")
            assert stderr == b""
            document = json.loads(stdout)
            assert document["intentKey"] == key
            assert document["outcome"] == document["localStatus"] == "unknown"
            assert document["interrupted"] is True
            assert document["receipt"] is None
            assert ledger_state(home) == [(key, "unknown", None)]
            if phase == "poll":
                assert json.loads(requests[1].body)["variables"]["key"] == key
            again = invoke(
                cli, home, "--endpoint", endpoint, "--json", "trade", *CLOSE, "--wait"
            )
            assert again.returncode == 1, again
            assert json.loads(again.stdout)["error"]["intentKey"] == key
            assert len(requests) == expected_requests
            documents.append(normalized(document, key))
    assert documents[0] == documents[1]


def test_trade_wait_recovers_when_stdout_closes(
    wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = []
    kind = "CLOSE_POSITION"
    with replay() as (endpoint, replies, requests):
        for index, cli in enumerate(wait_cli_pair):
            home = tmp_path / str(index) / "sai"
            connect(home, endpoint, monkeypatch)
            requests.clear()
            replies.extend(
                (
                    lambda request: accepted(submitted_key(request, kind), kind),
                    lambda _: response(
                        intent(
                            "filled",
                            kind=kind,
                            intentKey=submitted_key(requests[0], kind),
                        )
                    ),
                    lambda _: response(
                        history(
                            indexed(
                                "position_closed_user",
                                intentKey=submitted_key(requests[0], kind),
                            )
                        )
                    ),
                )
            )
            read, write = os.pipe()
            os.close(read)
            try:
                result = subprocess.run(
                    [*cli, "--endpoint", endpoint, "--json", "trade", *CLOSE, "--wait"],
                    env=cli_environment(home),
                    cwd=home.parent,
                    stdout=write,
                    stderr=subprocess.PIPE,
                    timeout=4,
                    check=False,
                )
            finally:
                os.close(write)
            stderr = result.stderr
            if index == 0 and result.returncode == 120:
                # CPython can override the exit code when its final stdout flush fails.
                stderr, flush_error = stderr.split(b"\n", 1)
                assert b"BrokenPipeError" in flush_error
            else:
                assert result.returncode == 1, result
            assert len(requests) == 3
            key = submitted_key(requests[0], kind)
            document = json.loads(stderr)
            assert document["intentKey"] == key
            assert document["localStatus"] == document["outcome"] == "terminal"
            assert document["receipt"] is None
            assert document["interrupted"] is False
            assert ledger_state(home) == [(key, "terminal", "terminal")]
            documents.append(normalized(document, key))
    assert documents[0] == documents[1]
