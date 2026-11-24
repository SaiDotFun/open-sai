"""Regression tests against a real native MCP subprocess."""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from scenarios import Endpoints, child_env, loopback, seed_home

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
    executable: Path, home: Path, endpoints: Endpoints, *, seed: str = "empty"
) -> Iterator[Stdio]:
    seed_home(home, endpoints, seed)
    env = {**child_env(home, endpoints), "SAI_LOG": "0"}
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
