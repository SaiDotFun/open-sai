"""Compare intent status through both CLIs against Keeper's recorded replays."""

from __future__ import annotations

import gzip
import json
import os
import select
import shutil
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from compare import is_version
from sai_cli.core import keeper, log, reconcile, storage
from sai_cli.tests.loopback import graphql_ws
from websockets.http11 import Request as UpgradeRequest
from websockets.http11 import Response as UpgradeResponse
from websockets.sync.server import ServerConnection

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "cli/sai_cli/tests/fixtures/intent_outcomes.json"
    ).read_text(encoding="utf-8")
)
ADDR = FIXTURE["address"]
KEY = FIXTURE["key"]
RESPONSES = {**FIXTURE["recorded"], **FIXTURE["replays"]}
NIBI = RESPONSES["pending_exec"]["data"]["userIntents"][0]["nibiAddress"]
OTHER = "0x" + "b" * 40
OTHER_NIBI = "nibi1hwamhwamhwamhwamhwamhwamhwamhwamc9gs8a"
CLI = tuple[str, ...]
# Rust C4 targets the public error codes from the pinned Python contract.
C4_INTENT_ERROR_CODES = frozenset(
    {
        "SLIPPAGE",
        "simulation_failed",
        "admission_expired",
        "broadcast_rejected",
        "chain_execution_failed",
        "market_order_expired",
        "market_order_fill_failed",
        "market_order_trading_paused",
        "market_order_market_closed",
        "market_order_trade_not_open",
        "market_order_outcome_not_indexed",
        "intent_already_used",
        "below_minimum",
        "insufficient_idle",
    }
)


@dataclass(frozen=True)
class Reply:
    body: bytes = b""
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()
    body_delay: float = 0
    header_delay: float = 0
    disconnect: bool = False


@dataclass(frozen=True)
class Request:
    path: str
    version: str
    headers: dict[str, str]
    body: bytes


@contextmanager
def replay(
    tls: ssl.SSLContext | None = None,
) -> Iterator[tuple[str, deque[Reply], list[Request]]]:
    replies: deque[Reply] = deque()
    requests: list[Request] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(
                Request(
                    self.path,
                    self.request_version,
                    {name.lower(): value for name, value in self.headers.items()},
                    body,
                )
            )
            if not replies:
                self.send_error(500, "Unexpected Keeper request")
                return
            reply = replies.popleft()
            if reply.disconnect:
                self.close_connection = True
                return
            self.send_response(reply.status)
            if reply.header_delay:
                self.flush_headers()
                time.sleep(reply.header_delay)
                self.send_header("Content-Type", "application/json")
                self.flush_headers()
                time.sleep(reply.header_delay)
            for name, value in reply.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            time.sleep(reply.body_delay)
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(reply.body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        if tls is not None:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        scheme = "https" if tls is not None else "http"
        worker = threading.Thread(
            target=server.serve_forever, args=(0.01,), daemon=True
        )
        worker.start()
        try:
            yield f"{scheme}://127.0.0.1:{server.server_port}/query", replies, requests
        finally:
            server.shutdown()
            worker.join()


def invoke(
    cli: CLI,
    home: Path,
    *args: str,
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*cli, *args],
        env={
            "PATH": os.defpath,
            "HOME": str(home.parent),
            "SAI_HOME": str(home),
            "SAI_LOG": "1",
            "SAI_UPDATE_AUTO": "0",
            "PYTHONUTF8": "1",
            **(overrides or {}),
        },
        cwd=home.parent,
        capture_output=True,
        check=False,
        timeout=30,
    )


@cache
def cli_version(cli: CLI) -> str:
    with tempfile.TemporaryDirectory(prefix="sai-parity-version-") as directory:
        result = invoke(cli, Path(directory) / "sai", "--version")
    assert result.returncode == 0, result.stderr
    version = result.stdout.decode().split()[-1]
    assert is_version(version)
    return version


def read_events(home: Path, cli: CLI) -> list[dict[str, Any]]:
    events = [
        json.loads(line)
        for path in sorted((home / "logs").glob("*.jsonl"))
        for line in path.read_bytes().splitlines()
    ]
    groups: dict[str, list[int]] = {}
    version = cli_version(cli)
    for event in events:
        assert event["version"] == version
        event["version"] = "<version>"
        run = event["runId"]
        event["runId"] = list(groups).index(run) if run in groups else len(groups)
        seq = groups.setdefault(run, [])
        seq.append(event["seq"])
        for dynamic in ("ts", "pid", "durationMs"):
            if dynamic in event:
                assert isinstance(event[dynamic], str if dynamic == "ts" else int)
                event[dynamic] = "<dynamic>"
    assert all(seq == list(range(1, len(seq) + 1)) for seq in groups.values())
    return events


def read_state(home: Path) -> tuple[Any, ...]:
    files = {
        str(path.relative_to(home)): path.stat().st_mode for path in home.rglob("*")
    }
    db = home / "trader/intents.sqlite3"
    if not db.exists():
        return (files,)
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        schema = connection.execute("SELECT sql FROM sqlite_master ORDER BY name")
        version = connection.execute("PRAGMA user_version").fetchone()
        rows = connection.execute("SELECT * FROM intents ORDER BY id")
        return files, schema.fetchall(), version, rows.fetchall()


def seed(
    home: Path, endpoint: str, monkeypatch: pytest.MonkeyPatch, initial: str
) -> None:
    monkeypatch.setenv("SAI_HOME", str(home))
    with log.silenced():
        storage.pending(KEY, endpoint, ADDR, "OPEN_TRADE", "17")
        storage.pending("foreign", endpoint, OTHER, "OPEN_TRADE", "17")
        if initial in {"accepted", "pending", "terminal"}:
            storage.set_status(KEY, endpoint, ADDR, initial)
        with closing(storage.db()) as connection, connection:
            # A fixed, expired timestamp makes the two ledgers comparable.
            connection.execute("UPDATE intents SET created_at=1")
            if initial in {"absent", "legacy_terminal"}:
                connection.execute(
                    "UPDATE intents SET status=? WHERE id=?",
                    (initial.removeprefix("legacy_"), KEY),
                )


def response(payload: str | dict[str, Any] | Reply) -> Reply:
    if isinstance(payload, Reply):
        return payload
    if isinstance(payload, str):
        payload = RESPONSES[payload]
    return Reply(json.dumps(payload).encode())


def wire(query: str, **variables: str | int | None) -> bytes:
    return json.dumps(
        {"query": query, "variables": variables},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


@dataclass(frozen=True)
class Step:
    replies: tuple[str | dict[str, Any] | Reply, ...]
    local: str | None
    key: str | None = KEY
    addr: str = ADDR
    code: int = 0
    error: str | None = None
    json_output: bool = True


def c4_timestamp_field(
    projected: dict[str, Any], source: dict[str, Any], field: str
) -> dict[str, Any]:
    """Use the original C4 timezone-aware ISO parser on the pinned CLI value."""
    value = source.get(field)
    timestamp = None
    if isinstance(value, str) and len(value) <= 40:
        with suppress(ValueError):
            if datetime.fromisoformat(value).tzinfo is not None:
                timestamp = value

    # A timestamp and its invalid flag occupy the same slot in the projection.
    invalid = field + "Invalid"
    return {
        (field if name == invalid else name): (
            timestamp if name in (field, invalid) else value
        )
        for name, value in projected.items()
        if name not in (field, invalid) or timestamp is not None
    }


def public_intent_output(output: bytes, *, compact: bool) -> bytes:
    """Project pinned CLI stdout to the Rust C4 field contract."""
    document = json.loads(output)
    public = reconcile.status_report(document)
    for name in ("userIntents", "matchedTradeHistory"):
        if name not in public:
            continue
        rows = (row for row in document[name] if isinstance(row, dict))
        for index, source in enumerate(rows):
            row = public[name][index]
            if name == "userIntents":
                row = c4_timestamp_field(row, source, "execBlockTs")
                if "errorCode" in row and row["errorCode"] not in C4_INTENT_ERROR_CODES:
                    row["errorCode"] = "KEEPER_ERROR"
                public[name][index] = row
            elif isinstance(block := source.get("block"), dict):
                row["block"] = c4_timestamp_field(row["block"], block, "block_ts")
    return (
        json.dumps(log.clean(public), indent=None if compact else 2) + "\n"
    ).encode()


def test_pinned_projection_preserves_c4_timestamps():
    c4_time = "2026-11-20 15:43:31.1234567890123456Z"
    document = {
        "userIntents": [
            {"execBlockTs": c4_time, "txHash": "a" * 64},
            {"execBlockTs": "2026-11-20T15:43:31"},
        ],
        "matchedTradeHistory": [
            {"block": {"block": 123, "block_ts": c4_time}},
            {"block": {"block_ts": "invalid"}},
        ],
    }
    expected = {
        "userIntents": [{"execBlockTs": c4_time, "txHash": "a" * 64}, {}],
        "matchedTradeHistory": [
            {"block": {"block": 123, "block_ts": c4_time}},
            {"block": {}},
        ],
    }
    assert (
        public_intent_output(json.dumps(document).encode(), compact=True)
        == (json.dumps(expected) + "\n").encode()
    )


def compare(
    cli_pair: tuple[CLI, CLI],
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    steps: tuple[Step, ...],
    initial: str | None = "unknown",
) -> None:
    with replay() as (endpoint, replies, requests):
        fixture = root / "fixture"
        fixture.mkdir(mode=0o700)
        if initial is not None:
            seed(fixture, endpoint, monkeypatch, initial)
        observed = []
        for index, cli in enumerate(cli_pair):
            home = root / str(index)
            shutil.copytree(fixture, home)
            runs = []
            for step in steps:
                replies.extend(response(item) for item in step.replies)
                requests.clear()
                args = ["--endpoint", endpoint]
                if step.json_output:
                    args.append("--json")
                args.extend(("intent", "status", step.addr))
                if step.key is not None:
                    args.extend(("--key", step.key))
                result = invoke(cli, home, *args)
                assert result.returncode == step.code, (result.stdout, result.stderr)
                assert not replies
                assert [request.body for request in requests] == [
                    wire(keeper.INTENTS, user=step.addr, key=step.key),
                    *[wire(keeper.HISTORY, trader=step.addr)] * (len(step.replies) - 1),
                ]
                if step.json_output:
                    assert not result.stderr
                    assert len(result.stdout.splitlines()) == 1
                    document = json.loads(result.stdout)
                    if step.error:
                        assert document["error"]["code"] == step.error
                        assert document["error"]["retryable"] is (step.code == 4)
                state = read_state(home)
                if step.local is not None:
                    _, _, _, rows = state
                    _, _, _, _, stored, _, _, keeper_status = next(
                        row for row in rows if row[0] == KEY
                    )
                    assert (keeper_status or stored) == step.local
                    assert (
                        next(row for row in rows if row[0] == "foreign")[4] == "unknown"
                    )
                stdout = result.stdout
                if index == 0 and step.code == 0:
                    stdout = public_intent_output(stdout, compact=step.json_output)
                runs.append((stdout, result.stderr, list(requests), state))
            observed.append((runs, read_events(home, cli)))
        assert observed[0] == observed[1]


@pytest.mark.parametrize(
    ("name", "local"),
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
def test_lifecycle(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    local: str,
) -> None:
    compare(cli_pair, tmp_path, monkeypatch, (Step((name,), local),))


@pytest.mark.parametrize(
    ("history", "local"),
    [
        (("server_finality", "open_history"), "terminal"),
        (("user_finality", "not_yet_in_history", "not_yet_in_history"), "pending"),
        (("other_trade_finality", "other_trade_history"), "terminal"),
        (("other_trade_executed", "other_trade_history"), "terminal"),
        (("limit_placement_exec_pass", "limit_placement_history"), "terminal"),
        (("filled", "not_yet_in_history", "not_yet_in_history"), "terminal"),
        (("outcome_not_indexed", "not_yet_in_history", "filled_history"), "terminal"),
        (
            ("outcome_not_indexed", "not_yet_in_history", "not_yet_in_history"),
            "pending",
        ),
        (("outcome_not_indexed", Reply(status=503)), "pending"),
        (
            (
                "outcome_not_indexed",
                Reply(response("not_yet_in_history").body, body_delay=2.25),
            ),
            "pending",
        ),
    ],
)
def test_history(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history: tuple[str | Reply, ...],
    local: str,
) -> None:
    compare(cli_pair, tmp_path, monkeypatch, (Step(history, local),))


def test_split_history_headers_keep_the_inactivity_timer_alive(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fill = Reply(response("filled_history").body, header_delay=1.2)
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (Step(("outcome_not_indexed", fill), "terminal"),),
    )


def test_empty_history_object_retries(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = {"data": {"perp": {"tradeHistory": {}}}}
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (Step(("outcome_not_indexed", empty, "filled_history"), "terminal"),),
    )


@pytest.mark.parametrize("key", [KEY, None])
def test_late_admission_and_stale_results(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str | None,
) -> None:
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (
            Step(("accepted",), "unknown", None),
            Step(("absent",), "absent"),
            Step(("accepted",), "pending", key),
            Step(("outcome_not_indexed",), "pending", None),
            Step(("submitted",), "pending"),
            Step(("absent",), "pending"),
            Step(("market_order_expired",), "pending", None),
            Step(("market_order_expired",), "terminal"),
            Step(("pending_exec",), "terminal"),
        ),
    )


def test_legacy_terminal_repairs_only_for_market_pending(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (
            Step(
                ("server_finality", "not_yet_in_history", "not_yet_in_history"),
                "terminal",
            ),
            Step(("pending_exec",), "pending"),
            Step(("market_order_expired",), "terminal"),
            Step(("pending_exec",), "terminal"),
        ),
        "legacy_terminal",
    )


def test_filters_foreign_rows_and_correlates_canonical_addresses(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = RESPONSES["pending_exec"]["data"]["userIntents"][0]
    invalid_rows = [
        {**row, "evmAddress": OTHER},
        {**row, "nibiAddress": OTHER_NIBI},
        {**row, "nibiAddress": NIBI[:-1] + "q"},
        {**row, "intentKey": "foreign"},
    ]
    foreign = {"data": {"userIntents": invalid_rows}}
    fill = RESPONSES["filled_history"]["data"]["perp"]["tradeHistory"][0]
    foreign_history = {
        "data": {
            "perp": {
                "tradeHistory": [
                    {**fill, "intentKey": "foreign"},
                    {**fill, "trade": {**fill["trade"], "trader": OTHER}},
                    {**fill, "txHash": "C" * 64, "evmTxHash": None},
                ]
            }
        }
    }
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (
            Step((foreign,), "unknown", addr=NIBI.upper()),
            Step(("pending_exec",), "pending", addr=NIBI.upper()),
            Step(
                ("outcome_not_indexed", foreign_history, "filled_history"),
                "terminal",
                addr=NIBI,
            ),
        ),
    )


def test_absent_response_only_creates_ledger_for_valid_keyed_account(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (
            Step(("absent",), None, None),
            Step(("absent",), None, addr="invalid"),
            Step(({"data": {"userIntents": None}},), None),
            Step(("pending_exec",), None, None, json_output=False),
            Step(("absent",), None),
        ),
        None,
    )


@pytest.mark.parametrize(
    ("replies", "local", "error"),
    [
        (
            (Reply(status=302, headers=(("Location", "/elsewhere"),)),),
            "unknown",
            "KEEPER_ERROR",
        ),
        ((Reply(status=401),), "unknown", "KEEPER_ERROR"),
        ((Reply(status=500),), "unknown", "KEEPER_ERROR"),
        ((Reply(disconnect=True),), "unknown", "KEEPER_ERROR"),
        ((Reply(b"malformed"),), "unknown", "KEEPER_ERROR"),
        ((Reply(b"[]"),), "unknown", "KEEPER_ERROR"),
        ((Reply(b'{"data":null}'),), "unknown", "KEEPER_ERROR"),
        (({"data": {"userIntents": 123}},), "unknown", "INCOMPLETE_DATA"),
        (({"data": {"userIntents": [None]}},), "unknown", "INTERNAL_ERROR"),
        (("outcome_not_indexed", {"data": {}}), "pending", "INCOMPLETE_DATA"),
        (
            ("outcome_not_indexed", {"data": {"perp": {"tradeHistory": None}}}),
            "pending",
            "INCOMPLETE_DATA",
        ),
    ],
)
def test_request_and_data_failures(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replies: tuple[str | dict[str, Any] | Reply, ...],
    local: str,
    error: str,
) -> None:
    compare(
        cli_pair, tmp_path, monkeypatch, (Step(replies, local, code=1, error=error),)
    )


@pytest.mark.parametrize("operation", ["intents", "history"])
def test_intent_status_logs_only_public_graphql_codes(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, operation: str
) -> None:
    diagnostic = "synthetic Keeper diagnostic"
    failure = {
        "data": {"userIntents": [], "perp": {"tradeHistory": []}},
        "errors": [
            {"message": diagnostic, "extensions": {"code": "FORBIDDEN"}},
            {"message": diagnostic, "extensions": {"code": "PRIVATE_CODE"}},
            {"message": diagnostic},
            {"message": diagnostic, "extensions": {"code": "BAD_USER_INPUT"}},
        ],
    }
    # The Python checkout has the current logging policy. C4's pinned CLI
    # predates it and remains the reference for the other reconciliation tests.
    candidate = cli_pair[1]
    reference = (sys.executable, "-m", "sai_cli.sai_cli")
    with replay() as (endpoint, replies, requests):
        for index, cli in enumerate((reference, candidate)):
            home = tmp_path / str(index)
            requests.clear()
            expected_requests = [wire(keeper.INTENTS, user=ADDR, key=KEY)]
            if operation == "history":
                replies.append(response("outcome_not_indexed"))
                history_request = (
                    wire(keeper.INTENT_HISTORY, trader=ADDR, limit=25, offset=0)
                    if index == 0
                    else wire(keeper.HISTORY, trader=ADDR)
                )
                expected_requests.append(history_request)
            replies.append(response(failure))
            result = invoke(
                cli,
                home,
                "--endpoint",
                endpoint,
                "--json",
                "intent",
                "status",
                ADDR,
                "--key",
                KEY,
            )
            assert not replies
            assert [request.body for request in requests] == expected_requests
            assert result.stderr == b""
            document = json.loads(result.stdout)
            if operation == "intents":
                assert result.returncode == 1
                assert document["error"]["code"] == "KEEPER_ERROR"
            else:
                assert result.returncode == 0
                assert document["outcome"] == "pending"
            [event] = [
                event
                for event in read_events(home, cli)
                if event["event"] == "keeper.request"
                and event["operation"] == operation
            ]
            assert event["code"] == "KEEPER_ERROR"
            assert event["keeperErrors"] == [
                {"code": "FORBIDDEN"},
                {"code": "KEEPER_ERROR"},
                {"code": "KEEPER_ERROR"},
            ]
            assert event["keeperErrorCount"] == 4


def test_compressed_keeper_response(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compressed = Reply(
        gzip.compress(response("pending_exec").body),
        headers=(("Content-Encoding", "gzip"),),
    )
    compare(cli_pair, tmp_path, monkeypatch, (Step((compressed,), "pending"),))


def test_connection_refused(cli_pair: tuple[CLI, CLI], tmp_path: Path) -> None:
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{unavailable.getsockname()[1]}/query"
        outputs = []
        for index, cli in enumerate(cli_pair):
            home = tmp_path / str(index)
            result = invoke(
                cli, home, "--json", "--endpoint", endpoint, "intent", "status", ADDR
            )
            assert result.returncode == 4
            assert result.stderr == b""
            assert json.loads(result.stdout)["error"]["code"] == "NOT_SENT"
            assert not (home / "trader").exists()
            outputs.append((result.stdout, read_events(home, cli)))
        assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    ("args", "exit_code", "code"),
    [
        (("--json",), 2, "INVALID_REQUEST"),
        (("--json", "intent"), 2, "INVALID_REQUEST"),
        (("--json", "config"), 2, "INVALID_REQUEST"),
        (("--bad-option", "--json", "intent", "status", ADDR), 2, "INVALID_REQUEST"),
        (("--json", "intent", "status"), 2, "INVALID_REQUEST"),
        (
            ("--json", "intent", "status", ADDR, "--key"),
            2,
            "INVALID_REQUEST",
        ),
        (("--json", "config", "get", "invalid"), 2, "INVALID_REQUEST"),
        (
            (
                "--json",
                "--endpoint",
                "http://keeper.invalid/query",
                "intent",
                "status",
                ADDR,
            ),
            1,
            "KEEPER_ERROR",
        ),
        (
            (
                "--json",
                "--endpoint",
                "https://user:pass@keeper.invalid/query",
                "intent",
                "status",
                ADDR,
            ),
            1,
            "KEEPER_ERROR",
        ),
        (("--json", "auth", "status"), 6, "SIGNED_OUT"),
    ],
)
def test_cli_errors_share_one_envelope(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    args: tuple[str, ...],
    exit_code: int,
    code: str,
) -> None:
    outputs = []
    for index, cli in enumerate(cli_pair):
        result = invoke(cli, tmp_path / str(index), *args)
        assert result.returncode == exit_code
        assert not result.stderr
        assert len(result.stdout.splitlines()) == 1
        assert json.loads(result.stdout)["error"]["code"] == code
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("args", [("intent", "--help"), ("intent", "status", "--help")])
def test_intent_help(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, args: tuple[str, ...]
) -> None:
    outputs = []
    for index, cli in enumerate(cli_pair):
        home = tmp_path / str(index)
        result = invoke(cli, home, *args)
        assert result.returncode == 0
        assert not result.stderr
        assert not home.exists()
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("path", ["/query", "/private/query"])
def test_expired_auth_status(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    endpoint = f"http://127.0.0.1:9{path}"
    fixture = tmp_path / "fixture"
    monkeypatch.setenv("SAI_HOME", str(fixture))
    with log.silenced():
        storage.save_session(
            {
                "evmAddress": ADDR,
                "endpoint": endpoint,
                "token": "fixture-token",
                "expiresAt": "2000-01-01T00:00:00Z",
            }
        )
    observed = []
    for index, cli in enumerate(cli_pair):
        home = tmp_path / str(index)
        shutil.copytree(fixture, home)
        result = invoke(cli, home, "--json", "--endpoint", endpoint, "auth", "status")
        assert result.returncode == 6
        assert result.stderr == b""
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "SESSION_EXPIRED"
        assert error["hint"].endswith(f"auth login NAME --addr {ADDR}")
        observed.append((result.stdout, read_events(home, cli)))
    assert observed[0] == observed[1]


@pytest.mark.parametrize("json_output", [True, False])
@pytest.mark.parametrize("known_code", [True, False])
@pytest.mark.parametrize("valid_hash", [True, False])
def test_public_intent_status_scrubs_keeper_payload(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
    known_code: bool,
    valid_hash: bool,
) -> None:
    token = "a1b2c3d4" * 8 if valid_hash else "synthetic-stored-session"
    private = f"Keeper message: {token} " + "x" * 500
    with replay() as (endpoint, replies, requests):
        expected = "market_order_expired" if known_code else "KEEPER_ERROR"
        for index, cli in enumerate(cli_pair):
            home = tmp_path / str(index)
            monkeypatch.setenv("SAI_HOME", str(home))
            with log.silenced():
                storage.save_session(
                    {
                        "evmAddress": ADDR,
                        "endpoint": endpoint,
                        "token": token,
                        "expiresAt": "2099-01-01T00:00:00Z",
                    }
                )
            row = RESPONSES["market_order_expired"]["data"]["userIntents"][0]
            row = {
                **row,
                "errorMsg": private,
                "errorCode": expected if known_code else token,
                "txHash": token,
                "serverDiagnostic": token,
            }
            requests.clear()
            replies.append(
                response(
                    {"data": {"userIntents": [row], "hint": private, "extra": token}}
                )
            )
            args = ["--endpoint", endpoint]
            if json_output:
                args.append("--json")
            result = invoke(cli, home, *args, "intent", "status", ADDR, "--key", KEY)
            assert result.returncode == 0
            assert not result.stderr
            assert [request.body for request in requests] == [
                wire(keeper.INTENTS, user=ADDR, key=KEY)
            ]
            output = result.stdout
            if index == 0:
                # The pinned CLI returns raw rows. Compare the public
                # projection and always check Rust stdout as emitted.
                output = public_intent_output(output, compact=json_output)
            assert token.encode() not in output
            assert private.encode() not in output
            envelope = json.loads(output)
            assert set(envelope) == {"userIntents"}
            public = envelope["userIntents"][0]
            assert "serverDiagnostic" not in public
            if valid_hash:
                assert public["txHash"] == "[redacted session token]"
            else:
                assert "txHash" not in public
                assert public["txHashInvalid"] is True
            assert public["errorCode"] == expected
            assert public["errorMsg"] == "Keeper reported an intent error."


def test_public_history_projects_after_raw_hash_correlation(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opaque = "synthetic-keeper-text"
    c4_time = "2026-11-20 15:43:31.1234567890123456Z"
    intent = RESPONSES["outcome_not_indexed"]["data"]["userIntents"][0]
    history = {
        "intentKey": KEY,
        "id": opaque,
        "tradeChangeType": opaque,
        "txHash": opaque,
        "evmTxHash": "0X" + "CD" * 32,
        "trade": {"id": opaque, "trader": ADDR, "note": opaque},
        "block": {"block": "2142-2", "block_ts": c4_time},
        "debug": opaque,
    }
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (
            Step(
                (
                    {
                        "data": {
                            "userIntents": [
                                {**intent, "txHash": opaque, "execBlockTs": c4_time}
                            ],
                            "extra": opaque,
                        }
                    },
                    {"data": {"perp": {"tradeHistory": [history]}}},
                ),
                "terminal",
            ),
        ),
    )


@pytest.mark.parametrize(
    ("change", "projected"),
    [("synthetic-keeper-text", None), ("position_liquidated", "position_liquidated")],
)
def test_public_history_preserves_liquidation_label(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    projected: str | None,
) -> None:
    opaque = "synthetic-keeper-text"
    intent = RESPONSES["outcome_not_indexed"]["data"]["userIntents"][0]
    history = {
        "intentKey": KEY,
        "id": opaque,
        "tradeChangeType": change,
        "txHash": opaque,
        "evmTxHash": "0X" + "CD" * 32,
        "trade": {"id": opaque, "trader": ADDR, "note": opaque},
        "block": {"block": "2142-2", "block_ts": opaque},
        "debug": opaque,
    }

    with replay() as (endpoint, replies, _requests):
        for index, cli in enumerate(cli_pair):
            home = tmp_path / str(index)
            home.mkdir(mode=0o700)
            seed(home, endpoint, monkeypatch, "unknown")
            replies.clear()
            replies.extend(
                [
                    response({"data": {"userIntents": [{**intent, "txHash": opaque}]}}),
                    response({"data": {"perp": {"tradeHistory": [history]}}}),
                ]
            )
            result = invoke(
                cli,
                home,
                "--endpoint",
                endpoint,
                "--json",
                "intent",
                "status",
                ADDR,
                "--key",
                KEY,
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
            [row] = json.loads(result.stdout)["matchedTradeHistory"]
            if index == 0:
                # The pinned Python CLI returns raw rows. Apply its public projection.
                row = keeper.safe_intent_history(row)
            assert row.get("tradeChangeType") == projected


@pytest.fixture(scope="module")
def keeper_certificate(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[ssl.SSLContext, Path]:
    certs = tmp_path_factory.mktemp("keeper-ca")

    def openssl(*args: str) -> str:
        return subprocess.run(
            ["openssl", *args],
            cwd=certs,
            check=True,
            capture_output=True,
            timeout=10,
            text=True,
        ).stdout

    key_args = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes")
    for name in ("keeper-ca", "other-ca"):
        openssl(
            "req",
            "-x509",
            *key_args,
            "-days",
            "2",
            "-subj",
            f"/CN={name}   Root/O=Fixture CA",
            "-keyout",
            f"{name}.key",
            "-out",
            f"{name}.pem",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
    (certs / "server.ext").write_text(
        "subjectAltName=IP:127.0.0.1\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth\n"
        "authorityKeyIdentifier=keyid,issuer\n",
        encoding="ascii",
    )
    for name, issuer in (("server", "keeper-ca"), ("proxy", "other-ca")):
        openssl(
            "req",
            "-new",
            *key_args,
            "-subj",
            "/CN=127.0.0.1",
            "-keyout",
            f"{name}.key",
            "-out",
            f"{name}.csr",
        )
        openssl(
            "x509",
            "-req",
            "-in",
            f"{name}.csr",
            "-CA",
            f"{issuer}.pem",
            "-CAkey",
            f"{issuer}.key",
            "-CAcreateserial",
            "-out",
            f"{name}.pem",
            "-days",
            "2",
            "-extfile",
            "server.ext",
        )
    proxy_roots = certs / "proxy-roots"
    proxy_roots.mkdir()
    shutil.copy(certs / "other-ca.pem", proxy_roots / "other-ca.pem")
    openssl("rehash", "proxy-roots")
    hashed = certs / "hashed"
    hashed.mkdir()
    shutil.copy(certs / "keeper-ca.pem", hashed / "keeper-ca.pem")
    openssl("rehash", "hashed")
    raw = certs / "unhashed"
    raw.mkdir()
    shutil.copy(certs / "keeper-ca.pem", raw / "keeper-ca.pem")
    hash_name = openssl(
        "x509", "-in", "keeper-ca.pem", "-noout", "-subject_hash"
    ).strip()
    for name, filename in (("wrong_hash", "00000000.0"), ("gap", f"{hash_name}.1")):
        invalid = certs / name
        invalid.mkdir()
        shutil.copy(certs / "keeper-ca.pem", invalid / filename)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs / "server.pem", certs / "server.key")
    return context, certs


@pytest.mark.skipif(os.name != "posix", reason="requires a process-group SIGINT")
def test_watch_reports_tls_certificate_failure(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    keeper_certificate: tuple[ssl.SSLContext, Path],
) -> None:
    context, certs = keeper_certificate
    with replay(context) as (endpoint, _replies, requests):
        ws = endpoint.replace("https://", "wss://", 1)
        for index, cli in enumerate(cli_pair):
            assert_watch_disconnect(
                cli,
                tmp_path / str(index),
                ws,
                "SSLCertVerificationError",
                overrides={"SSL_CERT_FILE": str(certs / "other-ca.pem")},
            )
        assert not requests


def assert_watch_disconnect(
    cli: CLI,
    home: Path,
    ws: str,
    error_type: str,
    *,
    overrides: dict[str, str] | None = None,
) -> None:
    with subprocess.Popen(
        [*cli, "--json", "--ws-endpoint", ws, "watch", "balances", ADDR],
        env={
            "PATH": os.defpath,
            "HOME": str(home.parent),
            "SAI_HOME": str(home),
            "SAI_LOG": "1",
            **(overrides or {}),
        },
        cwd=home.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as proc:
        assert proc.stderr is not None
        try:
            assert select.select([proc.stderr], [], [], 8)[0]
            warning = proc.stderr.readline()
            os.killpg(proc.pid, signal.SIGINT)
            stdout, _ = proc.communicate(timeout=5)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate(timeout=5)
    assert proc.returncode == 1
    assert json.loads(stdout)["error"]["message"] == "Aborted"
    assert warning == (
        f"Disconnected ({error_type}); data may be stale; reconnecting in 2s\n".encode()
    )
    assert [
        (event["code"], event["errorType"])
        for event in read_events(home, cli)
        if event["event"] == "watch.disconnected"
    ] == [("STREAM_ERROR", error_type)]


@pytest.mark.parametrize(
    ("header", "values", "error_type"),
    [
        ("Connection", (), "InvalidUpgrade"),
        ("Connection", ("keep-alive",), "InvalidUpgrade"),
        ("Upgrade", (), "InvalidUpgrade"),
        ("Upgrade", ("websocket", "other/2"), "InvalidUpgrade"),
        ("Upgrade", ("websocket trailing",), "InvalidHeaderFormat"),
        ("Sec-WebSocket-Accept", (), "InvalidHeader"),
        ("Sec-WebSocket-Accept", ("first", "second"), "InvalidHeader"),
        ("Sec-WebSocket-Accept", ("wrong",), "InvalidHeaderValue"),
    ],
)
def test_watch_reports_rejected_upgrade(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    header: str,
    values: tuple[str, ...],
    error_type: str,
) -> None:
    def replace_header(
        _connection: ServerConnection,
        _request: UpgradeRequest,
        response: UpgradeResponse,
    ) -> None:
        del response.headers[header]
        for value in values:
            response.headers[header] = value

    with graphql_ws(
        lambda connection: connection.close(), process_response=replace_header
    ) as ws:
        for index, cli in enumerate(cli_pair):
            assert_watch_disconnect(cli, tmp_path / str(index), ws, error_type)


@pytest.mark.parametrize(
    "mode",
    [
        "file",
        "dir",
        "file_precedes_dir",
        "unhashed_dir",
        "wrong_hash",
        "gap",
        "default",
    ],
)
def test_keeper_tls_trust_matches_httpx(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    keeper_certificate: tuple[ssl.SSLContext, Path],
    mode: str,
) -> None:
    context, certs = keeper_certificate
    trust = {
        "file": {"SSL_CERT_FILE": str(certs / "keeper-ca.pem")},
        "dir": {"SSL_CERT_DIR": str(certs / "hashed")},
        "file_precedes_dir": {
            "SSL_CERT_FILE": str(certs / "other-ca.pem"),
            "SSL_CERT_DIR": str(certs / "hashed"),
        },
        "unhashed_dir": {"SSL_CERT_DIR": str(certs / "unhashed")},
        "wrong_hash": {"SSL_CERT_DIR": str(certs / "wrong_hash")},
        "gap": {"SSL_CERT_DIR": str(certs / "gap")},
        "default": {},
    }[mode]
    trusted = mode in {"file", "dir"}
    with replay(context) as (endpoint, replies, requests):
        observed = []
        for index, cli in enumerate(cli_pair):
            requests.clear()
            if trusted:
                replies.append(response("pending_exec"))
            result = invoke(
                cli,
                tmp_path / str(index),
                "--endpoint",
                endpoint,
                "--json",
                "intent",
                "status",
                ADDR,
                overrides=trust,
            )
            assert result.returncode == (0 if trusted else 4)
            assert not result.stderr
            assert not replies
            assert [request.body for request in requests] == (
                [wire(keeper.INTENTS, user=ADDR, key=None)] if trusted else []
            )
            if not trusted:
                assert json.loads(result.stdout)["error"]["code"] == "NOT_SENT"
            stdout = (
                public_intent_output(result.stdout, compact=True)
                if index == 0 and trusted
                else result.stdout
            )
            observed.append((stdout, list(requests)))
        assert observed[0] == observed[1]


@pytest.mark.parametrize(
    ("host", "exclusion", "bypass"),
    [
        ("127.0.0.1", "{host}:{port}", True),
        ("127.0.0.1", "{host}:{wrong_port}", False),
        ("localhost", "{host}:{port}", True),
        ("localhost", "{host}:{wrong_port}", False),
        ("localhost", "LOCALHOST", True),
        ("localhost", ".localhost", False),
        ("localhost", "https://{host}:{port}", False),
        ("localhost", "http://", True),
        ("localhost", "http://*:{port}", True),
        ("localhost", "http://*:{wrong_port}", False),
        ("127.0.0.1", "not-this-host, *", True),
        ("127.0.0.1", "", False),
    ],
)
def test_no_proxy_matches_keeper_destination(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    host: str,
    exclusion: str,
    bypass: bool,
) -> None:
    with (
        replay() as (keeper_url, keeper_replies, keeper_requests),
        replay() as (proxy, proxy_replies, proxy_requests),
    ):
        endpoint = keeper_url.replace("127.0.0.1", host)
        port = int(endpoint.rsplit(":", 1)[1].split("/", 1)[0])
        pattern = exclusion.format(host=host, port=port, wrong_port=port - 1)
        for index, cli in enumerate(cli_pair):
            recorded, unexpected = (
                (keeper_requests, proxy_requests)
                if bypass
                else (proxy_requests, keeper_requests)
            )
            recorded.clear()
            (keeper_replies if bypass else proxy_replies).append(response("absent"))
            result = invoke(
                cli,
                tmp_path / str(index),
                "--endpoint",
                endpoint,
                "--json",
                "intent",
                "status",
                ADDR,
                overrides={
                    "HTTP_PROXY": proxy.removesuffix("/query"),
                    "NO_PROXY": pattern,
                },
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {"userIntents": []}
            assert not unexpected
            assert not (keeper_replies or proxy_replies)
            assert [(request.path, request.body) for request in recorded] == [
                (
                    "/query" if bypass else endpoint,
                    wire(keeper.INTENTS, user=ADDR, key=None),
                )
            ]


@contextmanager
def tls_proxy(
    context: ssl.SSLContext, keeper_url: str
) -> Iterator[tuple[str, list[str]]]:
    authority = keeper_url.removeprefix("https://").split("/", 1)[0]
    host, port = authority.split(":", 1)
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_CONNECT(self) -> None:
            requests.append(self.path)
            if self.path != authority:
                self.send_error(400)
                return
            with socket.create_connection((host, int(port)), timeout=3) as upstream:
                self.send_response(200, "Connection established")
                self.end_headers()
                self.connection.settimeout(3)
                # The proxy terminates its own TLS, then forwards opaque bytes
                # for the separate TLS handshake and request with Keeper.
                with suppress(OSError):
                    while True:
                        pending = self.connection.pending()
                        readable = (
                            [self.connection]
                            if pending
                            else select.select([self.connection, upstream], [], [], 3)[
                                0
                            ]
                        )
                        if not readable:
                            return
                        for source in readable:
                            data = source.recv(16384)
                            if not data:
                                return
                            peer = (
                                upstream
                                if source is self.connection
                                else self.connection
                            )
                            peer.sendall(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(
            target=server.serve_forever, args=(0.01,), daemon=True
        )
        worker.start()
        try:
            yield f"https://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            worker.join()


@pytest.mark.parametrize(
    "mode",
    ["split_cas", "file_trusts_proxy", "dir_trusts_proxy", "keeper_ignores_proxy_dir"],
)
def test_https_proxy_and_keeper_use_httpx_trust_sources(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    keeper_certificate: tuple[ssl.SSLContext, Path],
    mode: str,
) -> None:
    keeper_context, certs = keeper_certificate
    proxy_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    same_ca = mode in {"file_trusts_proxy", "dir_trusts_proxy"}
    leaf = "server" if same_ca else "proxy"
    proxy_context.load_cert_chain(certs / f"{leaf}.pem", certs / f"{leaf}.key")
    trust = {
        "split_cas": {
            "SSL_CERT_FILE": str(certs / "keeper-ca.pem"),
            "SSL_CERT_DIR": str(certs / "proxy-roots"),
        },
        "file_trusts_proxy": {"SSL_CERT_FILE": str(certs / "keeper-ca.pem")},
        "dir_trusts_proxy": {"SSL_CERT_DIR": str(certs / "hashed")},
        "keeper_ignores_proxy_dir": {
            "SSL_CERT_FILE": str(certs / "other-ca.pem"),
            "SSL_CERT_DIR": str(certs / "hashed"),
        },
    }[mode]
    accepted = mode != "keeper_ignores_proxy_dir"
    with (
        replay(keeper_context) as (endpoint, replies, requests),
        tls_proxy(proxy_context, endpoint) as (proxy_url, connects),
    ):
        for index, cli in enumerate(cli_pair):
            connects.clear()
            requests.clear()
            if accepted:
                replies.append(response("absent"))
            result = invoke(
                cli,
                tmp_path / str(index),
                "--endpoint",
                endpoint,
                "--json",
                "intent",
                "status",
                ADDR,
                overrides={**trust, "HTTPS_PROXY": proxy_url},
            )
            assert result.returncode == (0 if accepted else 4), result.stdout
            assert not result.stderr
            assert connects == [endpoint.removeprefix("https://").split("/", 1)[0]]
            assert not replies
            assert [request.body for request in requests] == (
                [wire(keeper.INTENTS, user=ADDR, key=None)] if accepted else []
            )
            output = json.loads(result.stdout)
            if accepted:
                assert output == {"userIntents": []}
            else:
                assert output["error"]["code"] == "NOT_SENT"
