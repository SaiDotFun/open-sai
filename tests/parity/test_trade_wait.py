"""Compare trade --wait through CLI subprocesses and a loopback Keeper."""

from __future__ import annotations

import base64
import json
import os
import select
import signal
import subprocess
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from sai_cli.core import keeper, log, storage
from test_intent_status import (
    ADDR,
    CLI,
    ReplayReply,
    Reply,
    Request,
    cli_environment,
    invoke,
    read_events,
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
TPSL = ("tpsl", "17", "--take-profit", "110.0100", "--clear-stop-loss")
LEVERAGE = ("leverage", "17", "--leverage", "5.00")
UPDATES = {
    TPSL: {
        "takeProfit": {"operation": "set", "value": "110.0100"},
        "stopLoss": {"operation": "clear"},
    },
    LEVERAGE: {
        "takeProfit": {"operation": "keep"},
        "stopLoss": {"operation": "keep"},
        "requestedLeverage": "5.00",
    },
}
CONTRACT = "nibi1contract"
NODE_INFO = "/cosmos/base/tendermint/v1beta1/node_info"
SMART_QUERY = f"/cosmwasm/wasm/v1/contract/{CONTRACT}/smart/"
ROUTE = {"enabled": True, "chainId": "cataclysm-1", "contractAddress": CONTRACT}
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
    Case("tpsl", TPSL, "UPDATE_TRADE", "tpsl_updated", "succeeded"),
    Case("leverage", LEVERAGE, "UPDATE_TRADE", "leverage_updated", "succeeded"),
    Case("tpsl-lost-ack", TPSL, "UPDATE_TRADE", "tpsl_updated", "succeeded", "lost"),
    Case(
        "leverage-lost-ack",
        LEVERAGE,
        "UPDATE_TRADE",
        "leverage_updated",
        "succeeded",
        "lost",
    ),
    Case("tpsl-rejected", TPSL, "UPDATE_TRADE", "tpsl_updated", "rejected", "rejected"),
    Case("leverage-failed", LEVERAGE, "UPDATE_TRADE", "leverage_updated", "failed"),
)


def trade_wait_pair(
    request: pytest.FixtureRequest, args: tuple[str, ...]
) -> tuple[CLI, CLI]:
    fixture = "position_wait_cli_pair" if args in UPDATES else "wait_cli_pair"
    return request.getfixturevalue(fixture)


@pytest.fixture
def position_wait_cli_pair(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[CLI, CLI]:
    if "SAI_PARITY_BIN" not in os.environ:
        pytest.skip("set SAI_PARITY_BIN to the Rust sai executable")
    reference = os.environ.get("SAI_PARITY_POSITION_WAIT_PYTHON")
    if not reference:
        pytest.fail(
            "set SAI_PARITY_POSITION_WAIT_PYTHON to the pinned Python sai launcher"
        )
    monkeypatch.setenv("SAI_PARITY_PYTHON", reference)
    return request.getfixturevalue("cli_pair")


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


def cancel_lcd(request: Request, *, trade_type: str = "limit") -> Reply:
    assert "authorization" not in request.headers
    if request.path == NODE_INFO:
        return response({"default_node_info": {"network": ROUTE["chainId"]}})
    path = urlsplit(request.path).path
    assert path.startswith(SMART_QUERY)
    query = json.loads(base64.b64decode(unquote(path.removeprefix(SMART_QUERY))))
    assert query == {"get_trade_raw": {"trader": ADDR, "index": 17}}
    return response(
        {
            "data": {
                "user": ADDR,
                "user_trade_index": "UserTradeIndex(17)",
                "is_open": True,
                "trade_type": trade_type,
            }
        }
    )


def queue_update_preflight(replies: deque[ReplayReply]) -> None:
    def get_position(request: Request) -> Reply:
        assert "authorization" not in request.headers
        assert json.loads(request.body) == {
            "query": keeper.POSITION,
            "variables": {"trader": ADDR, "id": 17},
        }
        return response({"data": {"perp": {"trade": {"id": 17, "trader": ADDR}}}})

    def get_route(request: Request) -> Reply:
        assert "authorization" not in request.headers
        assert json.loads(request.body) == {"query": keeper.ROUTE, "variables": {}}
        return response({"data": {"oneClickTrading": ROUTE}})

    replies.extend((get_position, get_route))


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
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: Case,
) -> None:
    documents = []
    keys: list[str] = []
    update = case.args in UPDATES
    with replay(
        get=lambda request: cancel_lcd(
            request, trade_type="trade" if update else "limit"
        )
    ) as (endpoint, replies, requests):
        for index, cli in enumerate(trade_wait_pair(request, case.args)):
            home = tmp_path / str(index) / "sai"
            connect(home, endpoint, monkeypatch)
            requests.clear()
            keys.clear()

            def admit(request: Request) -> Reply:
                wire = json.loads(request.body)
                if (
                    case.kind == "CANCEL_PENDING_TRADE"
                    and wire["query"] == keeper.ROUTE
                ):
                    assert len(requests) == 1
                    assert wire["variables"] == {}
                    assert "authorization" not in request.headers
                    replies.appendleft(admit)
                    return response({"data": {"oneClickTrading": ROUTE}})
                key = submitted_key(request, case.kind)
                keys.append(key)
                if update:
                    assert (
                        wire["variables"]["input"]["intent"]["body"]
                        == UPDATES[case.args]
                    )
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
                body = (
                    {"orderType": "market"}
                    if case.kind == "OPEN_TRADE"
                    else UPDATES.get(case.args, {})
                )
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
            if update:
                queue_update_preflight(replies)
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
                cli,
                home,
                "--endpoint",
                endpoint,
                "--lcd-endpoint",
                endpoint.removesuffix("/query"),
                "--json",
                "trade",
                *args,
                "--wait",
            )
            expected_exit = {"rejected": 4, "failed": 1}.get(case.outcome, 0)
            assert result.returncode == expected_exit, result
            assert result.stderr == b""
            document = json.loads(result.stdout)
            key = document["intentKey"]
            assert keys == [key]
            assert not replies
            wire = [
                json.loads(request.body)
                if request.body
                else {"path": urlsplit(request.path).path}
                for request in requests
            ]
            if case.kind == "CANCEL_PENDING_TRADE":
                assert wire[0]["query"] == keeper.ROUTE
                assert requests[1].path == NODE_INFO
                assert requests[2].path.startswith(SMART_QUERY)
            elif update:
                assert wire[0]["query"] == keeper.POSITION
                assert wire[1]["query"] == keeper.ROUTE
                assert requests[2].path == NODE_INFO
                assert requests[3].path.startswith(SMART_QUERY)
            else:
                assert all(request.body for request in requests)
            assert (
                sum(item.get("query", "").startswith("mutation") for item in wire) == 1
            )
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
                    if update:
                        assert receipt["execution"]["tradeChangeType"] == case.change
                        assert receipt["fill"] is None
            if update and expected_exit:
                assert (
                    document["error"]["code"]
                    == {"rejected": "ADMISSION_REJECTED", "failed": "KEEPER_ERROR"}[
                        case.outcome
                    ]
                )
            ledger = ledger_state(home)
            statuses = (
                ("rejected", None)
                if case.outcome == "rejected"
                else ("terminal", "terminal")
            )
            assert ledger == [(key, *statuses)]
            for item in wire:
                if item.get("query", "").startswith("mutation"):
                    submission = item["variables"]["input"]
                    stamp = submission["intent"].pop("tsIntentInit")
                    assert submission["lifecycle"].pop("tsLifecycleStep") == stamp
            documents.append(
                (
                    result.stdout.replace(key.encode(), b"<intent>"),
                    normalized([wire, ledger], key),
                )
            )
    assert documents[0] == documents[1]


@pytest.mark.parametrize("wait", [(), ("--wait",)], ids=["submit", "wait"])
def test_trade_cancel_refuses_a_filled_target_before_mutation(
    wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait: tuple[str, ...],
) -> None:
    def route(request: Request) -> Reply:
        wire = json.loads(request.body)
        if wire["query"].startswith("mutation"):
            # Reject a premature mutation so the failing case returns promptly.
            return response(
                {"errors": [{"extensions": {"code": "INTENT_NOT_ACCEPTED"}}]}
            )
        assert wire == {"query": keeper.ROUTE, "variables": {}}
        return response({"data": {"oneClickTrading": ROUTE}})

    with replay(get=lambda request: cancel_lcd(request, trade_type="trade")) as (
        endpoint,
        replies,
        requests,
    ):
        for index, cli in enumerate(wait_cli_pair):
            home = tmp_path / str(index) / "sai"
            connect(home, endpoint, monkeypatch)
            requests.clear()
            replies.append(route)
            result = invoke(
                cli,
                home,
                "--endpoint",
                endpoint,
                "--lcd-endpoint",
                endpoint.removesuffix("/query"),
                "--json",
                "trade",
                *CANCEL,
                *wait,
            )
            assert not any(
                request.body
                and json.loads(request.body)["query"].startswith("mutation")
                for request in requests
            )
            assert len(requests) == 3
            assert result.returncode == 1
            assert result.stderr == b""
            assert json.loads(result.stdout)["error"]["code"] == "INVALID_REQUEST"
            assert not (home / "trader/intents.sqlite3").exists()


@pytest.mark.parametrize(
    "args", [CLOSE, TPSL, LEVERAGE], ids=["close", "tpsl", "leverage"]
)
def test_trade_wait_dry_run(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
) -> None:
    update = args in UPDATES
    with replay(get=lambda request: cancel_lcd(request, trade_type="trade")) as (
        endpoint,
        replies,
        requests,
    ):
        for index, cli in enumerate(trade_wait_pair(request, args)):
            home = tmp_path / str(index) / "sai"
            if update:
                connect(home, endpoint, monkeypatch)
            else:
                home.mkdir(parents=True)
            requests.clear()
            documents = []
            counts = []
            for wait in ((), ("--wait",)):
                if update:
                    queue_update_preflight(replies)
                start = len(requests)
                result = invoke(
                    cli,
                    home,
                    "--endpoint",
                    endpoint,
                    "--lcd-endpoint",
                    endpoint.removesuffix("/query"),
                    "--json",
                    "trade",
                    *args,
                    "--dry-run",
                    *wait,
                )
                assert result.returncode == 0, result
                assert result.stderr == b""
                document = json.loads(result.stdout)
                assert document["dryRun"] is True
                assert "receipt" not in document
                documents.append(document)
                counts.append(len(requests) - start)
            assert counts[0] == counts[1]
            assert all(
                not request.body
                or not json.loads(request.body)["query"].startswith("mutation")
                for request in requests
            )
            assert not replies
            assert not (home / "trader/intents.sqlite3").exists()
            assert documents[0] == documents[1]


@pytest.mark.parametrize("phase", ["submit", "poll"])
@pytest.mark.parametrize(
    "args", [CLOSE, TPSL, LEVERAGE], ids=["close", "tpsl", "leverage"]
)
def test_trade_wait_interrupt_retains_reservation(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    args: tuple[str, ...],
) -> None:
    documents = []
    update = args in UPDATES
    kind = "UPDATE_TRADE" if update else "CLOSE_POSITION"
    preflight_reads = 4 if update else 0
    with replay(get=lambda request: cancel_lcd(request, trade_type="trade")) as (
        endpoint,
        replies,
        requests,
    ):
        for index, cli in enumerate(trade_wait_pair(request, args)):
            home = tmp_path / str(index) / "sai"
            connect(home, endpoint, monkeypatch)
            requests.clear()
            if update:
                queue_update_preflight(replies)
            command = (
                "--endpoint",
                endpoint,
                "--lcd-endpoint",
                endpoint.removesuffix("/query"),
                "--json",
                "trade",
                *args,
                "--wait",
            )
            # A zero-length body completes as soon as the headers arrive.
            delayed = Reply(body=b"{}", body_delay=4.0)
            replies.append(delayed if phase == "submit" else Reply(disconnect=True))
            if phase == "poll":
                replies.append(delayed)
            with subprocess.Popen(
                [*cli, *command],
                env=cli_environment(home),
                cwd=home.parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as process:
                try:
                    end = time.monotonic() + 5
                    expected_requests = preflight_reads + (
                        1 if phase == "submit" else 2
                    )
                    while len(requests) < expected_requests and time.monotonic() < end:
                        time.sleep(0.01)
                    assert len(requests) == expected_requests
                    process.send_signal(signal.SIGINT)
                    stdout, stderr = process.communicate(timeout=2)
                finally:
                    if process.poll() is None:
                        process.kill()
                assert process.returncode == 5, (stdout, stderr)
            key = submitted_key(requests[preflight_reads], kind)
            assert stderr == b""
            document = json.loads(stdout)
            assert document["intentKey"] == key
            assert document["outcome"] == document["localStatus"] == "unknown"
            assert document["interrupted"] is True
            assert document["code"] == "OUTCOME_UNKNOWN"
            assert document["receipt"] is None
            assert ledger_state(home) == [(key, "unknown", None)]
            if phase == "poll":
                assert json.loads(requests[-1].body)["variables"]["key"] == key
            if update:
                queue_update_preflight(replies)
            again = invoke(cli, home, *command)
            assert again.returncode == 1, again
            assert json.loads(again.stdout)["error"]["intentKey"] == key
            assert len(requests) == expected_requests + preflight_reads
            assert not replies
            documents.append(normalized(document, key))
    assert documents[0] == documents[1]


def test_position_trade_wait_times_out(
    position_wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_wait(
        cli: CLI,
        home: Path,
        endpoint: str,
        args: tuple[str, ...],
        replies: deque[ReplayReply],
        requests: list[Request],
    ) -> dict[str, Any]:
        keys: list[str] = []

        def admit(request: Request) -> Reply:
            key = submitted_key(request, "UPDATE_TRADE")
            keys.append(key)
            return accepted(key, "UPDATE_TRADE")

        def poll(request: Request) -> Reply:
            assert json.loads(request.body)["variables"] == {
                "user": ADDR,
                "key": keys[0],
                "includeAction": True,
            }
            replies.append(poll)
            return response(
                intent(
                    "pending_exec",
                    intentKey=keys[0],
                    kind="UPDATE_TRADE",
                    body=UPDATES[args],
                )
            )

        queue_update_preflight(replies)
        replies.extend((admit, poll))
        result = subprocess.run(
            [
                *cli,
                "--endpoint",
                endpoint,
                "--lcd-endpoint",
                endpoint.removesuffix("/query"),
                "--json",
                "trade",
                *args,
                "--wait",
            ],
            env=cli_environment(home),
            cwd=home.parent,
            capture_output=True,
            check=False,
            timeout=45,
        )
        assert (result.returncode, result.stderr) == (5, b"")
        document = json.loads(result.stdout)
        key = document["intentKey"]
        assert keys == [key]
        assert len(requests) > 6
        assert document["outcome"] == document["status"] == "unknown"
        assert document["code"] == document["error"]["code"] == "OUTCOME_UNKNOWN"
        assert document["receipt"]["requestBlock"] == REQUEST_BLOCK
        assert document["receipt"]["execution"] is None
        assert ledger_state(home) == [(key, "unknown", "pending")]
        waits = [
            (event["event"], event.get("timeoutMs"), event.get("code"))
            for event in read_events(home)
            if event.get("intentKey") == key
            and event["event"].startswith("intent.wait.")
        ]
        assert waits == [
            ("intent.wait.start", 30_000, None),
            ("intent.wait.end", None, "OUTCOME_UNKNOWN"),
        ]
        return normalized(document, key)

    # Both commands and runtimes exercise the real 30-second deadline concurrently.
    with ExitStack() as servers, ThreadPoolExecutor(max_workers=4) as batch:
        pairs = []
        for args in UPDATES:
            pending = []
            for index, cli in enumerate(position_wait_cli_pair):
                endpoint, replies, requests = servers.enter_context(
                    replay(get=lambda request: cancel_lcd(request, trade_type="trade"))
                )
                home = tmp_path / args[0] / str(index) / "sai"
                connect(home, endpoint, monkeypatch)
                pending.append(
                    batch.submit(
                        timed_wait, cli, home, endpoint, args, replies, requests
                    )
                )
            pairs.append(pending)
        for pair in pairs:
            assert pair[0].result() == pair[1].result()


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


def test_trade_wait_interrupts_a_full_stdout_pipe(
    wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            read_fd, write_fd = os.pipe()
            os.set_blocking(write_fd, False)
            try:
                while True:
                    os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                os.set_blocking(write_fd, True)
            try:
                with subprocess.Popen(
                    [*cli, "--endpoint", endpoint, "--json", "trade", *CLOSE, "--wait"],
                    env=cli_environment(home),
                    cwd=home.parent,
                    stdout=write_fd,
                    stderr=subprocess.PIPE,
                ) as process:
                    try:
                        end = time.monotonic() + 3
                        while len(requests) < 3 and time.monotonic() < end:
                            time.sleep(0.01)
                        assert len(requests) == 3
                        time.sleep(0.15)
                        assert process.poll() is None
                        assert os.get_blocking(write_fd), (
                            "CLI changed the inherited pipe's flags"
                        )
                        process.send_signal(signal.SIGINT)
                        assert process.stderr is not None
                        assert select.select([process.stderr], [], [], 3)[0], (
                            "no stderr recovery with stdout blocked"
                        )
                        document = json.loads(process.stderr.readline())
                        if index == 1:
                            assert process.wait(timeout=3) == 1
                        # Let Python's final stdout flush finish after reading recovery.
                        os.set_blocking(read_fd, False)
                        while select.select([read_fd], [], [], 0)[0]:
                            if not os.read(read_fd, 65536):
                                break
                        assert process.wait(timeout=3) == 1
                    finally:
                        if process.poll() is None:
                            process.kill()
            finally:
                os.close(read_fd)
                os.close(write_fd)
            key = submitted_key(requests[0], kind)
            assert document["intentKey"] == key
            assert document["interrupted"] is True
            assert document["localStatus"] == document["outcome"] == "terminal"
            assert ledger_state(home) == [(key, "terminal", "terminal")]
