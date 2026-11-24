"""Rehearse a sai-cli release wheel against the published v0.1.0 install."""

from __future__ import annotations

import argparse
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
from typing import IO, Any, NamedTuple

TESTS = Path(__file__).resolve().parents[1] / "cli" / "sai_cli" / "tests"
sys.path.insert(0, str(TESTS))
from loopback_keeper import KeeperHandler, keeper_server

BASELINE = "0.1.0"
REPO = "SaiDotFun/open-sai"
INSTALLER = (
    f"https://raw.githubusercontent.com/{REPO}/sai-cli/v{BASELINE}/scripts/install.py"
)
CLIENTS = ("claude", "codex")
SYNTHETIC_TOKEN = "release-rehearsal-fixture-only"
CHAIN = "cataclysm-1"
INTENT_QUERY = re.compile(
    r"\buserIntents\s*\(\s*where\s*:\s*\{\s*user\s*:\s*\$user\s*,?"
    r"\s*intentKey\s*:\s*\$key\s*\}"
)
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
        self.deferred = False

    def receive(self, signum: int, _frame: object) -> None:
        if self.signum is None:
            self.signum = signum
            if not self.deferred:
                raise RehearsalInterrupted(signum)

    @contextmanager
    def defer(self) -> Generator[None, None, None]:
        # Register cleanup before delivering a signal received during acquisition.
        self.deferred = True
        try:
            yield
        finally:
            self.deferred = False
            if self.signum is not None:
                raise RehearsalInterrupted(self.signum)


CANCELLATION = Cancellation()


@contextmanager
def cancel_on_signal() -> Generator[None, None, None]:
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, CANCELLATION.receive)
        yield
    finally:
        for sig, handler in previous.items():
            if handler is not None:
                signal.signal(sig, handler)
        CANCELLATION.signum = None


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RehearsalError(message)


class ReplyReader:
    def __init__(self, stream: IO[bytes], capture: bytearray | None = None) -> None:
        self.fd = stream.fileno()
        self.pending = bytearray()
        self.capture = capture
        os.set_blocking(self.fd, False)

    def line(self, step: str, *, timeout: float = 10) -> str:
        deadline = time.monotonic() + timeout
        while (end := self.pending.find(b"\n")) < 0:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.fd], [], [], left)[0]:
                raise RehearsalError(f"{step} timed out waiting for a complete reply")
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
    stdout: int | None = subprocess.PIPE,
    stderr: int | IO[bytes] | None = subprocess.PIPE,
) -> Generator[subprocess.Popen[bytes], None, None]:
    with ExitStack() as cleanup:
        with CANCELLATION.defer():
            process = cleanup.enter_context(
                subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            )
            cleanup.callback(stop_process_group, process)
        yield process


def stop_process_group(process: subprocess.Popen[bytes]) -> None:
    # The leader can exit while a descendant still owns a pipe or file.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


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
                probe.communicate(timeout=5)
                if probe.returncode == 0:
                    return candidate
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


class SelectedField(NamedTuple):
    name: str
    key: str
    children: tuple[SelectedField, ...]


def selection_set(query: str) -> tuple[SelectedField, ...]:
    """Parse the field selections used by the replay's inline operations."""
    tokens = deque(
        token
        for token in re.findall(r'#[^\n]*|"(?:\\.|[^"\\])*"|[_A-Za-z]\w*|[^\s,]', query)
        if not token.startswith("#")
    )

    def take_name() -> str:
        name = tokens.popleft()
        if not re.fullmatch(r"[_A-Za-z]\w*", name):
            raise ValueError("Expected a GraphQL field name")
        return name

    def skip_arguments() -> None:
        depth = 1
        tokens.popleft()
        while depth:
            token = tokens.popleft()
            depth += (token == "(") - (token == ")")

    def fields() -> tuple[SelectedField, ...]:
        tokens.popleft()
        result = []
        while tokens[0] != "}":
            key = name = take_name()
            if tokens[0] == ":":
                tokens.popleft()
                name = take_name()
            if tokens[0] == "(":
                skip_arguments()
            children = fields() if tokens[0] == "{" else ()
            result.append(SelectedField(name, key, children))
        tokens.popleft()
        return tuple(result)

    try:
        while tokens[0] != "{":
            if tokens[0] == "(":
                skip_arguments()
            else:
                take_name()
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
        self.latest = BASELINE
        self.submitted: list[str] = []
        self.lifecycle = "outcome_not_indexed"

    def add_release(self, wheel: Path, version: str) -> None:
        payload = wheel.read_bytes()
        sums = f"{hashlib.sha256(payload).hexdigest()}  {wheel.name}\n".encode()
        path = f"/{REPO}/releases/download/sai-cli/v{version}"
        self.assets[f"{path}/{wheel.name}"] = payload, "application/octet-stream"
        self.assets[f"{path}/SHA256SUMS"] = sums, "text/plain"

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
                    selected = selection_set(query)
                except ValueError:
                    self.send_error(400)
                    return
                roots = {field.name: field.children for field in selected}
                perp = {field.name for field in roots.get("perp", ())}
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
                    rows = []
                    user = variables.get("user")
                    key = variables.get("key")
                    if (
                        INTENT_QUERY.search(query)
                        and isinstance(user, str)
                        and user.lower() == replay.addr
                        and replay.submitted
                        and key == replay.submitted[0]
                    ):
                        row = replay.responses[replay.lifecycle]["data"]["userIntents"][
                            0
                        ]
                        rows.append({**row, "intentKey": key})
                    data = {"userIntents": rows}
                elif "tradeHistory" in perp:
                    data = {"perp": {"tradeHistory": []}}
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
            with owned_process(
                command, self.home, self.env if env is None else env
            ) as process:
                out, err = process.communicate(input_text.encode(), timeout=timeout)
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

    def verify_session(self, original: dict[str, str]) -> None:
        require(
            self.json("Read the stored session", "auth", "status") == original,
            "The stored session changed",
        )
        self.run(
            "Read the stored synthetic token",
            self.python,
            "-I",
            "-c",
            "import sys; from sai_cli.core.storage import load_session; "
            "sys.exit(load_session()['token'] != sys.stdin.read())",
            input_text=SYNTHETIC_TOKEN,
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
                        code = process.wait(timeout=5)
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


def ledger_row(ledger: Path, key: str, *, migrated: bool) -> tuple[Any, ...]:
    with closing(sqlite3.connect(f"{ledger.as_uri()}?mode=ro", uri=True)) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(intents)")}
        require(
            ("keeper_status" in columns) == migrated, "Unexpected intent ledger schema"
        )
        require(
            db.execute("PRAGMA user_version").fetchone() == (1,),
            "Unexpected ledger version",
        )
        states = "status, keeper_status" if migrated else "status"
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


def probe_retry(tool: Install, replay: Replay, key: str) -> bool:
    before = len(replay.submitted)
    result = tool.run(
        "Retry the synthetic open", tool.sai, "--json", *TRADE, check=False
    )
    if len(replay.submitted) == before + 1:
        require(
            result.returncode == 0, "The retry reached Keeper but was not acknowledged"
        )
        return False
    output = result.stdout + result.stderr
    require(
        len(replay.submitted) == before
        and result.returncode != 0
        and f"Reconcile uncertain intent {key}" in output,
        "The retry failed without the original intent's trade guard",
    )
    return True


def rehearse(root: Path, wheel: Path, version: str, diagnostics: Path | None) -> int:
    tool = Install(root, diagnostics)
    with keeper_server(Replay(wheel, version).handler()) as base:
        tool.run(
            "Install the published release",
            tool.uv,
            "run",
            INSTALLER,
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
    prefix = namespace_prefix(tool.home, tool.env)
    if prefix is None:
        print(
            "Loopback-only network namespace unavailable. Using the dead proxy for proxy-aware traffic.",
            file=sys.stderr,
            flush=True,
        )
        return rehearse_staged(tool, root, wheel, version)
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
        str(diagnostics) if diagnostics else "",
    )
    # Only the rehearsal script's own progress reaches these streams. It keeps
    # the installed CLI and agent clients' output behind its usual capture.
    with owned_process(command, tool.home, tool.env, stdout=None, stderr=None) as stage:
        return stage.wait()


def isolated_stage(root_name: str, wheel_name: str, version: str, log_name: str) -> int:
    root, wheel = Path(root_name), Path(wheel_name)
    tool = Install(root, Path(log_name) if log_name else None, prepared=True)
    with cancel_on_signal():
        return report_result(lambda: rehearse_staged(tool, root, wheel, version))


def rehearse_staged(tool: Install, root: Path, wheel: Path, version: str) -> int:
    replay = Replay(wheel, version)
    baseline_wheel = (
        tool.sai_home / "cache/wheels" / f"sai_cli-{BASELINE}-py3-none-any.whl"
    )
    replay.add_release(baseline_wheel, BASELINE)
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
        tool.verify_version(BASELINE)
        print(f"Installed published sai {BASELINE} at {tool.sai}", flush=True)

        tool.json(
            "Configure loopback Keeper", "config", "set", "keeper.endpoint", endpoint
        )
        tool.json("Disable auto-update", "config", "set", "update.auto", "false")
        tool.run(
            "Save the synthetic session",
            tool.python,
            "-I",
            "-c",
            "import json, sys; from sai_cli.core.storage import save_session; "
            "save_session(json.load(sys.stdin))",
            input_text=json.dumps(
                {
                    "evmAddress": replay.addr,
                    "endpoint": endpoint,
                    "token": SYNTHETIC_TOKEN,
                    "expiresAt": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
                }
            ),
        )
        session = tool.json("Read the baseline session", "auth", "status")
        config = tool.sai_home / "config.toml"
        configured = config.read_bytes()
        tool.json("Register MCP clients", "mcp", "install", *CLIENTS)
        tool.verify_mcp(BASELINE)

        first = tool.run(
            "Open against loopback Keeper", tool.sai, "--json", *TRADE, check=False
        )
        require(
            first.returncode == 1
            and len(replay.submitted) == 1
            and f"Outcome unknown for intent {replay.submitted[0]}" in first.stderr,
            f"The baseline did not report a lost acknowledgement (exit {first.returncode},"
            f" {len(replay.submitted)} Keeper submissions)",
        )
        key = replay.submitted[0]
        ledger = tool.sai_home / "trader/intents.sqlite3"
        require(
            ledger_row(ledger, key, migrated=False) == ("unknown",),
            "The baseline did not retain the unknown open",
        )
        require(probe_retry(tool, replay, key), "The baseline submitted a second open")

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
        tool.verify_session(session)
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
        finally:
            replay.lifecycle = "outcome_not_indexed"
            backup_ledger(checkpoint, ledger)
        print(
            "Doctor passed after Keeper reported a terminal outcome. Restored the pending ledger for downgrade.",
            flush=True,
        )

        tool.json(
            "Downgrade through sai update",
            "update",
            "--to",
            BASELINE,
            "--web-base",
            base,
        )
        tool.verify_version(BASELINE)
        require(config.read_bytes() == configured, "Downgrade changed the user config")
        tool.verify_session(session)
        tool.verify_mcp(BASELINE)
        local = tool.json(
            "Read the migrated ledger with the old CLI", "intent", "local", replay.addr
        )
        require(
            any(
                row["intentKey"] == key and row["status"] == "unknown" for row in local
            ),
            "The old CLI could not read the migrated pending open",
        )
        require(
            probe_retry(tool, replay, key),
            "The downgraded CLI submitted a second open before reconciliation",
        )
        print(
            f"Downgraded to {BASELINE}. Retry before reconciliation: blocked.",
            flush=True,
        )
        tool.json("Reconcile with the old CLI", *args)
        blocked = probe_retry(tool, replay, key)
        saved_status = ledger_row(ledger, key, migrated=True)[0]
        status = saved_status if saved_status in ("terminal", "unknown") else "other"
        print(
            f"v{BASELINE} reconciled market_order_outcome_not_indexed as {status}. "
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
        "wheel", type=Path, nargs="?", help="Candidate release wheel newer than 0.1.0"
    )
    parser.add_argument(
        "--self-test", action="store_true", help="Run local rehearsal tests"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Save child output in a private scratch log",
    )
    args = parser.parse_args()
    if args.self_test:
        if args.wheel:
            parser.error("--self-test takes no candidate wheel")
        suite = unittest.defaultTestLoader.discover(
            str(Path(__file__).resolve().parent), pattern="release_rehearsal_test.py"
        )
        return int(not unittest.TextTestRunner().run(suite).wasSuccessful())
    if args.wheel is None:
        parser.error("a candidate wheel is required")
    wheel = args.wheel.resolve()
    match = re.fullmatch(
        r"sai_cli-((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))-py3-none-any\.whl",
        wheel.name,
    )
    if (
        not match
        or tuple(map(int, match[1].split("."))) <= (0, 1, 0)
        or not wheel.is_file()
    ):
        parser.error(
            "provide an existing sai_cli-X.Y.Z-py3-none-any.whl newer than 0.1.0"
        )
    version = match[1]

    def work() -> int:
        diagnostics = None
        if args.verbose:
            directory = Path(tempfile.mkdtemp(prefix="sai-rehearsal-diagnostics-"))
            diagnostics = directory / "child-output.log"
            diagnostics.touch(mode=0o600)
            print(f"Child output log: {diagnostics}", file=sys.stderr)
        with cancel_on_signal(), ExitStack() as cleanup:
            with CANCELLATION.defer():
                scratch = cleanup.enter_context(
                    tempfile.TemporaryDirectory(
                        prefix="sai-release-rehearsal-",
                        dir=diagnostics.parent if diagnostics else None,
                    )
                )
            return rehearse(Path(scratch), wheel, version, diagnostics)

    return report_result(work)


if __name__ == "__main__":
    sys.exit(main())
