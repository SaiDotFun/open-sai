"""Rehearse a sai-cli release wheel against a published install."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
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
import tomllib
import unittest
from collections import deque
from collections.abc import Callable, Generator
from contextlib import ExitStack, closing, contextmanager, nullcontext, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, NamedTuple, TypeVar
from urllib.parse import quote

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
CUTOVER_BASELINE = "0.1.3"
CUTOVER_CLIENTS = (*CLIENTS, "cursor")
RETIRED_MCP_FIXTURE = "sai" + "-mcp"
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
        tag = release_install.find_latest(
            api_base, web_base, check_cancelled=CANCELLATION.check
        )
    except release_install.LookupFailed as exc:
        raise RehearsalError(
            f"Could not resolve the latest published sai-cli release: {exc}. "
            "Pass --from VERSION to select a published baseline."
        ) from exc
    if tag is None:
        raise RehearsalError("No sai-cli release is published yet")
    return release_install.version_of(tag)


def verify_published_baseline(version: str, api_base: str) -> None:
    tag = f"{release_install.TAG_PREFIX}{version}"
    url = f"{api_base.rstrip('/')}/repos/{REPO}/releases/tags/{quote(tag, safe='')}"
    error = f"Could not verify published sai-cli release {version}"
    try:
        status, _, body = release_install.within(
            release_install.TOTAL_TIMEOUT,
            f"checking release {tag}",
            lambda deadline: release_install.request(url, "GET", deadline),
            check_cancelled=CANCELLATION.check,
        )
    except release_install.LookupFailed as exc:
        raise RehearsalError(f"{error}: {exc}") from exc
    if status == 404:
        raise RehearsalError(f"{error}: release not found on GitHub")
    if status != 200:
        raise RehearsalError(f"{error}: GitHub API answered HTTP {status}")
    require(
        len(body) <= release_install.MAX_BODY,
        f"{error}: GitHub API answer is too large",
    )
    try:
        release = json.loads(body)
    except ValueError as exc:
        raise RehearsalError(
            f"{error}: GitHub API answered with malformed JSON"
        ) from exc
    require(
        isinstance(release, dict)
        and release.get("tag_name") == tag
        and release.get("draft") is False
        and release.get("prerelease") is False,
        f"{error}: expected a stable release tagged {tag}",
    )
    assets = release.get("assets")
    uploaded = (
        [
            asset.get("name")
            for asset in assets
            if isinstance(asset, dict) and asset.get("state") == "uploaded"
        ]
        if isinstance(assets, list)
        else []
    )
    missing = [
        name
        for name in (release_install.wheel_name(version), "SHA256SUMS")
        if name not in uploaded
    ]
    require(not missing, f"{error}: missing release assets {', '.join(missing)}")


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
    children: tuple[SelectedField, ...] | None


FILTER_ARGUMENT_TYPES = {
    "userIntents": "UserIntentsFilter!",
    "tradeHistory": "PerpTradeHistoryFilter",
}
FILTER_INPUT_FIELDS = {
    "UserIntentsFilter": {"user": "String!", "intentKey": "String"},
    "PerpTradeHistoryFilter": {"trader": "String!"},
}
VARIABLE_SCALARS = {"String": str, "Int": int, "Boolean": bool}
ENUM_LITERAL = object()


def expected_variable_type(path: tuple[str, ...]) -> str | None:
    if path in {("@include", "if"), ("@skip", "if")}:
        return "Boolean!"
    if len(path) < 2 or path[1] != "where":
        return None
    argument_type = FILTER_ARGUMENT_TYPES.get(path[0])
    if argument_type is None or len(path) == 2:
        return argument_type
    if len(path) == 3:
        return FILTER_INPUT_FIELDS[argument_type.removesuffix("!")].get(path[2])
    return None


def validate_input(type_name: str, item: Any) -> None:
    name = type_name.removesuffix("!")
    if item is None:
        if name != type_name:
            raise ValueError(f"Missing value for non-null GraphQL type {type_name}")
    elif name.startswith("["):
        for element in item if isinstance(item, list) else [item]:
            validate_input(name[1:-1], element)
    elif name in VARIABLE_SCALARS and type(item) is not VARIABLE_SCALARS[name]:
        raise ValueError(f"Invalid value for GraphQL scalar {name}")
    elif name in FILTER_INPUT_FIELDS:
        fields = FILTER_INPUT_FIELDS[name]
        if not isinstance(item, dict) or item.keys() - fields.keys():
            raise ValueError(f"Invalid value for GraphQL input object {name}")
        for field, field_type in fields.items():
            validate_input(field_type, item.get(field))


def selection_set(
    query: str, variables: dict[str, Any] | None = None
) -> tuple[SelectedField, ...]:
    """Parse inline field selections and resolve their supplied variables."""
    supplied = variables or {}
    declared: dict[str, tuple[str, Any]] = {}
    bindings: dict[str, Any] = {}
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

    def variable_type() -> str:
        if tokens[0] == "[":
            tokens.popleft()
            type_name = f"[{variable_type()}]"
            expect("]")
        else:
            type_name = take_name()
        if tokens[0] == "!":
            tokens.popleft()
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
                default = value(None)
                validate_input(type_name, default)
            if name in declared:
                raise ValueError("Duplicate GraphQL variable declaration")
            declared[name] = type_name, default
        expect(")")
        # Coerce all declared variables before evaluating any selection.
        for name, (type_name, default) in declared.items():
            bound = supplied.get(name, default)
            validate_input(type_name, bound)
            bindings[name] = bound

    def named_values(end: str, path: tuple[str, ...] | None) -> dict[str, Any]:
        result = {}
        while tokens[0] != end:
            name = take_name()
            if tokens.popleft() != ":" or name in result:
                raise ValueError("Expected a unique GraphQL name followed by ':'")
            result[name] = value(None if path is None else (*path, name))
        tokens.popleft()
        return result

    def value(path: tuple[str, ...] | None) -> Any:
        token = tokens.popleft()
        if token == "$":
            name = take_name()
            if path is None:
                raise ValueError("A GraphQL default must be constant")
            if name not in bindings:
                raise ValueError("Undeclared GraphQL variable")
            type_name, default = declared[name]
            if expected := expected_variable_type(path):
                if type_name.removesuffix("!") != expected.removesuffix("!") or (
                    expected.endswith("!")
                    and not type_name.endswith("!")
                    and default is None
                ):
                    raise ValueError(
                        f"Expected a {expected} variable at {'.'.join(path)}"
                    )
                validate_input(expected, bindings[name])
            return bindings[name]
        if token == "{":
            return named_values("}", path)
        if token == "[":
            items = []
            while tokens[0] != "]":
                items.append(value(None if path is None else ()))
            tokens.popleft()
            return items
        if re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", token):
            # An enum literal is distinct from both null and a string.
            return {"true": True, "false": False, "null": None}.get(token, ENUM_LITERAL)
        return json.loads(token)

    def included() -> bool:
        directives = {}
        while tokens[0] == "@":
            tokens.popleft()
            name = take_name()
            if name in directives or name not in {"include", "skip"}:
                raise ValueError("Unsupported or repeated GraphQL field directive")
            expect("(")
            arguments = named_values(")", (f"@{name}",))
            if arguments.keys() != {"if"}:
                raise ValueError("Expected an 'if' argument for the GraphQL directive")
            condition = arguments["if"]
            validate_input("Boolean!", condition)
            directives[name] = condition
        return directives.get("include", True) and not directives.get("skip", False)

    def fields() -> tuple[SelectedField, ...]:
        expect("{")
        if tokens[0] == "}":
            raise ValueError("Expected a nonempty GraphQL selection")
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
            if filter_type := FILTER_ARGUMENT_TYPES.get(name):
                validate_input(filter_type, arguments.get("where"))
            include = included()
            children = fields() if tokens[0] == "{" else None
            if include:
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
    if tokens:
        raise ValueError("Expected a single inline GraphQL operation")
    return selected


def project_fields(value: Any, fields: tuple[SelectedField, ...] | None) -> Any:
    if fields is None:
        return value
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
        self.nibi_addr = self.responses["filled"]["data"]["userIntents"][0][
            "nibiAddress"
        ]
        self.assets: dict[str, tuple[bytes, str]] = {}
        self.add_release(wheel, version)
        self.get_requests: list[str] = []
        self.latest = version
        self.submitted: list[str] = []
        self.lifecycle = "outcome_not_indexed"
        self.history: list[dict[str, Any]] = []

    def add_release(self, wheel: Path, version: str, *native_assets: Path) -> None:
        sums = []
        path = f"/{REPO}/releases/download/sai-cli/v{version}"
        for asset in (wheel, *native_assets):
            payload = asset.read_bytes()
            sums.append(f"{hashlib.sha256(payload).hexdigest()}  {asset.name}\n")
            self.assets[f"{path}/{asset.name}"] = payload, "application/octet-stream"
        self.assets[f"{path}/SHA256SUMS"] = "".join(sums).encode("ascii"), "text/plain"

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

    def history_page(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", len(self.history))
        if type(offset) is not int or type(limit) is not int or min(offset, limit) < 0:
            raise ValueError("Invalid history pagination")
        where = arguments.get("where")
        trader = where.get("trader") if isinstance(where, dict) else None
        if not isinstance(trader, str):
            return []
        aliases = {trader.lower()}
        if trader.lower() in {self.addr, self.nibi_addr}:
            aliases.update((self.addr, self.nibi_addr))
        rows = [
            row for row in self.history if row["trade"]["trader"].lower() in aliases
        ]
        return rows[offset : offset + limit]

    def handler(self) -> type[KeeperHandler]:
        replay = self

        class Handler(KeeperHandler):
            def do_GET(self) -> None:
                if self.path.startswith(
                    (f"/repos/{REPO}/releases", f"/{REPO}/releases/")
                ):
                    replay.get_requests.append(self.path)
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
                variables = request.get("variables")
                if variables is None:
                    variables = {}
                if not isinstance(query, str) or not isinstance(variables, dict):
                    self.send_error(400)
                    return
                try:
                    selected = selection_set(query, variables)
                except ValueError:
                    self.send_error(400)
                    return
                aliased_fields = {(field.key, field.name) for field in selected}
                if len(aliased_fields) != len({key for key, _ in aliased_fields}):
                    self.send_error(400)
                    return
                empty_objects = {}
                active = []
                for field in selected:
                    if field.children == () and field.name in {
                        "perp",
                        "oneClickTrading",
                    }:
                        empty_objects[field.key] = {}
                    else:
                        active.append(field)
                if not active:
                    self.reply({"data": empty_objects})
                    return
                selected = tuple(active)
                roots = {field.name for field in selected}
                perp = {
                    child.name
                    for root in selected
                    if root.name == "perp"
                    for child in root.children or ()
                }
                if "recordUserIntent" in roots:
                    if self.headers.get("Authorization") != f"Bearer {SYNTHETIC_TOKEN}":
                        self.send_error(403)
                        return
                    field = next(
                        field for field in selected if field.name == "recordUserIntent"
                    )
                    submitted = field.arguments.get("input")
                    key = (
                        submitted.get("intentKey")
                        if isinstance(submitted, dict)
                        else None
                    )
                    intent = (
                        submitted.get("intent") if isinstance(submitted, dict) else None
                    )
                    if (
                        not isinstance(intent, dict)
                        or intent.get("kind") != "OPEN_TRADE"
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
                    self.reply({"data": empty_objects | data})
                    return
                elif "tradeHistory" in perp:
                    try:
                        data = {
                            root.key: {
                                field.key: project_fields(
                                    replay.history_page(field.arguments), field.children
                                )
                                if field.name == "tradeHistory"
                                else None
                                for field in root.children or ()
                            }
                            if root.name == "perp"
                            else None
                            for root in selected
                        }
                    except ValueError:
                        self.send_error(400)
                        return
                    self.reply({"data": empty_objects | data})
                    return
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
                self.reply({"data": empty_objects | project_fields(data, selected)})

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
        self,
        root: Path,
        diagnostics: Path | None = None,
        *,
        prepared: bool = False,
        clients: tuple[str, ...] = CLIENTS,
        uv: Path | None = None,
    ) -> None:
        self.diagnostics = diagnostics
        self.home = root / "home"
        self.cwd = self.home
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
        for name in ("uv", *clients):
            tool = str(uv) if name == "uv" and uv is not None else shutil.which(name)
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
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            # File-backed input stays readable across communicate() timeouts.
            with tempfile.TemporaryFile(dir=self.home) as source:
                source.write(input_text.encode())
                source.seek(0)
                with owned_process(
                    command,
                    cwd or self.cwd,
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

    def json(self, step: str, *args: str, cwd: Path | None = None) -> Any:
        return json.loads(self.run(step, self.sai, "--json", *args, cwd=cwd).stdout)

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
                    command, self.cwd, self.env, stderr=err or subprocess.DEVNULL
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


class NativeAssets(NamedTuple):
    baseline: Path
    bridge: Path
    shim: Path
    archive: Path

    @property
    def bridge_version(self) -> str:
        return self.bridge.name.split("-")[1]

    @property
    def version(self) -> str:
        return self.shim.name.split("-")[1]


def native_assets(
    published: Path, bridge: Path, shim: Path, archive: Path
) -> NativeAssets:
    baseline = published / release_install.wheel_name(CUTOVER_BASELINE)
    expected = release_install.expected_hash(
        (published / "SHA256SUMS").read_text(encoding="ascii"), baseline.name
    )
    require(
        expected is not None and release_install.file_digest(baseline) == expected,
        f"Published wheel {baseline.name} did not match SHA256SUMS",
    )
    match = re.fullmatch(rf"sai_cli-({VERSION})-py3-none-any\.whl", bridge.name)
    require(
        match is not None and bridge.is_file(), "Expected a full Python bridge wheel"
    )
    assets = NativeAssets(baseline.resolve(), bridge.resolve(), shim, archive.resolve())
    require(
        tuple(map(int, assets.bridge_version.split(".")))
        > tuple(map(int, CUTOVER_BASELINE.split("."))),
        "The bridge must be newer than the published Python baseline",
    )
    require(
        assets.version not in (assets.bridge_version, CUTOVER_BASELINE),
        "Use a distinct native version",
    )
    machine = platform.machine()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    require(
        archive.name == f"sai-{assets.version}-{machine}-unknown-linux-gnu.tar.gz"
        and archive.is_file(),
        "Expected a Linux native archive matching this host and the loopback shim wheel",
    )
    return assets


def rehearse_native(root: Path, assets: NativeAssets, diagnostics: Path | None) -> int:
    tool = Install(root, diagnostics, clients=(*CLIENTS, "cursor-agent"))
    prefix = namespace_prefix(tool.home, tool.env)
    if prefix is None:
        raise RehearsalError(
            "Native cutover requires a loopback-only network namespace"
        )
    tool.env.update(UV_PYTHON=sys.executable, UV_PYTHON_DOWNLOADS="never")
    for wheel in (assets.baseline, assets.bridge):
        tool.run(
            f"Cache dependencies for {wheel.name}",
            tool.uv,
            "pip",
            "install",
            "--target",
            root / "dependency-cache",
            wheel,
        )
    print(
        f"Native candidate {assets.archive.name}, SHA-256 "
        f"{release_install.file_digest(assets.archive)}.",
        flush=True,
    )
    command = (
        *prefix,
        sys.executable,
        "-I",
        "-c",
        'import runpy, sys; sys.exit(runpy.run_path(sys.argv[1])["native_stage"](*sys.argv[2:]))',
        Path(__file__).resolve(),
        root,
        str(diagnostics) if diagnostics else "",
        *assets,
    )
    with owned_process(command, tool.home, tool.env, stdout=None, stderr=None) as stage:
        return wait_with_cancellation(stage.wait, None)


def native_stage(root: str, log: str, *paths: str) -> int:
    assets = NativeAssets(*map(Path, paths))
    tool = Install(Path(root), Path(log) if log else None, prepared=True)
    tool.cwd = Path(root) / "project"

    def work() -> int:
        with cancel_on_signal(), reap_detached_children():
            require(
                [name for _, name in socket.if_nameindex()] == ["lo"],
                "Expected a loopback-only network namespace",
            )
            tool.cwd.mkdir(mode=0o700)
            return rehearse_native_staged(tool, assets)

    return report_result(work)


class CutoverMcp:
    def __init__(self, tool: Install, base: str) -> None:
        self.tool = tool
        self.paths = {
            "claude": tool.home / ".claude.json",
            "codex": tool.home / ".codex/config.toml",
            "cursor": tool.home / ".cursor/mcp.json",
        }
        self.command = [str(tool.sai), "mcp", "serve"]
        self.env = {
            "SAI_HOME": str(tool.sai_home),
            # The CI shim requires this fixture origin. Codex starts stdio
            # servers with a filtered environment.
            "SAI_NATIVE_RELEASE_BASE": base,
        }
        self.other = {"url": f"{base}/unrelated-mcp"}

    def expected(self, client: str, *, stale: bool) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "command": RETIRED_MCP_FIXTURE if stale else self.command[0],
            "args": [] if stale else self.command[1:],
            "env": self.env,
        }
        if client == "claude":
            entry["type"] = "stdio"
        key = "mcp_servers" if client == "codex" else "mcpServers"
        return {key: {"sai": entry, "other": self.other}}

    def read(self, client: str) -> dict[str, Any]:
        path = self.paths[client]
        source = path.read_text(encoding="utf-8")
        return tomllib.loads(source) if client == "codex" else json.loads(source)

    def seed(self, client: str, *, stale: bool = False) -> None:
        path = self.paths[client]
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = self.expected(client, stale=stale)
        if client == "codex":
            entry = data["mcp_servers"]["sai"]
            source = (
                "[mcp_servers.sai]\n"
                f"command = {json.dumps(entry['command'], ensure_ascii=False)}\n"
                f"args = {json.dumps(entry['args'])}\n"
                "[mcp_servers.sai.env]\n"
                + "".join(
                    f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
                    for key, value in self.env.items()
                )
                + "[mcp_servers.other]\n"
                f"url = {json.dumps(self.other['url'])}\n"
            )
        else:
            source = json.dumps(data)
        path.write_text(source, encoding="utf-8")
        path.chmod(0o600)

    def add_stale_cursor_alias(self) -> None:
        data = self.read("cursor")
        data["mcpServers"][RETIRED_MCP_FIXTURE] = self.expected("cursor", stale=True)[
            "mcpServers"
        ]["sai"]
        self.paths["cursor"].write_text(json.dumps(data), encoding="utf-8")

    def verify_cursor_alias(self) -> None:
        data = self.read("cursor")
        expected = self.expected("cursor", stale=False)["mcpServers"]["sai"]
        actual = data.get("mcpServers", {}).get(RETIRED_MCP_FIXTURE)
        require(
            actual == expected,
            "The native updater did not repoint the existing Cursor alias",
        )

    def verify(self, client: str, *, cursor_alias: bool = False) -> None:
        expected = self.expected(client, stale=False)
        data = self.read(client)
        key = next(iter(expected))
        if cursor_alias:
            data[key].pop(RETIRED_MCP_FIXTURE, None)
        entry = data.get(key, {}).get("sai", {})
        if isinstance(entry.get("command"), str):
            # uv can spell the registered bin path with a parent component.
            entry["command"] = os.path.normpath(entry["command"])
        require(
            all(data.get(key) == value for key, value in expected.items()),
            f"{client} did not preserve the registrations and select {self.tool.sai}",
        )
        if client == "claude":
            report = self.tool.run(
                "Start Sai through Claude Code",
                "claude",
                "mcp",
                "get",
                "sai",
                timeout=30,
            )
            require(
                re.search(r"^\s*Status: .*\bConnected\s*$", report.stdout, re.MULTILINE)
                is not None,
                "Claude Code did not connect to the registered Sai server",
            )
        elif client == "codex":
            verify_codex_mcp(self.tool)
        else:
            report = self.tool.run(
                "Start Sai through Cursor",
                "cursor-agent",
                "mcp",
                "list-tools",
                "sai",
                timeout=30,
            )
            require(
                re.search(r"^- trade_open\b", report.stdout, re.MULTILINE) is not None,
                "Cursor did not list the registered Sai tools",
            )


def verify_codex_mcp(tool: Install) -> None:
    with tool.stdio("Start Sai through Codex", "codex", "app-server") as (
        send,
        receive,
    ):

        def send_message(message: dict[str, Any]) -> None:
            send.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
            send.flush()

        def request(identifier: int, method: str, params: dict[str, Any]) -> Any:
            send_message({"id": identifier, "method": method, "params": params})
            for _ in range(40):
                reply = json.loads(receive.line(f"Codex {method}"))
                if reply.get("id") == identifier:
                    require("result" in reply, f"Codex {method} failed")
                    return reply["result"]
            raise RehearsalError(f"Codex {method} did not reply")

        request(
            1, "initialize", {"clientInfo": {"name": "sai-rehearsal", "version": "1"}}
        )
        send_message({"method": "initialized"})
        status = request(2, "mcpServerStatus/list", {"serverName": "sai"})
        require(
            any(
                row["name"] == "sai" and "trade_open" in row["tools"]
                for row in status["data"]
            ),
            "Codex did not list the registered Sai tools",
        )


def cutover_update(
    tool: Install, base: str, version: str, *, cwd: Path | None = None
) -> dict[str, Any]:
    report = tool.json(
        f"Update to {version}",
        "update",
        "--to",
        version,
        "--api-base",
        base,
        "--web-base",
        base,
        cwd=cwd,
    )
    require(
        report.get("status") == "updated" and report.get("target") == version,
        f"sai update did not install {version}",
    )
    return report


def cutover_doctor(tool: Install, label: str, *, cwd: Path) -> dict[str, Any]:
    result = tool.run(
        label, tool.sai, "--json", "doctor", "--offline", check=False, cwd=cwd
    )
    report = json.loads(result.stdout)
    report = report.get("error", {}).get("details", report)
    require(isinstance(report.get("checks"), list), "Doctor did not return its checks")
    return {entry["id"]: entry for entry in report["checks"]}


def intent_log(tool: Install, key: str) -> list[dict[str, Any]]:
    report = tool.json("Read retained intent logs", "logs", "intent", key, "--offline")
    require(report.get("intentKey") == key, "The log report omitted the pending intent")
    return report["events"]


def verify_intent_logs(tool: Install, key: str, expected: list[dict[str, Any]]) -> None:
    actual = intent_log(tool, key)
    require(
        bool(expected) and all(row in actual for row in expected),
        "The report did not include the expected intent events",
    )


def verify_local_intent(tool: Install, replay: Replay, key: str) -> None:
    local = tool.json("Read the pending intent ledger", "intent", "local", replay.addr)
    require(
        any(
            row["intentKey"] == key and row["status"] in {"unknown", "pending"}
            for row in local
        ),
        "The local ledger did not report the original pending intent",
    )


def rehearse_native_staged(tool: Install, assets: NativeAssets) -> int:
    baseline, bridge, native = CUTOVER_BASELINE, assets.bridge_version, assets.version
    replay = Replay(assets.baseline, baseline)
    replay.add_release(assets.bridge, bridge)
    replay.add_release(assets.shim, native, assets.archive)
    replay.lifecycle = "pending_exec"
    failures = 0

    def check(label: str, action: Callable[[], None]) -> None:
        nonlocal failures
        try:
            action()
        except RehearsalError as exc:
            failures += 1
            print(f"FAIL {label} ({exc})", flush=True)
        except (OSError, TypeError, ValueError, KeyError) as exc:
            failures += 1
            print(f"FAIL {label} ({type(exc).__name__})", flush=True)
        else:
            print(f"PASS {label}", flush=True)

    with (
        keeper_server(replay.handler()) as base,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as dead_proxy,
    ):
        dead_proxy.bind(("127.0.0.1", 0))
        tool.env.update(loopback_env(base, dead_proxy.getsockname()[1]))
        # Exercise stored config resolution throughout the cutover.
        del tool.env["SAI_ENDPOINT"]
        tool.env["SAI_NATIVE_RELEASE_BASE"] = base
        tool.run(
            "Install the verified published Python wheel",
            sys.executable,
            SCRIPTS / "install.py",
            "--version",
            baseline,
            "--api-base",
            base,
            "--web-base",
            base,
        )
        tool.verify_version(baseline)
        launcher = tool.sai.resolve(strict=True)
        endpoint = f"{base}/query"
        tool.json(
            "Configure loopback Keeper", "config", "set", "keeper.endpoint", endpoint
        )
        tool.json("Disable auto-update", "config", "set", "update.auto", "false")
        expiry = (datetime.now(UTC) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        session = {
            "evmAddress": replay.addr,
            "endpoint": endpoint,
            "token": SYNTHETIC_TOKEN,
            "expiresAt": expiry,
        }
        tool.run(
            "Save the synthetic session with published Python",
            tool.python,
            "-I",
            "-c",
            "import json, sys; from sai_cli.core.storage import save_session; "
            "save_session(json.load(sys.stdin))",
            input_text=json.dumps(session),
        )
        session_bytes = tool.session_file.read_bytes()
        config = tool.sai_home / "config.toml"
        config_bytes = config.read_bytes()
        first = tool.run(
            "Leave an open pending on loopback Keeper",
            tool.sai,
            "--json",
            *TRADE,
            check=False,
        )
        require(
            len(replay.submitted) == 1
            and is_lost_acknowledgement(first, replay.submitted[0]),
            "Published Python did not save the open with a lost acknowledgement",
        )
        key = replay.submitted[0]
        ledger = tool.sai_home / "trader/intents.sqlite3"
        verify_local_intent(tool, replay, key)
        require(
            probe_retry(tool, replay, key, legacy=True),
            "Published Python retried the open",
        )
        python_events = [
            row for row in intent_log(tool, key) if row["version"] == baseline
        ]
        require(
            any(row["event"] == "intent.reserved" for row in python_events),
            "Published Python did not log the original reservation",
        )
        mcp = CutoverMcp(tool, base)
        for client in CUTOVER_CLIENTS:
            mcp.seed(client)
            mcp.verify(client)
        print(
            f"PASS Python {baseline} install and populated profile at {tool.sai} -> {launcher}",
            flush=True,
        )

        cutover_update(tool, base, bridge)
        tool.verify_version(bridge)

        def python_doctor_from_home() -> None:
            mcp.seed("cursor", stale=True)
            doctors = cutover_doctor(tool, "Run Python doctor from HOME", cwd=tool.home)
            require(
                doctors.get("mcp.cursor", {}).get("status") == "fixed",
                "Python doctor did not report a successful Cursor repair from HOME",
            )
            mcp.verify("cursor")

        check("Python doctor repairs Cursor from HOME", python_doctor_from_home)
        for client in CUTOVER_CLIENTS:
            mcp.seed(client, stale=True)
        update = cutover_update(tool, base, native, cwd=tool.home)
        tool.verify_version(native)
        digest = release_install.file_digest(assets.archive)
        binaries = list(tool.sai_home.glob(f"bin/*/{native}/*/{digest}/sai"))
        require(
            len(binaries) == 1, "The shim did not cache the verified native archive"
        )
        require(
            tool.sai.resolve(strict=True) == launcher,
            "The bridge changed uv's launcher target",
        )
        print(
            f"PASS Python bridge {bridge} installed native {native} at {binaries[0]} through {tool.sai}",
            flush=True,
        )

        def repair_claude() -> None:
            repairs = next(
                (
                    result.get("manualRepairs", [])
                    for result in update.get("mcp", [])
                    if result["client"] == "claude"
                ),
                [],
            )
            require(
                any(
                    repair["scope"] == "user" and repair["name"] == "sai"
                    for repair in repairs
                ),
                "The bridge update did not report the Claude Code repair",
            )
            tool.json(
                "Apply the Claude Code repair reported by update",
                "mcp",
                "install",
                "--client",
                "claude",
                "--scope",
                "user",
                "--repair-existing",
                "sai",
            )
            mcp.verify("claude")

        check(
            "Bridge update reports a Claude Code repair and the native command applies it",
            repair_claude,
        )

        def bridge_cursor() -> None:
            cursor = [row for row in update.get("mcp", []) if row["client"] == "cursor"]
            require(
                len(cursor) == 1 and cursor[0]["status"] == "fixed",
                "The bridge update did not report a successful Cursor repair from HOME",
            )
            mcp.verify("cursor")

        check("Bridge update MCP codex", lambda: mcp.verify("codex"))
        check("Bridge update repairs Cursor from HOME", bridge_cursor)

        def profile() -> None:
            require(config.read_bytes() == config_bytes, "The config file changed")
            tool.verify_saved_endpoint(endpoint)
            require(
                tool.session_file.read_bytes() == session_bytes,
                "The session file changed",
            )
            status = tool.json("Read the existing session", "auth", "status")
            require(
                status.get("addr") == replay.addr
                and status.get("endpoint") == base
                and datetime.fromisoformat(status["expiresAt"])
                == datetime.fromisoformat(expiry),
                "The active session did not match the published Python session",
            )
            verify_local_intent(tool, replay, key)

        check("Native reads existing config, session, and ledger", profile)
        check(
            "Native reads published Python intent logs",
            lambda: verify_intent_logs(tool, key, python_events),
        )

        def retry() -> None:
            require(probe_retry(tool, replay, key), "Native retried the pending open")
            status = tool.json(
                "Reconcile the pending open with native",
                "intent",
                "status",
                replay.addr,
                "--key",
                key,
            )
            require(
                status.get("outcome") == "pending"
                and ledger_row(ledger, key, migrated=True) == ("unknown", "pending"),
                "Native did not persist the pending Keeper result",
            )
            require(
                probe_retry(tool, replay, key), "Native retried after reconciliation"
            )

        check("Native blocks retry and writes a pending ledger status", retry)
        native_events = [
            row
            for row in intent_log(tool, key)
            if row["version"] == native and row["event"] == "intent.keeper"
        ]

        def verify_doctor_mcp(doctor: dict[str, Any], client: str) -> None:
            expected = "pass" if client == "claude" else "fixed"
            status = doctor.get(f"mcp.{client}", {}).get("status", "missing")
            require(
                status == expected, f"Doctor reported {status}, expected {expected}"
            )
            mcp.verify(client)

        for location, cwd in (("project", tool.cwd), ("HOME", tool.home)):
            for client in ("codex", "cursor"):
                mcp.seed(client, stale=True)
            doctor = cutover_doctor(tool, f"Run native doctor from {location}", cwd=cwd)
            for client in CUTOVER_CLIENTS:
                check(
                    f"Native doctor from {location} MCP {client}",
                    lambda client=client, doctor=doctor: verify_doctor_mcp(
                        doctor, client
                    ),
                )

        # Restore the good entries from before the doctor probe. Add a stale
        # Cursor alias to check repair and startup independently on rollback.
        mcp.seed("codex")
        mcp.seed("cursor")
        mcp.add_stale_cursor_alias()
        cutover_update(tool, base, baseline)
        tool.verify_version(baseline)
        require(
            tool.sai.resolve(strict=True) == launcher,
            "Rollback changed uv's launcher target",
        )
        print(
            f"PASS Native update restored published Python {baseline} through {tool.sai}",
            flush=True,
        )
        check("Rolled-back Python reads the same config, session, and ledger", profile)

        def verify_rollback_logs() -> None:
            require(
                bool(native_events), "Native Sai did not log the pending Keeper result"
            )
            verify_intent_logs(tool, key, [*python_events, *native_events])

        check(
            "Rolled-back Python reads intent logs from both runtimes",
            verify_rollback_logs,
        )
        check(
            "Rolled-back Python blocks the pending open",
            lambda: require(
                probe_retry(tool, replay, key, legacy=True),
                "Rolled-back Python retried the open",
            ),
        )
        for client in CUTOVER_CLIENTS:
            check(
                f"Rollback update MCP {client}",
                lambda client=client: mcp.verify(
                    client, cursor_alias=client == "cursor"
                ),
            )
        check(
            "Rollback update repoints the stale Cursor alias", mcp.verify_cursor_alias
        )
    print(
        f"Native cutover rehearsal finished with {failures} failed checks.",
        flush=True,
    )
    return int(bool(failures))


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


def main(*, api_base: str = release_install.API_BASE) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "wheel",
        type=Path,
        nargs="?",
        help="Candidate release wheel newer than the baseline",
    )
    parser.add_argument(
        "--bridge-wheel",
        type=Path,
        help="Full Python bridge wheel for the native cutover",
    )
    parser.add_argument(
        "--native-archive",
        type=Path,
        help="Native cutover archive matching the positional loopback shim wheel",
    )
    parser.add_argument(
        "--published-assets",
        type=Path,
        help="Directory containing the published 0.1.3 wheel and its SHA256SUMS",
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
    cutover = any((args.bridge_wheel, args.native_archive, args.published_assets))
    if cutover and not all(
        (args.bridge_wheel, args.native_archive, args.published_assets)
    ):
        parser.error(
            "native cutover needs --bridge-wheel, --native-archive, and --published-assets"
        )
    if cutover and (args.baseline is not None or args.self_test):
        parser.error("native cutover uses the published 0.1.3 baseline")
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
        with cancel_on_signal(), cleanup_stack() as cleanup:
            diagnostics = None
            if args.verbose:
                directory = Path(tempfile.mkdtemp(prefix="sai-rehearsal-diagnostics-"))
                diagnostics = directory / "child-output.log"
                diagnostics.touch(mode=0o600)
                print(f"Child output log: {diagnostics}", file=sys.stderr)
            scratch = Path(
                cleanup.enter_context(
                    tempfile.TemporaryDirectory(
                        prefix="sai-release-rehearsal-",
                        dir=diagnostics.parent if diagnostics else None,
                    )
                )
            )
            if cutover:
                assets = native_assets(
                    args.published_assets, args.bridge_wheel, wheel, args.native_archive
                )
                with reap_detached_children():
                    return rehearse_native(scratch, assets, diagnostics)
            baseline = (
                args.baseline
                if args.baseline is not None
                else resolve_baseline(api_base)
            )
            require(
                tuple(map(int, version.split(".")))
                > tuple(map(int, baseline.split("."))),
                f"Candidate sai {version} must be newer than baseline sai {baseline}",
            )
            if args.baseline is not None:
                verify_published_baseline(baseline, api_base)
            with reap_detached_children():
                return rehearse(
                    scratch,
                    wheel,
                    version,
                    baseline,
                    diagnostics,
                    strict=args.strict,
                )

    return report_result(work)


if __name__ == "__main__":
    sys.exit(main())
