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


def test_cancelling_market_read_closes_keeper_connection(
    native: Path, tmp_path: Path
) -> None:
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


@pytest.mark.parametrize(
    ("market_id", "code"),
    [(1.5, "int_from_float"), ("3e0", "int_parsing"), ([], "int_type")],
)
def test_trade_integer_rejections(
    native: Path, tmp_path: Path, market_id: object, code: str
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
        assert f"[type={code}]" in result["content"][0]["text"]


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


@pytest.mark.parametrize(
    ("tool", "arguments", "field", "category"),
    [
        ("account_positions", {REJECTED_URL: True}, None, "extra_forbidden"),
        ("account_positions", {"extra": REJECTED_URL}, None, "extra_forbidden"),
        ("account_positions", {"addr": [REJECTED_URL]}, "addr", "string_type"),
        ("preview_close", {"extra": REJECTED_URL}, "trade_id", "missing"),
    ],
    ids=["extra-name", "extra-value", "wrong-type-array", "missing-required"],
)
def test_validation_omits_rejected_input(
    native: Path,
    tmp_path: Path,
    tool: str,
    arguments: dict[str, Any],
    field: str | None,
    category: str,
) -> None:
    with (
        loopback("normal") as endpoints,
        server(native, tmp_path / "home", endpoints) as stdio,
    ):
        result = stdio.call(2, tool, **arguments)
        assert result["isError"] is True
        assert "structuredContent" not in result
        assert len(result["content"]) == 1
        text = result["content"][0]["text"]
        assert f"[type={category}]" in text
        if field is not None:
            assert f"\n{field}\n" in text
        assert REJECTED_MARKER not in json.dumps(result)
        assert "input_value=" not in text
        assert "input_type=" not in text
        assert not endpoints.wire


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
