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
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Generator
from contextlib import closing, contextmanager, nullcontext, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

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


class RehearsalError(RuntimeError):
    pass


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
    stderr: int | IO[bytes] = subprocess.PIPE,
) -> Generator[subprocess.Popen[bytes], None, None]:
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
        start_new_session=True,
    ) as process:
        try:
            yield process
        finally:
            # The leader can exit while a descendant still owns a pipe or file.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


class Replay:
    def __init__(self, wheel: Path, version: str) -> None:
        fixture = json.loads((TESTS / "fixtures" / "intent_outcomes.json").read_text())
        self.addr = fixture["address"]
        self.responses = {**fixture["recorded"], **fixture["replays"]}
        self.wheel = wheel.read_bytes()
        self.sums = f"{hashlib.sha256(self.wheel).hexdigest()}  {wheel.name}\n".encode()
        self.latest = BASELINE
        self.release_path = f"/{REPO}/releases/download/sai-cli/v{version}/"
        self.wheel_name = wheel.name
        self.submitted: list[str] = []
        self.lifecycle = "outcome_not_indexed"

    def handler(self) -> type[KeeperHandler]:
        replay = self

        class Handler(KeeperHandler):
            def do_GET(self) -> None:
                if self.path.startswith(f"/repos/{REPO}/releases?"):
                    self.reply([{"tag_name": f"sai-cli/v{replay.latest}"}])
                elif self.path == replay.release_path + replay.wheel_name:
                    self.send_body(replay.wheel, "application/octet-stream")
                elif self.path == replay.release_path + "SHA256SUMS":
                    self.send_body(replay.sums, "text/plain")
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
                if "recordUserIntent" in query:
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
                    self.reply(
                        {
                            "data": {
                                "recordUserIntent": {
                                    "intentKey": key,
                                    "evmAddress": replay.addr,
                                    "lifecycleStep": "jwt_pass",
                                    "execActor": "server",
                                }
                            }
                        }
                    )
                elif "userIntents" in query:
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
                    self.reply({"data": {"userIntents": rows}})
                elif "tradeHistory" in query:
                    self.reply({"data": {"perp": {"tradeHistory": []}}})
                elif "oneClickTrading" in query:
                    self.reply(
                        {
                            "data": {
                                "oneClickTrading": {
                                    "enabled": True,
                                    "chainId": CHAIN,
                                    "contractAddress": "nibi1x",
                                }
                            }
                        }
                    )
                elif "borrowings" in query:
                    self.reply(
                        {
                            "data": {
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
                        }
                    )
                else:
                    self.send_error(400)

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
    def __init__(self, root: Path, diagnostics: Path | None = None) -> None:
        self.diagnostics = diagnostics
        self.home = root / "home"
        self.home.mkdir(mode=0o700)
        (self.home / ".codex").mkdir(mode=0o700)
        self.sai_home = self.home / ".sai"
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        self.sai = bin_dir / "sai"
        self.uv = bin_dir / "uv"
        self.python = self.home / ".local/share/uv/tools/sai-cli/bin/python"
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
    ) -> subprocess.CompletedProcess[str]:
        try:
            with owned_process(command, self.home, self.env) as process:
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


def rehearse(root: Path, wheel: Path, version: str, diagnostics: Path | None) -> None:
    tool = Install(root, diagnostics)
    replay = Replay(wheel, version)
    with keeper_server(replay.handler()) as base:
        endpoint = f"{base}/query"
        tool.run(
            "Install the published release",
            tool.uv,
            "run",
            INSTALLER,
            "--api-base",
            base,
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
        tool.verify_session(session)
        tool.verify_mcp(version)
        args = ("intent", "status", replay.addr, "--key", key)
        status = tool.json("Reconcile with the candidate", *args)
        require(
            status["outcome"] == "pending"
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
        finally:
            replay.lifecycle = "outcome_not_indexed"
            backup_ledger(checkpoint, ledger)
        print(
            "Doctor passed after Keeper reported a terminal outcome. Restored the pending ledger for downgrade.",
            flush=True,
        )

        tool.json("Downgrade through sai update", "update", "--to", BASELINE)
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
    try:
        diagnostics = None
        if args.verbose:
            directory = Path(tempfile.mkdtemp(prefix="sai-rehearsal-diagnostics-"))
            diagnostics = directory / "child-output.log"
            diagnostics.touch(mode=0o600)
            print(f"Child output log: {diagnostics}", file=sys.stderr)
        with tempfile.TemporaryDirectory(
            prefix="sai-release-rehearsal-",
            dir=diagnostics.parent if diagnostics else None,
        ) as scratch:
            rehearse(Path(scratch), wheel, match[1], diagnostics)
    except RehearsalError as exc:
        print(f"Release rehearsal failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"Release rehearsal failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
