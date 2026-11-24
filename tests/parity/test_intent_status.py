"""Compare intent status through both CLIs against Keeper's recorded replays."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from sai_cli.core import keeper, log, storage

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


@dataclass(frozen=True)
class Reply:
    body: bytes = b""
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()
    body_delay: float = 0
    disconnect: bool = False


@dataclass(frozen=True)
class Request:
    path: str
    version: str
    headers: dict[str, str]
    body: bytes


@contextmanager
def replay() -> Iterator[tuple[str, deque[Reply], list[Request]]]:
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
        worker = threading.Thread(
            target=server.serve_forever, args=(0.01,), daemon=True
        )
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/query", replies, requests
        finally:
            server.shutdown()
            worker.join()


def invoke(cli: CLI, home: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*cli, *args],
        env={
            "PATH": os.defpath,
            "HOME": str(home.parent),
            "SAI_HOME": str(home),
            "SAI_LOG": "1",
            "SAI_UPDATE_AUTO": "0",
            "PYTHONUTF8": "1",
        },
        cwd=home.parent,
        capture_output=True,
        check=False,
        timeout=30,
    )


def read_events(home: Path) -> list[dict[str, Any]]:
    events = [
        json.loads(line)
        for path in sorted((home / "logs").glob("*.jsonl"))
        for line in path.read_bytes().splitlines()
    ]
    groups: dict[str, list[int]] = {}
    for event in events:
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


def wire(query: str, **variables: str | None) -> bytes:
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


def public_intent_errors(output: bytes, *, compact: bool) -> bytes:
    """Apply PR 69's error fields to stdout from the pinned Python CLI."""
    if not output:
        return output
    document = json.loads(output)
    if not isinstance(document, dict) or not isinstance(
        document.get("userIntents"), list
    ):
        return output
    safe_record = getattr(keeper, "safe_intent_record", None)
    for row in document["userIntents"]:
        if safe_record is not None:
            # PR 69 applies this projection in MCP. Use its error fields as
            # soon as the Python reference includes the helper.
            public = safe_record(row)
        elif (
            row.get("errorCode")
            or row.get("errorMsg")
            or row.get("lifecycleStep")
            in {
                "jwt_failed",
                "sign_reject",
                "sign_failed",
                "exec_req_fail",
                "exec_fail",
            }
        ):
            # The pinned reference exposes raw fields. Listed codes cover its
            # lifecycle replay fixtures. Production Rust owns the full allowlist.
            code = row.get("errorCode")
            known = {
                "admission_expired",
                "broadcast_rejected",
                "simulation_failed",
                "chain_execution_failed",
                "market_order_outcome_not_indexed",
                "market_order_expired",
                "market_order_fill_failed",
                "market_order_trading_paused",
                "market_order_market_closed",
                "market_order_trade_not_open",
            }
            public = {
                "errorCode": code if code in known else "KEEPER_ERROR",
                "errorMsg": "Keeper reported an intent error.",
            }
        else:
            continue
        for field in ("errorCode", "errorMsg"):
            if field in public:
                row[field] = public[field]
    return (json.dumps(document, indent=None if compact else 2) + "\n").encode()


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
                    stdout = public_intent_errors(stdout, compact=step.json_output)
                runs.append((stdout, result.stderr, list(requests), state))
            observed.append((runs, read_events(home)))
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
        (
            (
                {
                    "errors": [
                        {"message": "Keeper failure", "extensions": {"code": "FIXTURE"}}
                    ]
                },
            ),
            "unknown",
            "KEEPER_ERROR",
        ),
        (({"errors": [{"message": "denied"}], "data": {}},), "unknown", "KEEPER_ERROR"),
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
            outputs.append((result.stdout, read_events(home)))
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
        observed.append((result.stdout, read_events(home)))
    assert observed[0] == observed[1]


@pytest.mark.parametrize("json_output", [True, False])
@pytest.mark.parametrize("known_code", [True, False])
def test_public_intent_status_scrubs_keeper_message(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
    known_code: bool,
) -> None:
    token = "synthetic-stored-session"
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
            }
            requests.clear()
            replies.append(response({"data": {"userIntents": [row]}}))
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
                # Until the reference CLI uses the projection, compare its
                # public error fields. The Rust side is always checked raw.
                output = public_intent_errors(output, compact=json_output)
            assert token.encode() not in output
            assert private.encode() not in output
            public = json.loads(output)["userIntents"][0]
            assert public["errorCode"] == expected
            assert public["errorMsg"] == "Keeper reported an intent error."
