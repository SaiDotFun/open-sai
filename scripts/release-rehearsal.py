"""Rehearse a sai-cli release wheel against a published install."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from collections import deque
from collections.abc import Callable, Generator
from contextlib import ExitStack, closing, contextmanager, nullcontext, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, NamedTuple, TypeVar

SCRIPTS = Path(__file__).resolve().parent
TESTS = SCRIPTS.parent / "cli" / "sai_cli" / "tests"
sys.path[:0] = [str(SCRIPTS), str(TESTS)]
import install as release_install
from loopback_keeper import KeeperHandler, keeper_server

REPO = release_install.REPO
VERSION = r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}"
CLIENTS = ("claude", "codex")
SYNTHETIC_TOKEN = "release-rehearsal-fixture-only"
CHAIN = "cataclysm-1"
INTENT_KEY = re.compile(r"\d{8}T\d{9}Z_[0-9A-HJKMNP-TV-Z]{16}")
TRADE = (
    "trade",
    "open",
    "--market",
    "0",
    "--side",
    "long",
    "--type",
    "market",
    "--amount",
    "10",
    "--leverage",
    "2",
    "--slippage",
    "1",
)
MCP_REQUESTS = (
    {
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "release-rehearsal", "version": "1"},
        },
    },
    {"method": "notifications/initialized"},
    {"id": 2, "method": "tools/list"},
)
# Use the WebSocket package already installed in Sai's tool environment.
WEBSOCKET_SERVER = """
import sys
from websockets.sync.server import serve

def handshake(connection):
    connection.recv()
    connection.send('{"type":"connection_ack"}')

with serve(handshake, "127.0.0.1", 0, subprotocols=["graphql-transport-ws"]) as server:
    from threading import Thread
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"ws://127.0.0.1:{server.socket.getsockname()[1]}/query", flush=True)
    sys.stdin.read()
    server.shutdown()
    thread.join()
"""

# The reserved proxy port never listens. Probe with an unresolvable test name.
CHECK_NETWORK = """
import sys
from urllib.error import URLError
from urllib.request import urlopen

with urlopen(sys.argv[1], timeout=3) as response:
    if response.status != 200:
        sys.exit(1)
for scheme in ("http", "https"):
    try:
        with urlopen(f"{scheme}://release-rehearsal.invalid/", timeout=3):
            sys.exit(1)
    except URLError as error:
        if not isinstance(error.reason, ConnectionRefusedError):
            sys.exit(1)
"""
ISOLATED_STAGE = """
import runpy, sys
sys.exit(runpy.run_path(sys.argv[1])["isolated_stage"](*sys.argv[2:]))
"""
CHECK_NAMESPACE = """
import socket, sys
sys.exit([name for _, name in socket.if_nameindex()] != ["lo"])
"""


class RehearsalError(RuntimeError):
    pass


class RehearsalInterrupted(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


class Cancellation:
    def __init__(self) -> None:
        self.signum: int | None = None

    def receive(self, signum: int, _frame: object) -> None:
        # A Python handler can run before a context manager enters __exit__.
        # Record cancellation here and raise only at explicit checkpoints.
        if self.signum is None:
            self.signum = signum

    def check(self) -> None:
        if self.signum is not None:
            raise RehearsalInterrupted(self.signum)


CANCELLATION = Cancellation()
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
CANCEL_INTERVAL = 0.1
T = TypeVar("T")


@contextmanager
def cancel_on_signal() -> Generator[None, None, None]:
    previous = {}
    try:
        for sig in STOP_SIGNALS:
            previous[sig] = signal.signal(sig, CANCELLATION.receive)
        yield
    finally:
        try:
            for sig, handler in previous.items():
                if handler is not None:
                    signal.signal(sig, handler)
            CANCELLATION.check()
        finally:
            CANCELLATION.signum = None


@contextmanager
def cleanup_stack() -> Generator[ExitStack, None, None]:
    cleanup = ExitStack()
    try:
        yield cleanup
    finally:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
        try:
            cleanup.close()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def wait_with_cancellation(operation: Callable[[float], T], timeout: float | None) -> T:
    deadline = time.monotonic() + timeout if timeout is not None else float("inf")
    while True:
        CANCELLATION.check()
        remaining = deadline - time.monotonic()
        try:
            return operation(max(0, min(CANCEL_INTERVAL, remaining)))
        except subprocess.TimeoutExpired as exc:
            if timeout is not None and remaining <= CANCEL_INTERVAL:
                exc.timeout = timeout
                raise


def communicate(
    process: subprocess.Popen[bytes],
    *,
    timeout: float,
) -> tuple[bytes, bytes]:
    return wait_with_cancellation(
        lambda interval: process.communicate(timeout=interval), timeout
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RehearsalError(message)


def resolve_baseline(
    api_base: str = release_install.API_BASE, web_base: str = release_install.WEB_BASE
) -> str:
    try:
        tag = release_install.find_latest(api_base, web_base)
    except release_install.LookupFailed as exc:
        raise RehearsalError(
            f"Could not resolve the latest published sai-cli release: {exc}. "
            "Pass --from VERSION to select a published baseline."
        ) from exc
    if tag is None:
        raise RehearsalError("No sai-cli release is published yet")
    return release_install.version_of(tag)


class ReplyReader:
    def __init__(self, stream: IO[bytes], capture: bytearray | None = None) -> None:
        self.fd = stream.fileno()
        self.pending = bytearray()
        self.capture = capture
        os.set_blocking(self.fd, False)

    def line(self, step: str, *, timeout: float = 10) -> str:
        deadline = time.monotonic() + timeout
        while (end := self.pending.find(b"\n")) < 0:
            CANCELLATION.check()
            left = deadline - time.monotonic()
            if left <= 0:
                raise RehearsalError(f"{step} timed out waiting for a complete reply")
            if not select.select([self.fd], [], [], min(CANCEL_INTERVAL, left))[0]:
                continue
            try:
                chunk = os.read(self.fd, 8192)
            except BlockingIOError:
                continue
            if not chunk:
                raise RehearsalError(f"{step} received no complete reply")
            if self.capture is not None:
                self.capture.extend(chunk)
            self.pending.extend(chunk)
        line = bytes(self.pending[:end])
        del self.pending[: end + 1]
        return line.decode("utf-8")


@contextmanager
def owned_process(
    command: tuple[str | Path, ...],
    cwd: Path,
    env: dict[str, str],
    *,
    stdin: IO[bytes] | int = subprocess.PIPE,
    stdout: int | None = subprocess.PIPE,
    stderr: int | IO[bytes] | None = subprocess.PIPE,
) -> Generator[subprocess.Popen[bytes], None, None]:
    CANCELLATION.check()
    with cleanup_stack() as cleanup:
        # Keep the group ID owned even when communicate() reaps the command.
        leader = cleanup.enter_context(
            subprocess.Popen(
                (sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(1)"),
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                process_group=0,
            )
        )
        try:
            process = cleanup.enter_context(
                subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    process_group=leader.pid,
                )
            )
        finally:
            cleanup.callback(stop_process_group, leader)
        CANCELLATION.check()
        yield process
    CANCELLATION.check()


def stop_process_group(leader: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(leader.pid, signal.SIGKILL)


def stop_adopted_children() -> None:
    # All owned command contexts have closed. Remaining children are orphans
    # adopted by the rehearsal, including helpers that created a new session.
    while True:
        try:
            exited, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if exited:
            continue
        pids = set()
        for children in Path("/proc/self/task").glob("*/children"):
            with suppress(FileNotFoundError):
                pids.update(map(int, children.read_text().split()))
        for pid in pids:
            # The PID remains owned until this process reaps the child.
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        if pids:
            os.waitpid(-1, 0)


@contextmanager
def reap_detached_children() -> Generator[None, None, None]:
    if sys.platform != "linux":
        yield
        return
    require(
        Path("/proc/thread-self/children").is_file(),
        "Linux descendant cleanup requires the procfs children list",
    )
    get_subreaper, set_subreaper = 37, 36  # linux/prctl.h
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]

    def set_option(option: int, value: int) -> None:
        if prctl(option, value, 0, 0, 0) == -1:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))

    previous = ctypes.c_int()
    set_option(get_subreaper, ctypes.addressof(previous))
    with cleanup_stack() as cleanup:
        set_option(set_subreaper, 1)
        cleanup.callback(set_option, set_subreaper, previous.value)
        cleanup.callback(stop_adopted_children)
        yield


def loopback_env(base: str, proxy_port: int) -> dict[str, str]:
    proxy = f"http://127.0.0.1:{proxy_port}"
    return {
        "SAI_ENDPOINT": f"{base}/query",
        "SAI_WS_ENDPOINT": f"ws://{base.removeprefix('http://')}/query",
        "SAI_LCD_ENDPOINT": base,
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "WS_PROXY": proxy,
        "WSS_PROXY": proxy,
        "ALL_PROXY": proxy,
        "NO_PROXY": "127.0.0.1",
        "UV_OFFLINE": "1",
    }


def namespace_prefix(cwd: Path, env: dict[str, str]) -> tuple[str, ...] | None:
    unshare = shutil.which("unshare")
    ip = shutil.which("ip")
    if not (sys.platform == "linux" and unshare and ip):
        return None
    # A PID namespace makes Linux kill every member when its init exits,
    # including children the rehearsal ran in separate process groups.
    prefix = (
        unshare,
        "--user",
        "--map-root-user",
        "--net",
        "--pid",
        "--fork",
        "--kill-child",
        "--mount-proc",
        "--",
        "/bin/sh",
        "-c",
        '"$1" link set dev lo up 2>/dev/null || exit 1; shift; exec "$@"',
        "release-rehearsal",
        ip,
    )
    apparmor = shutil.which("aa-exec")
    candidates = [prefix]
    if apparmor:
        candidates.append((apparmor, "-p", "rootlesskit", "--", *prefix))
    for candidate in candidates:
        try:
            with owned_process(
                (*candidate, sys.executable, "-I", "-c", CHECK_NAMESPACE), cwd, env
            ) as probe:
                communicate(probe, timeout=5)
                if probe.returncode == 0:
                    return candidate
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


class SelectedField(NamedTuple):
    name: str
    key: str
    arguments: dict[str, Any]
    children: tuple[SelectedField, ...]


def selection_set(
    query: str, variables: dict[str, Any] | None = None
) -> tuple[SelectedField, ...]:
    """Parse inline field selections and resolve their supplied variables."""
    bindings = variables or {}
    declared: dict[str, tuple[str | None, Any]] = {}
    tokens = deque(
        token
        for token in re.findall(
            r'#[^\r\n]*|"(?:\\.|[^"\\])*"|[_A-Za-z][_0-9A-Za-z]*'
            r"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|[^\s,]",
            query,
        )
        if not token.startswith("#")
    )

    def take_name() -> str:
        name = tokens.popleft()
        if not re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", name):
            raise ValueError("Expected a GraphQL name")
        return name

    def expect(token: str) -> None:
        if tokens.popleft() != token:
            raise ValueError(f"Expected the GraphQL token {token!r}")

    def variable_type() -> str | None:
        if tokens[0] == "[":
            tokens.popleft()
            variable_type()
            expect("]")
            type_name = None
        else:
            type_name = take_name()
        if tokens[0] == "!":
            tokens.popleft()
            if type_name is not None:
                return f"{type_name}!"
        return type_name

    def variable_definitions() -> None:
        expect("(")
        while tokens[0] != ")":
            expect("$")
            name = take_name()
            expect(":")
            type_name = variable_type()
            default = None
            if tokens[0] == "=":
                tokens.popleft()
                if type_name in {"String", "String!"} and not (
                    tokens[0].startswith('"')
                    or (type_name == "String" and tokens[0] == "null")
                ):
                    raise ValueError("Invalid GraphQL String default")
                default = value()
            if name in declared:
                raise ValueError("Duplicate GraphQL variable declaration")
            declared[name] = type_name, default
        expect(")")

    def named_values(end: str, path: tuple[str, ...]) -> dict[str, Any]:
        result = {}
        while tokens[0] != end:
            name = take_name()
            if tokens.popleft() != ":" or name in result:
                raise ValueError("Expected a unique GraphQL name followed by ':'")
            result[name] = value((*path, name))
        tokens.popleft()
        return result

    def value(path: tuple[str, ...] = ()) -> Any:
        token = tokens.popleft()
        if token == "$":
            name = take_name()
            if name not in declared:
                raise ValueError("Undeclared GraphQL variable")
            type_name, default = declared[name]
            if type_name not in {"String", "String!"} and path in (
                ("userIntents", "where", "user"),
                ("userIntents", "where", "intentKey"),
            ):
                raise ValueError(
                    "Expected a String variable for the userIntents filter"
                )
            if (
                path == ("userIntents", "where", "user")
                and type_name == "String"
                and default is None
            ):
                raise ValueError(
                    "Expected a non-null declaration or default for the user filter"
                )
            return bindings.get(name, default)
        if token == "{":
            return named_values("}", path)
        if token == "[":
            items = []
            while tokens[0] != "]":
                items.append(value())
            tokens.popleft()
            return items
        if re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", token):
            # Enum names do not satisfy the replay's string filters.
            return {"true": True, "false": False}.get(token)
        return json.loads(token)

    def fields() -> tuple[SelectedField, ...]:
        expect("{")
        result = []
        while tokens[0] != "}":
            key = name = take_name()
            if tokens[0] == ":":
                tokens.popleft()
                name = take_name()
            arguments = {}
            if tokens[0] == "(":
                tokens.popleft()
                arguments = named_values(")", (name,))
            children = fields() if tokens[0] == "{" else ()
            result.append(SelectedField(name, key, arguments, children))
        tokens.popleft()
        return tuple(result)

    try:
        if tokens[0] != "{":
            if take_name() not in {"query", "mutation", "subscription"}:
                raise ValueError("Expected a GraphQL operation")
            if tokens[0] not in {"(", "{"}:
                take_name()
            if tokens[0] == "(":
                variable_definitions()
        selected = fields()
    except IndexError as exc:
        raise ValueError("Incomplete GraphQL selection") from exc
    if tokens or not selected:
        raise ValueError("Expected a single inline GraphQL operation")
    return selected


def project_fields(value: Any, fields: tuple[SelectedField, ...]) -> Any:
    if isinstance(value, list):
        return [project_fields(item, fields) for item in value]
    if isinstance(value, dict):
        return {
            field.key: project_fields(value.get(field.name), field.children)
            for field in fields
        }
    return value


class Replay:
    def __init__(self, wheel: Path, version: str) -> None:
        fixture = json.loads((TESTS / "fixtures" / "intent_outcomes.json").read_text())
        self.addr = fixture["address"]
        self.responses = {**fixture["recorded"], **fixture["replays"]}
        self.assets: dict[str, tuple[bytes, str]] = {}
        self.add_release(wheel, version)
        self.latest = version
        self.submitted: list[str] = []
        self.lifecycle = "outcome_not_indexed"
        self.history: list[dict[str, Any]] = []

    def add_release(self, wheel: Path, version: str) -> None:
        payload = wheel.read_bytes()
        sums = f"{hashlib.sha256(payload).hexdigest()}  {wheel.name}\n".encode()
        path = f"/{REPO}/releases/download/sai-cli/v{version}"
        self.assets[f"{path}/{wheel.name}"] = payload, "application/octet-stream"
        self.assets[f"{path}/SHA256SUMS"] = sums, "text/plain"

    def intent_rows(self, where: Any) -> list[dict[str, Any]]:
        if not isinstance(where, dict) or where.keys() != {"user", "intentKey"}:
            return []
        user, key = where["user"], where["intentKey"]
        if (
            isinstance(user, str)
            and user.lower() == self.addr
            and self.submitted
            and key == self.submitted[0]
        ):
            row = self.responses[self.lifecycle]["data"]["userIntents"][0]
            return [{**row, "intentKey": key}]
        return []

    def handler(self) -> type[KeeperHandler]:
        replay = self

        class Handler(KeeperHandler):
            def do_GET(self) -> None:
                if self.path.startswith(f"/repos/{REPO}/releases?"):
                    self.reply([{"tag_name": f"sai-cli/v{replay.latest}"}])
                elif asset := replay.assets.get(self.path):
                    self.send_body(*asset)
                elif self.path == "/cosmos/base/tendermint/v1beta1/node_info":
                    self.reply({"default_node_info": {"network": CHAIN}})
                else:
                    self.send_error(404)

            def do_POST(self) -> None:
                if self.path != "/query":
                    self.send_error(404)
                    return
                request = self.read_json()
                query = request.get("query")
                variables = request.get("variables") or {}
                if not isinstance(query, str) or not isinstance(variables, dict):
                    self.send_error(400)
                    return
                try:
                    selected = selection_set(query, variables)
                except ValueError:
                    self.send_error(400)
                    return
                roots = {field.name: field.children for field in selected}
                perp = {field.name: field for field in roots.get("perp", ())}
                if "recordUserIntent" in roots:
                    if self.headers.get("Authorization") != f"Bearer {SYNTHETIC_TOKEN}":
                        self.send_error(403)
                        return
                    submitted = variables.get("input", {})
                    key = submitted.get("intentKey")
                    if (
                        submitted.get("intent", {}).get("kind") != "OPEN_TRADE"
                        or not isinstance(key, str)
                        or not INTENT_KEY.fullmatch(key)
                    ):
                        self.send_error(400)
                        return
                    replay.submitted.append(key)
                    if len(replay.submitted) == 1:
                        # Keeper got the request, but the client never gets its ack.
                        self.close_connection = True
                        return
                    data: dict[str, Any] = {
                        "recordUserIntent": {
                            "intentKey": key,
                            "evmAddress": replay.addr,
                            "lifecycleStep": "jwt_pass",
                            "execActor": "server",
                        }
                    }
                elif "userIntents" in roots:
                    data = {
                        field.key: project_fields(
                            replay.intent_rows(field.arguments.get("where")),
                            field.children,
                        )
                        if field.name == "userIntents"
                        else None
                        for field in selected
                    }
                    if len(data) != len(selected):
                        self.send_error(400)
                        return
                    self.reply({"data": data})
                    return
                elif "tradeHistory" in perp:
                    arguments = perp["tradeHistory"].arguments
                    offset = arguments.get("offset", 0)
                    limit = arguments.get("limit", len(replay.history))
                    if (
                        type(offset) is not int
                        or type(limit) is not int
                        or min(offset, limit) < 0
                    ):
                        self.send_error(400)
                        return
                    data = {
                        "perp": {
                            "tradeHistory": replay.history[offset : offset + limit]
                        }
                    }
                elif "oneClickTrading" in roots:
                    data = {
                        "oneClickTrading": {
                            "enabled": True,
                            "chainId": CHAIN,
                            "contractAddress": "nibi1x",
                        }
                    }
                elif "borrowings" in perp:
                    data = {
                        "perp": {
                            "borrowings": [
                                {
                                    "marketId": 0,
                                    "collateralToken": {"symbol": "USDC"},
                                    "isOpen": True,
                                    "price": 100,
                                }
                            ]
                        }
                    }
                else:
                    self.send_error(400)
                    return
                self.reply({"data": project_fields(data, selected)})

        return Handler


def launcher_interpreter(name: str, launcher: Path) -> Path | None:
    with launcher.open("rb") as file:
        header = file.readline(4096)
    if not header.startswith(b"#!"):
        return None
    interpreter = shlex.split(header[2:].decode())
    if Path(interpreter[0]).name != "env":
        return Path(interpreter[0])
    words = interpreter[1:]
    if words and words[0] == "-S":
        words = words[1:]
    executable = shutil.which(words[0]) if words else None
    require(
        executable is not None, f"{name}'s launcher interpreter was not found on PATH"
    )
    assert executable is not None
    return Path(executable)


class Install:
    def __init__(
        self, root: Path, diagnostics: Path | None = None, *, prepared: bool = False
    ) -> None:
        self.diagnostics = diagnostics
        self.home = root / "home"
        self.sai_home = self.home / ".sai"
        self.session_file = self.sai_home / "trader/sessions.json"
        bin_dir = self.home / ".local" / "bin"
        self.sai = bin_dir / "sai"
        self.uv = bin_dir / "uv"
        self.python = self.home / ".local/share/uv/tools/sai-cli/bin/python"
        if prepared:
            self.env = dict(os.environ)
            return
        self.home.mkdir(mode=0o700)
        (self.home / ".codex").mkdir(mode=0o700)
        bin_dir.mkdir(parents=True)
        runtime_dirs = []
        for name in ("uv", *CLIENTS):
            tool = shutil.which(name)
            if tool is None:
                raise RehearsalError(f"{name} is required on PATH")
            launcher = Path(tool).resolve()
            interpreter = launcher_interpreter(name, launcher)
            if interpreter is None:
                (bin_dir / name).symlink_to(launcher)
            else:
                runtime = interpreter.resolve()
                runtime_dirs.append(str(runtime.parent))
                if runtime.name != interpreter.name:
                    runtime_dirs.append(str(interpreter.parent))
                # Run scripts at their real path so their sibling files resolve.
                entry = bin_dir / name
                entry.write_text(f'#!/bin/sh\nexec {shlex.quote(str(launcher))} "$@"\n')
                entry.chmod(0o700)
        self.env = {
            "HOME": str(self.home),
            "SAI_HOME": str(self.sai_home),
            "PATH": os.pathsep.join(
                dict.fromkeys(
                    [str(bin_dir), *runtime_dirs, *os.defpath.split(os.pathsep)]
                )
            ),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "CODEX_HOME": str(self.home / ".codex"),
            "UV_NO_CONFIG": "1",
            "CI": "1",
        }

    def run(
        self,
        step: str,
        *command: str | Path,
        input_text: str = "",
        check: bool = True,
        timeout: float = 300,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            # File-backed input stays readable across communicate() timeouts.
            with tempfile.TemporaryFile(dir=self.home) as source:
                source.write(input_text.encode())
                source.seek(0)
                with owned_process(
                    command,
                    self.home,
                    self.env if env is None else env,
                    stdin=source,
                ) as process:
                    out, err = communicate(process, timeout=timeout)
                    result = subprocess.CompletedProcess(
                        command,
                        process.returncode,
                        out.decode(errors="replace"),
                        err.decode(errors="replace"),
                    )
        except subprocess.TimeoutExpired as exc:
            self.log_output(step, exc.output or b"", exc.stderr or b"")
            raise RehearsalError(f"{step} exited 124") from exc
        except OSError as exc:
            raise RehearsalError(f"{step} exited 127") from exc
        self.log_output(step, out, err)
        if check and result.returncode:
            raise RehearsalError(f"{step} exited {result.returncode}")
        return result

    def log_output(self, step: str, stdout: bytes, stderr: bytes) -> None:
        if self.diagnostics is not None:
            with self.diagnostics.open("ab") as log:
                log.write(f"\n[{step}] stdout\n".encode() + stdout)
                log.write(f"\n[{step}] stderr\n".encode() + stderr)

    def json(self, step: str, *args: str) -> Any:
        return json.loads(self.run(step, self.sai, "--json", *args).stdout)

    def verify_version(self, version: str) -> None:
        result = self.run("Run the launcher", self.sai, "--version")
        require(
            result.stdout.strip().endswith(f" {version}"), f"Expected sai {version}"
        )

    def verify_saved_endpoint(self, endpoint: str) -> None:
        env = self.env.copy()
        env.pop("SAI_ENDPOINT", None)
        result = self.run(
            "Read the saved Keeper endpoint",
            self.sai,
            "--json",
            "config",
            "get",
            "keeper.endpoint",
            env=env,
        )
        require(
            json.loads(result.stdout)
            == {"key": "keeper.endpoint", "value": endpoint, "source": "file"},
            "Candidate did not resolve the saved Keeper endpoint from the config file",
        )

    def verify_session(self, original: bytes, expected: dict[str, str]) -> None:
        require(
            self.session_file.is_file() and self.session_file.read_bytes() == original,
            "The stored session file changed",
        )
        self.run(
            "Verify the stored session record",
            self.python,
            "-I",
            "-c",
            "import json, sys; from sai_cli.core.storage import load_session; "
            "sys.exit(load_session() != json.load(sys.stdin))",
            input_text=json.dumps(expected),
        )

    def verify_mcp(self, version: str) -> None:
        entries = self.json("Inspect MCP registrations", "mcp", "list")["clients"]
        command = [str(self.sai), "mcp", "serve"]
        for name in CLIENTS:
            row = next(entry for entry in entries if entry["client"] == name)
            require(
                row["current"]
                and row["scope"] == "user"
                and row["registeredCommand"] == command,
                f"{name} registration no longer points to the installed launcher",
            )
        replies = {}
        with self.stdio("Run MCP server", *command) as (send, receive):
            for msg in MCP_REQUESTS:
                send.write((json.dumps({"jsonrpc": "2.0", **msg}) + "\n").encode())
                send.flush()
                if "id" in msg:
                    reply = json.loads(receive.line(f"MCP {msg['method']}"))
                    require(reply.get("id") == msg["id"], "Unexpected MCP reply ID")
                    replies[msg["id"]] = reply
        require("result" in replies[1], f"MCP {version} did not initialize")
        tools = replies.get(2, {}).get("result", {}).get("tools", [])
        require(
            any(tool["name"] == "trade_open" for tool in tools),
            f"MCP {version} did not list trade_open",
        )

    @contextmanager
    def stdio(
        self, step: str, *command: str | Path
    ) -> Generator[tuple[IO[bytes], ReplyReader], None, None]:
        capture = bytearray() if self.diagnostics is not None else None
        with (
            tempfile.TemporaryFile() if capture is not None else nullcontext(None)
        ) as err:
            try:
                with owned_process(
                    command, self.home, self.env, stderr=err or subprocess.DEVNULL
                ) as process:
                    assert process.stdout is not None and process.stdin is not None
                    try:
                        yield process.stdin, ReplyReader(process.stdout, capture)
                    except (RehearsalError, BrokenPipeError) as exc:
                        if code := process.poll():
                            raise RehearsalError(f"{step} exited {code}") from exc
                        raise
                    process.stdin.close()
                    try:
                        code = wait_with_cancellation(process.wait, 5)
                    except subprocess.TimeoutExpired as exc:
                        raise RehearsalError(f"{step} exited 124") from exc
                    if code:
                        raise RehearsalError(f"{step} exited {code}")
            finally:
                if err is not None and capture is not None:
                    err.seek(0)
                    self.log_output(step, bytes(capture), err.read())

    @contextmanager
    def websocket(self) -> Generator[str, None, None]:
        with self.stdio(
            "Run loopback Keeper WebSocket", self.python, "-I", "-c", WEBSOCKET_SERVER
        ) as (_, receive):
            address = receive.line("Loopback Keeper WebSocket")
            require(
                address.startswith("ws://127.0.0.1:"), "Expected a loopback WebSocket"
            )
            yield address


def backup_ledger(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(source)) as old,
        closing(sqlite3.connect(destination)) as new,
    ):
        old.backup(new)


def ledger_row(ledger: Path, key: str, *, migrated: bool | None) -> tuple[Any, ...]:
    with closing(sqlite3.connect(f"{ledger.as_uri()}?mode=ro", uri=True)) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(intents)")}
        has_keeper_status = "keeper_status" in columns
        require(
            migrated is None or has_keeper_status == migrated,
            "Unexpected intent ledger schema",
        )
        require(
            db.execute("PRAGMA user_version").fetchone() == (1,),
            "Unexpected ledger version",
        )
        states = "status, keeper_status" if has_keeper_status else "status"
        row = db.execute(f"SELECT {states} FROM intents WHERE id=?", (key,)).fetchone()
    require(row is not None, "The original open is missing from the ledger")
    return row


def check_doctor(tool: Install, base: str, key: str, *, pending: bool) -> None:
    with tool.websocket() as ws:
        result = tool.run(
            "Run sai doctor",
            tool.sai,
            "--endpoint",
            f"{base}/query",
            "--json",
            "doctor",
            "--ws-endpoint",
            ws,
            "--lcd-endpoint",
            base,
            check=False,
            timeout=30,
        )
    report = json.loads(result.stdout)
    if pending:
        report = report.get("error", {}).get("details", {})
    checks = {check["id"]: check for check in report.get("checks", [])}
    expected = "fail" if pending else "pass"
    require(
        report.get("overall") == expected and result.returncode == int(pending),
        f"Doctor did not report {expected}",
    )
    require(
        {"install", "version", "session", "intents", "keeper", "websocket", "lcd"}
        <= checks.keys(),
        "Doctor is missing required checks",
    )
    unhealthy = {
        name for name, row in checks.items() if row["status"] not in {"pass", "fixed"}
    }
    require(
        unhealthy == ({"intents"} if pending else set()),
        "Doctor reported an unexpected warning or failure",
    )
    if pending:
        require(
            checks["intents"]["details"]["blocking"] == [key],
            "Doctor did not report the original intent as the blocker",
        )


def reports_intent_error(
    result: subprocess.CompletedProcess[str], code: str, key: str
) -> bool:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    return (
        isinstance(error, dict)
        and error.get("code") == code
        and error.get("intentKey") == key
    )


def is_lost_acknowledgement(result: subprocess.CompletedProcess[str], key: str) -> bool:
    return (
        result.returncode == 5 and reports_intent_error(result, "OUTCOME_UNKNOWN", key)
    ) or (
        result.returncode == 1
        and f"Outcome unknown for intent {key}; reconcile before another trade"
        in result.stderr
    )


def is_retry_block(
    result: subprocess.CompletedProcess[str], key: str, *, legacy: bool
) -> bool:
    return (
        result.returncode != 0
        and reports_intent_error(result, "RECENT_OPEN_PENDING", key)
    ) or (
        legacy
        and result.returncode == 1
        and f"Reconcile uncertain intent {key} before submitting another trade"
        in result.stderr
    )


def probe_retry(
    tool: Install, replay: Replay, key: str, *, legacy: bool = False
) -> bool:
    before = len(replay.submitted)
    result = tool.run(
        "Retry the synthetic open", tool.sai, "--json", *TRADE, check=False
    )
    if len(replay.submitted) == before + 1:
        require(
            result.returncode == 0, "The retry reached Keeper but was not acknowledged"
        )
        return False
    require(
        len(replay.submitted) == before and is_retry_block(result, key, legacy=legacy),
        "The retry failed without the original intent's trade guard",
    )
    return True


def probe_intent_projection(tool: Install, replay: Replay, key: str) -> None:
    row = replay.responses["filled_history"]["data"]["perp"]["tradeHistory"][0]
    # Intent status requests 25 rows per page. Place the matching row on page two.
    replay.history = [{**row, "id": row["id"] + i} for i in range(25, 0, -1)]
    replay.history.append(
        {**row, "intentKey": key, "block": {**row["block"], "block_ts": "invalid"}}
    )
    replay.lifecycle = "outcome_not_indexed"
    result = tool.json(
        "Probe intent status projection", "intent", "status", replay.addr, "--key", key
    )
    require(
        result.get("outcome") == "terminal-filled"
        and result.get("matchedTradeHistory")
        == [
            {
                "id": row["id"],
                "intentKey": key,
                "tradeChangeType": row["tradeChangeType"],
                "txHash": row["txHash"].lower(),
                "block": {"block": row["block"]["block"], "block_tsInvalid": True},
                "trade": row["trade"],
            }
        ],
        "Intent status did not project the matching history row from the next page",
    )
    print(
        "Intent status projected the matching row from paginated trade history.",
        flush=True,
    )


def rehearse(
    root: Path,
    wheel: Path,
    version: str,
    baseline: str,
    diagnostics: Path | None,
    *,
    strict: bool,
) -> int:
    tool = Install(root, diagnostics)
    prefix = namespace_prefix(tool.home, tool.env)
    if strict and prefix is None:
        raise RehearsalError(
            "--strict requires a loopback-only network namespace for egress isolation"
        )
    replay = Replay(wheel, version)
    replay.latest = baseline
    installer = f"https://raw.githubusercontent.com/{REPO}/sai-cli/v{baseline}/scripts/install.py"
    with keeper_server(replay.handler()) as base:
        tool.run(
            "Install the published release",
            tool.uv,
            "run",
            installer,
            "--api-base",
            base,
        )
    # uv caches the candidate's dependencies without running candidate commands.
    tool.run(
        "Cache candidate dependencies",
        tool.uv,
        "pip",
        "install",
        "--python",
        tool.python,
        "--target",
        root / "dependency-cache",
        wheel,
    )
    if prefix is None:
        print(
            "Loopback-only network namespace unavailable. Using the dead proxy for proxy-aware traffic.",
            file=sys.stderr,
            flush=True,
        )
        return rehearse_staged(tool, root, wheel, version, baseline)
    print("Using a loopback-only network namespace for the rehearsal.", flush=True)
    command = (
        *prefix,
        sys.executable,
        "-I",
        "-c",
        ISOLATED_STAGE,
        Path(__file__).resolve(),
        root,
        wheel,
        version,
        baseline,
        str(diagnostics) if diagnostics else "",
    )
    # Only the rehearsal script's own progress reaches these streams. It keeps
    # the installed CLI and agent clients' output behind its usual capture.
    with owned_process(command, tool.home, tool.env, stdout=None, stderr=None) as stage:
        return wait_with_cancellation(stage.wait, None)


def isolated_stage(
    root_name: str, wheel_name: str, version: str, baseline: str, log_name: str
) -> int:
    root, wheel = Path(root_name), Path(wheel_name)
    tool = Install(root, Path(log_name) if log_name else None, prepared=True)

    def work() -> int:
        with cancel_on_signal(), reap_detached_children():
            return rehearse_staged(tool, root, wheel, version, baseline)

    return report_result(work)


def rehearse_staged(
    tool: Install, root: Path, wheel: Path, version: str, baseline: str
) -> int:
    baseline_wheel = (
        tool.sai_home / "cache/wheels" / release_install.wheel_name(baseline)
    )
    replay = Replay(baseline_wheel, baseline)
    replay.add_release(wheel, version)
    with (
        keeper_server(replay.handler()) as base,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as dead_proxy,
    ):
        endpoint = f"{base}/query"
        dead_proxy.bind(("127.0.0.1", 0))
        tool.env.update(loopback_env(base, dead_proxy.getsockname()[1]))
        tool.run(
            "Check loopback network settings",
            tool.python,
            "-I",
            "-c",
            CHECK_NETWORK,
            f"{base}/repos/{REPO}/releases?per_page=100",
        )
        tool.verify_version(baseline)
        print(f"Installed published sai {baseline} at {tool.sai}", flush=True)

        tool.json(
            "Configure loopback Keeper", "config", "set", "keeper.endpoint", endpoint
        )
        tool.json("Disable auto-update", "config", "set", "update.auto", "false")
        session_record = {
            "evmAddress": replay.addr,
            "endpoint": endpoint,
            "token": SYNTHETIC_TOKEN,
            "expiresAt": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        }
        tool.run(
            "Save the synthetic session",
            tool.python,
            "-I",
            "-c",
            "import json, sys; from sai_cli.core.storage import save_session; "
            "save_session(json.load(sys.stdin))",
            input_text=json.dumps(session_record),
        )
        session_file = tool.session_file.read_bytes()
        tool.verify_session(session_file, session_record)
        config = tool.sai_home / "config.toml"
        configured = config.read_bytes()
        tool.json("Register MCP clients", "mcp", "install", *CLIENTS)
        tool.verify_mcp(baseline)

        first = tool.run(
            "Open against loopback Keeper", tool.sai, "--json", *TRADE, check=False
        )
        require(
            len(replay.submitted) == 1
            and is_lost_acknowledgement(first, replay.submitted[0]),
            f"The baseline did not report a lost acknowledgement (exit {first.returncode},"
            f" {len(replay.submitted)} Keeper submissions)",
        )
        key = replay.submitted[0]
        ledger = tool.sai_home / "trader/intents.sqlite3"
        require(
            ledger_row(ledger, key, migrated=None) in (("unknown",), ("unknown", None)),
            "The baseline did not retain the unknown open",
        )
        require(
            probe_retry(tool, replay, key, legacy=True),
            "The baseline submitted a second open",
        )

        replay.latest = version
        tool.json(
            "Update to the candidate", "update", "--api-base", base, "--web-base", base
        )
        tool.verify_version(version)
        require(
            probe_retry(tool, replay, key),
            "Candidate submitted a second open before reconciling the legacy intent",
        )
        require(
            ledger_row(ledger, key, migrated=True) == ("unknown", None),
            "The original open must still be unknown before reconciliation",
        )
        require(config.read_bytes() == configured, "Update changed the user config")
        tool.verify_saved_endpoint(endpoint)
        tool.verify_session(session_file, session_record)
        tool.verify_mcp(version)
        args = ("intent", "status", replay.addr, "--key", key)
        status = tool.json("Reconcile with the candidate", *args)
        require(
            isinstance(status, dict)
            and status.get("outcome") == "pending"
            and ledger_row(ledger, key, migrated=True) == ("unknown", "pending"),
            "Candidate did not migrate the open to pending",
        )
        require(
            probe_retry(tool, replay, key),
            "Candidate submitted a second open after reconciling the pending intent",
        )
        check_doctor(tool, base, key, pending=True)
        print(
            f"Upgraded to {version}. Session, config, and MCP survived. Retry blocked before and after reconciliation.",
            flush=True,
        )

        checkpoint = root / "pending.sqlite3"
        backup_ledger(ledger, checkpoint)
        try:
            replay.lifecycle = "market_order_expired"
            check_doctor(tool, base, key, pending=False)
            require(
                ledger_row(ledger, key, migrated=True) == ("terminal", "terminal"),
                "Doctor did not preserve the original open as terminal",
            )
            probe_intent_projection(tool, replay, key)
        finally:
            replay.lifecycle = "outcome_not_indexed"
            replay.history.clear()
            backup_ledger(checkpoint, ledger)
        print(
            "Doctor passed after Keeper reported a terminal outcome. Restored the pending ledger for downgrade.",
            flush=True,
        )

        tool.json(
            "Downgrade through sai update",
            "update",
            "--to",
            baseline,
            "--web-base",
            base,
        )
        tool.verify_version(baseline)
        require(config.read_bytes() == configured, "Downgrade changed the user config")
        tool.verify_session(session_file, session_record)
        tool.verify_mcp(baseline)
        local = tool.json(
            "Read the migrated ledger with the old CLI", "intent", "local", replay.addr
        )
        require(
            any(
                row["intentKey"] == key and row["status"] in {"unknown", "pending"}
                for row in local
            ),
            "The old CLI could not read the migrated pending open",
        )
        require(
            probe_retry(tool, replay, key, legacy=True),
            "The downgraded CLI submitted a second open before reconciliation",
        )
        print(
            f"Downgraded to {baseline}. Retry before reconciliation: blocked.",
            flush=True,
        )
        tool.json("Reconcile with the old CLI", *args)
        blocked = probe_retry(tool, replay, key, legacy=True)
        saved_status = ledger_row(ledger, key, migrated=True)[0]
        status = saved_status if saved_status in ("terminal", "unknown") else "other"
        print(
            f"v{baseline} reconciled market_order_outcome_not_indexed as {status}. "
            f"Retry after reconciliation: {'blocked' if blocked else 'reached loopback Keeper'}."
        )
    return 0


def report_result(work: Callable[[], int]) -> int:
    try:
        return work()
    except RehearsalInterrupted as exc:
        print(
            f"Release rehearsal interrupted by {signal.Signals(exc.signum).name}",
            file=sys.stderr,
        )
        return 128 + exc.signum
    except RehearsalError as exc:
        print(f"Release rehearsal failed: {exc}", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"Release rehearsal failed ({type(exc).__name__})", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "wheel",
        type=Path,
        nargs="?",
        help="Candidate release wheel newer than the baseline",
    )
    parser.add_argument(
        "--from",
        dest="baseline",
        metavar="VERSION",
        help="Published baseline version (default: latest published sai-cli release)",
    )
    parser.add_argument(
        "--self-test", action="store_true", help="Run local rehearsal tests"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Save child output in a private scratch log",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Require a loopback-only network namespace for the rehearsal",
    )
    args = parser.parse_args()
    if args.self_test:
        if args.wheel:
            parser.error("--self-test takes no candidate wheel")
        if args.strict:
            parser.error("--strict requires a candidate wheel")
        if args.baseline is not None:
            parser.error("--from requires a candidate wheel")
        suite = unittest.defaultTestLoader.discover(
            str(Path(__file__).resolve().parent), pattern="release_rehearsal_test.py"
        )
        return int(not unittest.TextTestRunner().run(suite).wasSuccessful())
    if args.wheel is None:
        parser.error("a candidate wheel is required")
    wheel = args.wheel.resolve()
    match = re.fullmatch(rf"sai_cli-({VERSION})-py3-none-any\.whl", wheel.name)
    if not match or not wheel.is_file():
        parser.error("provide an existing sai_cli-X.Y.Z-py3-none-any.whl")
    if args.baseline is not None and not re.fullmatch(VERSION, args.baseline):
        parser.error("--from requires a version in X.Y.Z form")
    version = match[1]

    def work() -> int:
        baseline = args.baseline if args.baseline is not None else resolve_baseline()
        require(
            tuple(map(int, version.split("."))) > tuple(map(int, baseline.split("."))),
            f"Candidate sai {version} must be newer than baseline sai {baseline}",
        )
        diagnostics = None
        if args.verbose:
            directory = Path(tempfile.mkdtemp(prefix="sai-rehearsal-diagnostics-"))
            diagnostics = directory / "child-output.log"
            diagnostics.touch(mode=0o600)
            print(f"Child output log: {diagnostics}", file=sys.stderr)
        with cancel_on_signal(), cleanup_stack() as cleanup:
            scratch = cleanup.enter_context(
                tempfile.TemporaryDirectory(
                    prefix="sai-release-rehearsal-",
                    dir=diagnostics.parent if diagnostics else None,
                )
            )
            with reap_detached_children():
                return rehearse(
                    Path(scratch),
                    wheel,
                    version,
                    baseline,
                    diagnostics,
                    strict=args.strict,
                )

    return report_result(work)


if __name__ == "__main__":
    sys.exit(main())
