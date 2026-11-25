"""Regression tests against a real native MCP subprocess."""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp_cases import REJECTED_AUTH_ADDRESS, TRADE_TOOLS
from scenarios import ADDRESS, INTENT, Endpoints, child_env, loopback, seed_home

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Uses POSIX stdio pipes")


@pytest.fixture
def native() -> Path:
    executable = shutil.which(os.getenv("SAI_PARITY_BIN", ""))
    if not executable:
        pytest.skip("SAI_PARITY_BIN must name the native candidate")
    return Path(executable).absolute()


class Stdio:
    def __init__(self, proc: subprocess.Popen[bytes]):
        self.proc = proc
        self.pending = bytearray()

    def write(self, raw: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(raw + b"\n")
        self.proc.stdin.flush()

    def send(self, method: str, *, id_: int | None = None, **params: Any) -> None:
        request: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if id_ is not None:
            request["id"] = id_
        if params:
            request["params"] = params
        self.write(json.dumps(request).encode())

    def receive(self, timeout: float = 3) -> dict[str, Any]:
        assert self.proc.stdout is not None
        deadline = time.monotonic() + timeout
        while b"\n" not in self.pending:
            ready, _, _ = select.select(
                [self.proc.stdout], [], [], max(0, deadline - time.monotonic())
            )
            assert ready, "MCP response timed out"
            data = os.read(self.proc.stdout.fileno(), 65536)
            assert data, "MCP closed stdout"
            self.pending.extend(data)
        end = self.pending.index(b"\n")
        frame = json.loads(self.pending[:end])
        del self.pending[: end + 1]
        assert frame["jsonrpc"] == "2.0"
        return frame

    def call(self, id_: int, name: str, **arguments: Any) -> dict[str, Any]:
        self.send("tools/call", id_=id_, name=name, arguments=arguments)
        response = self.receive()
        assert response["id"] == id_
        return response["result"]


@contextmanager
def server(
    executable: Path,
    home: Path,
    endpoints: Endpoints,
    *,
    seed: str = "empty",
    logs: bool = False,
) -> Iterator[Stdio]:
    seed_home(home, endpoints, seed)
    env = {**child_env(home, endpoints), "SAI_LOG": "1" if logs else "0"}
    with subprocess.Popen(
        [executable, "mcp", "serve"],
        cwd=home,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        try:
            stdio = Stdio(child)
            stdio.send(
                "initialize",
                id_=1,
                protocolVersion="2025-11-25",
                capabilities={},
                clientInfo={"name": "sai-regression", "version": "1"},
            )
            assert stdio.receive()["id"] == 1
            stdio.send("notifications/initialized")
            yield stdio
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)


def test_sigint_exits_with_stdin_open(native: Path, tmp_path: Path) -> None:
    with (
        loopback("normal") as endpoints,
        server(native, tmp_path / "home", endpoints) as stdio,
    ):
        stdio.send("ping", id_=2)
        assert stdio.receive() == {"jsonrpc": "2.0", "id": 2, "result": {}}
        stdio.proc.send_signal(signal.SIGINT)
        assert stdio.proc.wait(timeout=3) == 0
        assert stdio.proc.stdout is not None
        assert stdio.proc.stdout.read() == b""


def test_ping_during_exclusive_ledger_lock(native: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    with (
        loopback("normal") as endpoints,
        server(native, home, endpoints, seed="ledger", logs=True) as stdio,
        closing(sqlite3.connect(home / "sai/trader/intents.sqlite3")) as database,
    ):
        database.execute("BEGIN EXCLUSIVE")
        try:
            stdio.send(
                "tools/call", id_=2, name="intent_local", arguments={"addr": ADDRESS}
            )
            deadline = time.monotonic() + 3
            while not any(
                '"event":"mcp.tool_call"' in file.read_text()
                for file in (home / "sai/logs").glob("mcp-*.jsonl")
            ):
                assert time.monotonic() < deadline, "intent_local did not start"
                time.sleep(0.01)
            stdio.send("ping", id_=3)
            assert stdio.receive() == {"jsonrpc": "2.0", "id": 3, "result": {}}
        finally:
            database.rollback()
        response = stdio.receive()
        assert response["id"] == 2
        assert json.loads(response["result"]["content"][0]["text"])["intents"]


def test_cancelling_read_closes_keeper_connection(native: Path, tmp_path: Path) -> None:
    with (
        loopback("mcp-cancel") as endpoints,
        server(native, tmp_path / "home", endpoints) as stdio,
    ):
        stdio.send("tools/call", id_=2, name="market_list", arguments={})
        assert endpoints.interrupt_ready.wait(3), "Keeper did not receive market_list"
        stdio.send("notifications/cancelled", requestId=2, reason="fixture")
        assert stdio.receive() == {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": 0, "message": "Request cancelled"},
        }
        assert endpoints.interrupt_closed.wait(3), "Keeper connection remained open"
        stdio.send("ping", id_=3)
        assert stdio.receive() == {"jsonrpc": "2.0", "id": 3, "result": {}}


@pytest.mark.parametrize("interrupt", ["cancel", "eof"])
def test_interrupting_intent_wait_preserves_the_ledger(
    native: Path, tmp_path: Path, interrupt: str
) -> None:
    home = tmp_path / "home"
    with (
        loopback("mcp-cancel") as endpoints,
        server(native, home, endpoints, seed="ledger") as stdio,
        closing(sqlite3.connect(home / "sai/trader/intents.sqlite3")) as database,
    ):
        original = database.execute("SELECT * FROM intents").fetchall()
        assert len(original) == 1
        stdio.send("tools/call", id_=2, name="intent_wait", arguments={"key": INTENT})
        assert endpoints.interrupt_ready.wait(3), "Keeper did not receive intent_wait"
        if interrupt == "eof":
            assert stdio.proc.stdin is not None
            stdio.proc.stdin.close()
            assert stdio.proc.wait(timeout=1.5) == 0
            assert stdio.proc.stdout is not None
            assert stdio.proc.stdout.read() == b""
        else:
            stdio.send("notifications/cancelled", requestId=2, reason="fixture")
            assert stdio.receive(timeout=1.5) == {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": 0, "message": "Request cancelled"},
            }
        assert endpoints.interrupt_closed.wait(1.5), "Keeper connection remained open"
        assert database.execute("SELECT * FROM intents").fetchall() == original
        assert [request.label for request in endpoints.wire] == ["waitUserIntents"]


def test_auth_status_omits_a_rejected_address(native: Path, tmp_path: Path) -> None:
    with (
        loopback("normal") as endpoints,
        server(native, tmp_path / "home", endpoints, seed="session") as stdio,
    ):
        result = stdio.call(2, "auth_status", addr=REJECTED_AUTH_ADDRESS)
        assert result["isError"] is False
        assert "fixture-mcp-addr" not in json.dumps(result)
        body = json.loads(result["content"][0]["text"])
        assert body["code"] == "SESSION_UNAVAILABLE"
        assert body["error"].startswith("Requested account is not connected;")
        assert not endpoints.wire


def test_short_intent_wait_polls_while_eight_long_waits_are_pending(
    native: Path, tmp_path: Path
) -> None:
    with (
        loopback("interrupt-http") as endpoints,
        server(native, tmp_path / "home", endpoints, seed="ledger") as stdio,
    ):
        try:
            for id_ in range(2, 10):
                stdio.send(
                    "tools/call",
                    id_=id_,
                    name="intent_wait",
                    arguments={"addr": ADDRESS, "key": INTENT, "timeout": 10},
                )
            deadline = time.monotonic() + 3
            while len(endpoints.wire) < 8:
                assert time.monotonic() < deadline, "Keeper did not receive eight waits"
                time.sleep(0.01)
            stdio.send(
                "tools/call",
                id_=10,
                name="intent_wait",
                arguments={"addr": ADDRESS, "key": INTENT, "timeout": 0.25},
            )
            response = stdio.receive(timeout=1.5)
            assert response["id"] == 10
            body = json.loads(response["result"]["content"][0]["text"])
            assert body["code"] == "OUTCOME_UNKNOWN"
            assert body["intentKey"] == INTENT
            assert body["addr"] == ADDRESS
            assert [request.label for request in endpoints.wire] == [
                "waitUserIntents"
            ] * 9
        finally:
            endpoints.interrupt_closed.set()


def test_cancellation_is_admitted_while_request_slots_are_full(
    native: Path, tmp_path: Path
) -> None:
    with (
        loopback("mcp-cancel") as endpoints,
        server(native, tmp_path / "home", endpoints) as stdio,
    ):
        stdio.send("tools/call", id_=2, name="market_list", arguments={})
        assert endpoints.interrupt_ready.wait(3), "Keeper did not receive market_list"
        for id_ in range(3, 66):
            stdio.send("tools/call", id_=id_, name="market_list", arguments={})
        stdio.send("ping", id_=66)
        assert stdio.receive() == {
            "jsonrpc": "2.0",
            "id": 66,
            "error": {"code": -32000, "message": "Too many pending requests"},
        }
        stdio.send("notifications/cancelled", requestId=2)
        assert stdio.receive() == {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": 0, "message": "Request cancelled"},
        }


@pytest.mark.parametrize("use_python", [False, True], ids=["native", "python"])
def test_invalid_request_frames(native: Path, tmp_path: Path, use_python: bool) -> None:
    executable = native
    if use_python:
        reference = shutil.which(os.getenv("SAI_MCP_PARITY_PYTHON", ""))
        if not reference:
            pytest.skip("Requires the pinned Python MCP reference")
        executable = Path(reference).absolute()
    with (
        loopback("normal") as endpoints,
        server(executable, tmp_path / "home", endpoints) as stdio,
    ):
        stdio.send("fixture/unknown", id_=2)
        stdio.send("tools/call", id_=3, name="market_list", arguments=[])
        for id_ in (2, 3):
            assert stdio.receive() == {
                "jsonrpc": "2.0",
                "id": id_,
                "error": {
                    "code": -32602,
                    "message": "Invalid request parameters",
                    "data": "",
                },
            }
        stdio.write(b"{broken")
        assert stdio.receive() == {
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {
                "level": "error",
                "logger": "mcp.server.exception_handler",
                "data": "Internal Server Error",
            },
        }
        assert stdio.proc.stderr is not None
        assert select.select([stdio.proc.stderr], [], [], 3)[0]
        assert stdio.proc.stderr.readline()
        stdio.send("ping", id_=4)
        assert stdio.receive() == {"jsonrpc": "2.0", "id": 4, "result": {}}


@pytest.mark.parametrize("market_id", ["  -3.00 ", "01_1", "+02", 3.0, True])
def test_trade_integer_coercion(
    native: Path, tmp_path: Path, market_id: object
) -> None:
    with (
        loopback("normal") as endpoints,
        server(native, tmp_path / "home", endpoints, seed="session") as stdio,
    ):
        result = stdio.call(
            2,
            "trade_open",
            market_id=market_id,
            side="long",
            order_type="market",
            amount="10",
            leverage="2",
            slippage="1",
            preview_digest="fixture",
        )
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"])["code"] == "NOT_IMPLEMENTED"
        assert not endpoints.wire


def validation_text(tool: str, *issues: tuple[str | None, str, str]) -> str:
    label = "validation error" if len(issues) == 1 else "validation errors"
    lines = [f"Error executing tool {tool}: {len(issues)} {label} for {tool}Arguments"]
    for field, detail, category in issues:
        if field is not None:
            lines.append(field)
        lines.extend(
            (
                f"  {detail} [type={category}]",
                f"    For further information visit https://errors.pydantic.dev/2.14/v/{category}",
            )
        )
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("market_id", "detail", "code"),
    [
        (
            1.5,
            "Input should be a valid integer, got a number with a fractional part",
            "int_from_float",
        ),
        (
            "3e0",
            "Input should be a valid integer, unable to parse string as an integer",
            "int_parsing",
        ),
        ([], "Input should be a valid integer", "int_type"),
    ],
)
def test_trade_integer_rejections(
    native: Path, tmp_path: Path, market_id: object, detail: str, code: str
) -> None:
    with (
        loopback("normal") as endpoints,
        server(native, tmp_path / "home", endpoints, seed="session") as stdio,
    ):
        result = stdio.call(
            2,
            "trade_open",
            market_id=market_id,
            side="long",
            order_type="market",
            amount="10",
            leverage="2",
            slippage="1",
            preview_digest="fixture",
        )
        assert result["isError"] is True
        assert result["content"] == [
            {
                "type": "text",
                "text": validation_text("trade_open", ("market_id", detail, code)),
            }
        ]
        assert "structuredContent" not in result
        assert not endpoints.wire


@pytest.mark.parametrize("offline", ["true", "YES", 1.0])
def test_audit_boolean_coercion(native: Path, tmp_path: Path, offline: object) -> None:
    with (
        loopback("normal") as endpoints,
        server(native, tmp_path / "home", endpoints) as stdio,
    ):
        result = stdio.call(2, "logs_intent", key="fixture", offline=offline)
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"])["code"] == "NOT_IMPLEMENTED"
        refused = stdio.call(3, "logs_intent", key="fixture", offline=" True ")
        assert refused["isError"] is True
        assert refused["content"][0]["text"] == "Argument 'offline' must be a boolean."


@pytest.mark.parametrize("symlink", [False, True], ids=["binary", "symlink"])
@pytest.mark.parametrize("banner", ["sai", "sai, version"], ids=["native", "python"])
def test_info_observes_launcher_replacement(
    native: Path, tmp_path: Path, symlink: bool, banner: str
) -> None:
    launcher = tmp_path / "sai"
    if symlink:
        launcher.symlink_to(native)
    else:
        shutil.copy2(native, launcher)
    with (
        loopback("normal") as endpoints,
        server(launcher, tmp_path / "home", endpoints) as stdio,
    ):
        before = json.loads(stdio.call(2, "mcp_info")["content"][0]["text"])
        assert before["version"] == before["installedVersion"]
        assert before["restartNeeded"] is False

        replacement = tmp_path / "replacement"
        replacement.write_text(f"#!/bin/sh\nprintf '{banner} 99.1.2\\n'\n")
        replacement.chmod(0o700)
        replacement.replace(launcher)

        after = json.loads(stdio.call(3, "mcp_info")["content"][0]["text"])
        assert after["version"] == before["version"]
        assert after["installedVersion"] == "99.1.2"
        assert after["restartNeeded"] is True


REJECTED_MARKER = "fixture-mcp-key"
REJECTED_URL = f"//127.0.0.1:9/?api_key={REJECTED_MARKER}"


EXTRA_ARGUMENT = (None, "Extra inputs are not permitted", "extra_forbidden")
INVALID_ADDRESS = ("addr", "Input should be a valid string", "string_type")


@pytest.mark.parametrize(
    ("tool", "arguments", "issues"),
    [
        ("account_positions", {REJECTED_URL: True}, (EXTRA_ARGUMENT,)),
        ("account_positions", {"extra": REJECTED_URL}, (EXTRA_ARGUMENT,)),
        ("account_positions", {"addr": [REJECTED_URL]}, (INVALID_ADDRESS,)),
        ("auth_status", {"addr": [REJECTED_AUTH_ADDRESS]}, (INVALID_ADDRESS,)),
        ("auth_status", {"address": REJECTED_AUTH_ADDRESS}, (EXTRA_ARGUMENT,)),
        ("auth_list", {"unexpected": REJECTED_AUTH_ADDRESS}, (EXTRA_ARGUMENT,)),
        (
            "account_positions",
            {"addr": {"nested": [{"value": REJECTED_URL}]}},
            (INVALID_ADDRESS,),
        ),
        (
            "preview_close",
            {"extra": REJECTED_URL},
            (("trade_id", "Field required", "missing"), EXTRA_ARGUMENT),
        ),
        (
            "preview_close",
            {"trade_id": f"{REJECTED_MARKER}-" + "z" * 1024},
            (
                (
                    "trade_id",
                    "Input should be a valid integer, unable to parse string as an integer",
                    "int_parsing",
                ),
            ),
        ),
    ],
    ids=[
        "extra-name",
        "extra-value",
        "wrong-type-array",
        "auth-status-array-addr",
        "auth-status-misspelled-address",
        "auth-list-unexpected-argument",
        "nested-object",
        "missing-required",
        "long-string",
    ],
)
def test_validation_omits_rejected_input(
    native: Path,
    tmp_path: Path,
    tool: str,
    arguments: dict[str, Any],
    issues: tuple[tuple[str | None, str, str], ...],
) -> None:
    with (
        loopback("normal") as endpoints,
        server(native, tmp_path / "home", endpoints) as stdio,
    ):
        result = stdio.call(2, tool, **arguments)
        assert result["isError"] is True
        assert "structuredContent" not in result
        assert result["content"] == [
            {"type": "text", "text": validation_text(tool, *issues)}
        ]
        assert not endpoints.wire


def test_rejected_arguments_are_logged_as_metadata(
    native: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    credential = "fixture-opaque-preview-credential"
    numeric = 21873.625
    unknown_name = "fixture-private-field-name"
    unknown_value = "fixture-unknown-argument-value"
    with (
        loopback("normal") as endpoints,
        server(native, home, endpoints, logs=True) as stdio,
    ):
        first = stdio.call(
            2,
            "preview_close",
            trade_id=credential,
            addr=ADDRESS,
            **{unknown_name: {"nested": [unknown_value]}},
        )
        assert first["isError"] is True
        refused = stdio.call(3, "preview_close", trade_id=numeric)
        assert refused["isError"] is True
        signed_out = stdio.call(4, "trade_close", trade_id=credential)
        assert signed_out["isError"] is False
        assert json.loads(signed_out["content"][0]["text"])["code"] == "SIGNED_OUT"
        assert not endpoints.wire

        assert stdio.proc.stdin is not None
        stdio.proc.stdin.close()
        assert stdio.proc.wait(timeout=3) == 0
        assert stdio.proc.stderr is not None
        stderr = stdio.proc.stderr.read().decode()

    logs = (home / "sai" / "logs").glob("*.jsonl")
    log_lines = [line for log in logs for line in log.read_text().splitlines()]
    for rejected in (credential, str(numeric), unknown_name, unknown_value):
        assert rejected not in stderr
        assert all(rejected not in line for line in log_lines)
    events = [json.loads(line) for line in log_lines]
    calls = [event for event in events if event["event"] == "mcp.tool_call"]
    assert [call["tool"] for call in calls] == [
        "preview_close",
        "preview_close",
        "trade_close",
    ]
    string_metadata = {"type": "string", "length": len(credential)}
    assert calls[0]["arguments"] == {"trade_id": string_metadata, "addr": ADDRESS}
    assert calls[0]["unknownArguments"] == 1
    assert calls[0]["unknownArgumentMetadata"] == [{"type": "object", "length": 1}]
    assert calls[1]["arguments"] == {"trade_id": {"type": "number", "length": 1}}
    assert calls[2]["arguments"] == {"trade_id": string_metadata}


def test_session_lookup_refusals_omit_submitted_addresses(
    native: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    credential = "fixture-opaque-session-lookup-credential"
    error = (
        "Requested account is not connected; run `sai auth list` to see connected "
        "accounts, or sign in with `sai auth login NAME` or `sai auth import`, in a terminal."
    )
    with (
        loopback("normal") as endpoints,
        server(native, home, endpoints, seed="session", logs=True) as stdio,
    ):
        for id_, tool in enumerate(TRADE_TOOLS, 2):
            result = stdio.call(id_, tool, addr=credential)
            assert result["isError"] is False
            assert credential not in json.dumps(result)
            assert json.loads(result["content"][0]["text"]) == {
                "error": error,
                "code": "SESSION_UNAVAILABLE",
            }
        assert not endpoints.wire

        assert stdio.proc.stdin is not None
        stdio.proc.stdin.close()
        assert stdio.proc.wait(timeout=3) == 0
        assert stdio.proc.stderr is not None
        assert credential.encode() not in stdio.proc.stderr.read()

    logs = (home / "sai" / "logs").glob("*.jsonl")
    log_lines = [line for log in logs for line in log.read_text().splitlines()]
    assert all(credential not in line for line in log_lines)
    events = [json.loads(line) for line in log_lines]
    results = [event for event in events if event["event"] == "mcp.tool_result"]
    assert len(results) == len(TRADE_TOOLS)
    assert {result["tool"] for result in results} == set(TRADE_TOOLS)
    assert all(result["error"] == error for result in results)
    assert all(result["code"] == "SESSION_UNAVAILABLE" for result in results)


def test_local_ledger_read_while_keeper_reconciliation_is_busy(
    native: Path, tmp_path: Path
) -> None:
    with (
        loopback("interrupt-http") as endpoints,
        server(native, tmp_path / "home", endpoints, seed="ledger") as stdio,
    ):
        try:
            for id_ in range(2, 10):
                stdio.send(
                    "tools/call",
                    id_=id_,
                    name="intent_status",
                    arguments={"addr": ADDRESS, "key": INTENT},
                )
            deadline = time.monotonic() + 5
            while sum(request.label == "userIntents" for request in endpoints.wire) < 8:
                assert time.monotonic() < deadline, (
                    "Keeper did not receive all eight requests"
                )
                time.sleep(0.01)
            result = stdio.call(10, "intent_local", addr=ADDRESS)
            assert json.loads(result["content"][0]["text"])["intents"]
        finally:
            endpoints.interrupt_closed.set()


def test_sigint_flushes_command_end_during_locked_reconciliation(
    native: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    with (
        loopback("normal") as endpoints,
        server(native, home, endpoints, seed="ledger", logs=True) as stdio,
        closing(sqlite3.connect(home / "sai/trader/intents.sqlite3")) as database,
    ):
        database.execute("BEGIN EXCLUSIVE")
        stdio.send(
            "tools/call",
            id_=2,
            name="intent_status",
            arguments={"addr": ADDRESS, "key": INTENT},
        )
        deadline = time.monotonic() + 5
        while not any(
            '"event":"intent.keeper"' in file.read_text()
            for file in (home / "sai/logs").glob("mcp-*.jsonl")
        ):
            assert time.monotonic() < deadline, "intent_status did not reach the ledger"
            time.sleep(0.01)
        stdio.proc.send_signal(signal.SIGINT)
        assert stdio.proc.wait(timeout=3) == 0
        events = [
            json.loads(line)
            for file in sorted((home / "sai/logs").glob("mcp-*.jsonl"))
            for line in file.read_text().splitlines()
        ]
        assert events[-1]["event"] == "command.end"
        assert events[-1]["exitCode"] == 0


@pytest.mark.parametrize(
    "envelope",
    [b'{"jsonrpc":"2.0","id":2}', b'{"jsonrpc":"1.0","id":2,"method":"ping"}'],
    ids=["missing-method", "wrong-version"],
)
def test_invalid_envelope_reports_diagnostic(
    native: Path, tmp_path: Path, envelope: bytes
) -> None:
    with (
        loopback("normal") as endpoints,
        server(native, tmp_path / "home", endpoints) as stdio,
    ):
        stdio.write(envelope)
        assert stdio.receive() == {
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {
                "level": "error",
                "logger": "mcp.server.exception_handler",
                "data": "Internal Server Error",
            },
        }
        stdio.send("ping", id_=3)
        assert stdio.receive() == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_oversized_partial_frame_is_diagnosed_before_newline(
    native: Path, tmp_path: Path
) -> None:
    with (
        loopback("normal") as endpoints,
        server(native, tmp_path / "home", endpoints) as stdio,
    ):
        assert stdio.proc.stdin is not None
        stdio.proc.stdin.write(b" " * (16 * 1024 * 1024 + 1))
        stdio.proc.stdin.flush()
        diagnostic = stdio.receive()
        assert diagnostic["method"] == "notifications/message"
        assert diagnostic["params"] == {
            "level": "error",
            "logger": "mcp.server.exception_handler",
            "data": "Internal Server Error",
        }
        stdio.write(b"")
        stdio.send("ping", id_=3)
        assert stdio.receive() == {"jsonrpc": "2.0", "id": 3, "result": {}}


@pytest.mark.skipif(sys.platform != "linux", reason="Sets Linux pipe size")
def test_paused_stdout_applies_backpressure(native: Path, tmp_path: Path) -> None:
    import fcntl

    with (
        loopback("normal") as endpoints,
        server(native, tmp_path / "home", endpoints) as stdio,
    ):
        assert stdio.proc.stdin is not None
        assert stdio.proc.stdout is not None
        stdin = stdio.proc.stdin.fileno()
        fcntl.fcntl(stdin, fcntl.F_SETPIPE_SZ, 4096)
        fcntl.fcntl(stdio.proc.stdout.fileno(), fcntl.F_SETPIPE_SZ, 4096)
        stdio.send("tools/list", id_=2)
        assert select.select([stdio.proc.stdout], [], [], 3)[0]
        os.set_blocking(stdin, False)
        pending = memoryview(
            b"".join(
                json.dumps(
                    {"jsonrpc": "2.0", "id": id_, "method": "tools/list"}
                ).encode()
                + b"\n"
                for id_ in range(3, 1500)
            )
        )
        while pending:
            if not select.select([], [stdin], [], 1)[1]:
                break
            pending = pending[os.write(stdin, pending[:4096]) :]
        assert pending, "Server consumed all requests with the output pipe blocked"
        assert stdio.receive()["result"]["tools"]


def test_stdout_failure_ends_session_with_stdin_open(
    native: Path, tmp_path: Path
) -> None:
    with (
        loopback("normal") as endpoints,
        server(native, tmp_path / "home", endpoints) as stdio,
    ):
        assert stdio.proc.stdout is not None
        stdio.proc.stdout.close()
        stdio.send("ping", id_=2)
        stdio.proc.wait(timeout=3)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("account_overview", {"addr": ADDRESS}),
        ("logs_intent", {"key": INTENT, "offline": True}),
        ("logs_intent", {"key": INTENT, "offline": False}),
    ],
)
def test_deferred_read_tools_accept_valid_arguments_without_requests(
    native: Path, tmp_path: Path, tool: str, arguments: dict[str, Any]
) -> None:
    with (
        loopback("normal") as endpoints,
        server(native, tmp_path / "home", endpoints, seed="session") as stdio,
    ):
        result = stdio.call(2, tool, **arguments)
        assert result["isError"] is False
        text = result["content"][0]["text"]
        assert result["structuredContent"] == {"result": text}
        assert json.loads(text)["code"] == "NOT_IMPLEMENTED"
        assert not endpoints.wire


def test_auth_switch_keeps_rejected_arguments_private(
    native: Path, tmp_path: Path
) -> None:
    marker = "synthetic-unregistered-mcp-auth-credential"
    home = tmp_path / "home"
    with (
        loopback("normal") as endpoints,
        server(native, home, endpoints, seed="accounts", logs=True) as stdio,
    ):
        result = stdio.call(2, "auth_use", addr=marker)
        assert json.loads(result["content"][0]["text"])["error"].startswith(
            "Requested account is not connected;"
        )
        rejected = stdio.call(3, "auth_use", addr={"value": marker})
        assert rejected["isError"] is True
        assert rejected["content"][0]["text"] == "Argument 'addr' must be a string."
        assert not endpoints.wire
        assert stdio.proc.stdin is not None and stdio.proc.stdout is not None
        stdio.proc.stdin.close()
        assert stdio.proc.wait(timeout=3) == 0
        assert stdio.proc.stdout.read() == b""
        assert stdio.proc.stderr is not None
        assert marker.encode() not in stdio.proc.stderr.read()
    logs = "".join(path.read_text() for path in (home / "sai/logs").glob("mcp-*.jsonl"))
    assert "mcp.tool_call" in logs
    assert "session.active" not in logs
    assert marker not in json.dumps([result, rejected]) + logs


def test_auth_switch_succeeds_after_legacy_cleanup_failure(
    native: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    with (
        loopback("normal") as endpoints,
        server(native, home, endpoints, seed="accounts") as stdio,
    ):
        legacy = home / "sai/trader/session.json"
        legacy.mkdir()
        switched = json.loads(
            stdio.call(2, "auth_use", addr=ADDRESS)["content"][0]["text"]
        )
        assert [row["addr"] for row in switched["accounts"] if row["active"]] == [
            ADDRESS
        ]
        status = json.loads(stdio.call(3, "auth_status")["content"][0]["text"])
        assert status["addr"] == ADDRESS
        assert legacy.is_dir()
        assert not endpoints.wire
        assert stdio.proc.stderr is not None
        assert select.select([stdio.proc.stderr], [], [], 3)[0]
        assert (
            b"Could not remove legacy session file trader/session.json"
            in stdio.proc.stderr.readline()
        )


def test_intent_wait_reports_unknown_when_account_lookup_is_locked(
    native: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    with (
        loopback("normal") as endpoints,
        server(native, home, endpoints, seed="ledger") as stdio,
        closing(sqlite3.connect(home / "sai/trader/intents.sqlite3")) as database,
    ):
        database.execute("BEGIN EXCLUSIVE")
        result = stdio.call(2, "intent_wait", key=INTENT)
        report = json.loads(result["content"][0]["text"])
        assert report["outcome"] == "unknown"
        assert report["code"] == "OUTCOME_UNKNOWN"
        assert report["addr"] is None
        assert "addrSource" not in report
        assert not endpoints.wire
