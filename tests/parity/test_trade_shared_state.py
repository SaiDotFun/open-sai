"""Shared-home contention and restart against a loopback Keeper."""

from __future__ import annotations

import asyncio
import json
import os
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from keeper_wire import WireRequest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent
from network import require_loopback, restricted, unroutable_packets
from scenarios import ADDRESS, OTHER_ADDRESS, Endpoints, child_env, loopback, seed_home
from trade_replay import REQUEST_HASH, SESSION, TradeReplay

pytestmark = pytest.mark.parity_namespace

REFERENCE = "973f3f59f38dd89d0c2e03e674157934a00316ae"
KEY = "20261124T120000000Z_0123456789ABCDEF"
RETRY = "20261124T120001000Z_0123456789ABCDEF"
LEDGER = Path("sai/trader/intents.sqlite3")
PY_RESERVE = """
import os
import sys
from sai_cli.core import log, storage

sys.stdin.buffer.read(1)
try:
    storage.pending(
        os.environ["SAI_TEST_RESERVE_KEY"],
        os.environ["SAI_TEST_RESERVE_ENDPOINT"],
        os.environ["SAI_TEST_RESERVE_ACCOUNT"],
        "CLOSE_POSITION",
        "7",
    )
except storage.UnresolvedIntent as exc:
    print(f"blocked by {exc.intent_key}", file=sys.stderr, flush=True)
    sys.exit(4)
log.flush()
print("reserved", file=sys.stderr, flush=True)
if "SAI_TEST_RESERVE_WAIT" in os.environ:
    sys.stdin.buffer.read(1)
"""


@pytest.fixture(autouse=True)
def loopback_only() -> Generator[None]:
    require_loopback()
    before = unroutable_packets()
    yield
    assert unroutable_packets() == before, (
        "A shared-state subprocess attempted network egress"
    )


@pytest.fixture(scope="module")
def python_reference() -> Path | None:
    path = os.environ.get("SAI_PYTHON_REFERENCE")
    if path is None:
        return None
    project = Path(path).resolve()
    head = subprocess.check_output(
        ["git", "-C", project, "rev-parse", "HEAD"], text=True, timeout=5
    ).strip()
    assert head == REFERENCE, f"Shared-state Python fixture requires {REFERENCE}"
    subprocess.run(
        [
            "git",
            "-C",
            project,
            "diff",
            "--exit-code",
            head,
            "--",
            "cli",
            "pyproject.toml",
            "uv.lock",
        ],
        capture_output=True,
        check=True,
        timeout=5,
    )
    python = project / ".venv/bin/python"
    assert os.access(python, os.X_OK), "Run uv sync --locked in the reference checkout"
    return python


@dataclass(frozen=True)
class Runtime:
    name: str
    cli: tuple[str, ...]
    main_cli: tuple[str, ...] | None = None

    @property
    def rust(self) -> bool:
        return self.name == "rust"

    @property
    def main(self) -> Runtime | None:
        if self.main_cli is not None:
            return Runtime("rust", self.main_cli)
        return None


def runtime(name: str, request: pytest.FixtureRequest, python: Path | None) -> Runtime:
    if name == "rust":
        if python is None:
            pytest.skip(f"set SAI_PYTHON_REFERENCE to a checkout of {REFERENCE}")
        _, native = request.getfixturevalue("cli_pair")
        main = os.environ.get("SAI_PARITY_MAIN_BIN")
        if main is None:
            assert os.environ.get("GITHUB_ACTIONS") != "true", (
                "Missing Rust main baseline"
            )
            return Runtime(name, native)
        executable = shutil.which(main)
        assert executable is not None, "SAI_PARITY_MAIN_BIN must name an executable"
        return Runtime(name, native, (executable,))
    return Runtime(name, (str(python or sys.executable), "-m", "sai_cli.sai_cli"))


@dataclass(frozen=True)
class Home:
    path: Path
    endpoints: Endpoints

    @property
    def env(self) -> dict[str, str]:
        return {**child_env(self.path, self.endpoints), "SAI_UPDATE_AUTO": "0"}

    def invoke(self, runner: Runtime, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            restricted([*runner.cli, *args]),
            cwd=self.path,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def json(self, runner: Runtime, *args: str, code: int = 0) -> Any:
        result = self.invoke(runner, "--json", *args)
        assert result.returncode == code, (
            runner.name,
            args,
            result.stdout,
            result.stderr,
        )
        return json.loads(result.stdout)

    def local(self, runner: Runtime, key: str, kind: str, status: str) -> None:
        assert self.json(runner, "intent", "local", ADDRESS) == [
            {"intentKey": key, "kind": kind, "status": status}
        ]

    def row(self, key: str, kind: str, entity: str, status: str) -> None:
        with closing(
            sqlite3.connect((self.path / LEDGER).as_uri() + "?mode=ro", uri=True)
        ) as db:
            assert db.execute("PRAGMA user_version").fetchone() == (1,)
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            rows = db.execute(
                "SELECT id, endpoint, account, kind, entity, status, keeper_status, "
                "typeof(created_at) FROM intents"
            ).fetchall()
        stored = "unknown" if status == "pending" else status
        outcome = status if status in {"pending", "terminal"} else None
        assert rows == [
            (key, self.endpoints.keeper, ADDRESS, kind, entity, stored, outcome, "real")
        ]
        assert (self.path / LEDGER).stat().st_mode & 0o777 == 0o600

    def ledger_rows(self) -> list[tuple[Any, ...]] | None:
        path = self.path / LEDGER
        if not path.exists():
            return None
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute("SELECT * FROM intents ORDER BY id").fetchall()

    def events(self, key: str) -> list[dict[str, Any]]:
        events = []
        sequences: dict[str, list[int]] = defaultdict(list)
        for path in sorted((self.path / "sai/logs").glob("*.jsonl")):
            for line in path.read_text().splitlines():
                assert SESSION["token"] not in line
                event = json.loads(line)
                sequences[event["runId"]].append(event["seq"])
                if event.get("intentKey") == key:
                    events.append(event)
        assert all(seq == list(range(1, len(seq) + 1)) for seq in sequences.values())
        return sorted(events, key=lambda event: (event["ts"], event["seq"]))


@contextmanager
def reservation(
    home: Home,
    runner: Runtime,
    key: str,
    *,
    rust_helper: str,
    pause: bool = False,
) -> Generator[subprocess.Popen[bytes]]:
    args = (
        [rust_helper, "--exact", "reservation_subprocess", "--nocapture"]
        if runner.rust
        else [runner.cli[0], "-c", PY_RESERVE]
    )
    env = {
        **home.env,
        "SAI_TEST_RESERVE_KEY": key,
        "SAI_TEST_RESERVE_ENDPOINT": home.endpoints.keeper,
        "SAI_TEST_RESERVE_ACCOUNT": ADDRESS,
    }
    if pause:
        env["SAI_TEST_RESERVE_WAIT"] = "1"
    with subprocess.Popen(
        restricted(args),
        cwd=home.path,
        env=env,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    ) as proc:
        assert proc.stdin is not None
        try:
            proc.stdin.write(b"1")
            proc.stdin.flush()
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


@pytest.mark.parametrize("writer_name", ["python", "rust"])
def test_restart_after_reservation(
    writer_name: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    python_reference: Path | None,
) -> None:
    executable = os.environ.get("SAI_PARITY_RESERVE_BIN")
    if executable is None:
        pytest.skip(
            "set SAI_PARITY_RESERVE_BIN to the sai-core storage test executable"
        )
    rust_helper = shutil.which(executable)
    assert rust_helper is not None, "SAI_PARITY_RESERVE_BIN must name an executable"
    rust = runtime("rust", request, python_reference)
    python = runtime("python", request, python_reference)
    writer, reader = (rust, python) if writer_name == "rust" else (python, rust)
    replay = TradeReplay(reserved_key=KEY)
    with loopback("default", trade_replay=replay) as endpoints:
        home = Home(tmp_path / "home", endpoints)
        seed_home(home.path, endpoints, "session")
        with reservation(
            home, writer, KEY, rust_helper=rust_helper, pause=True
        ) as proc:
            assert proc.stderr is not None
            assert select.select([proc.stderr], [], [], 8)[0], "No reservation signal"
            assert proc.stderr.readline() == b"reserved\n"
            home.row(KEY, replay.kind, replay.entity, "unknown")
            assert proc.poll() is None
            proc.kill()
            assert proc.wait(timeout=5) == -signal.SIGKILL

        home.local(reader, KEY, replay.kind, "unknown")
        before = home.json(reader, "intent", "status", ADDRESS, "--key", KEY)
        assert before == {"userIntents": [], "localStatus": "unknown"}
        with reservation(home, reader, RETRY, rust_helper=rust_helper) as proc:
            _, err = proc.communicate(timeout=8)
            assert proc.returncode == 4 and err == f"blocked by {KEY}\n".encode()

        # Only an empty Keeper read after the admission grace period releases
        # the block. Age the fixture row instead of waiting five minutes.
        with closing(sqlite3.connect(home.path / LEDGER)) as db, db:
            db.execute(
                "UPDATE intents SET created_at = ? WHERE id = ?",
                (time.time() - 301, KEY),
            )
        after = home.json(reader, "intent", "status", ADDRESS, "--key", KEY)
        assert after == {"userIntents": [], "localStatus": "absent"}
        home.row(KEY, replay.kind, replay.entity, "absent")
        for runner in (reader, writer):
            home.local(runner, KEY, replay.kind, "absent")
        assert replay.attempts() == {} and replay.admitted_keys() == set()
        assert not endpoints.errors
        assert [wire.label for wire in endpoints.wire] == ["userIntents", "userIntents"]
        assert [
            (event["previous"], event["status"])
            for event in home.events(KEY)
            if event["event"] == "intent.status"
        ] == [("unknown", "absent")]
        with reservation(home, reader, RETRY, rust_helper=rust_helper) as proc:
            _, err = proc.communicate(timeout=8)
            assert proc.returncode == 0 and err == b"reserved\n"


@dataclass(frozen=True)
class Action:
    args: tuple[str, ...]
    kind: str
    change: str
    entity: str = "7"
    server: str = "default"
    outcome: str = "succeeded"

    def replay(self, **options: Any) -> TradeReplay:
        return TradeReplay(self.kind, self.entity, self.change, **options)


ACTIONS = {
    "open": Action(
        (
            "open",
            "--market",
            "3",
            "--side",
            "long",
            "--type",
            "market",
            "--amount",
            "20",
            "--leverage",
            "2",
            "--slippage",
            "1",
        ),
        "OPEN_TRADE",
        "position_opened",
        entity="",
        outcome="filled",
    ),
    "close": Action(
        ("close", "7"), "CLOSE_POSITION", "position_closed_user", outcome="filled"
    ),
    "cancel": Action(
        ("close", "7", "--cancel-order"),
        "CANCEL_PENDING_TRADE",
        "order_closed_user",
        server="pending-order",
    ),
    "margin": Action(
        ("margin", "7", "--direction", "add", "--amount", "1"),
        "UPDATE_TRADE",
        "leverage_updated",
    ),
    "partial-close": Action(
        ("partial-close", "7", "--percent", "50"),
        "DECREASE_POSITION_SIZE",
        "trade_size_decrease",
        outcome="filled",
    ),
    "tpsl": Action(
        ("tpsl", "7", "--take-profit", "110", "--clear-stop-loss"),
        "UPDATE_TRADE",
        "tpsl_updated",
    ),
    "leverage": Action(
        ("leverage", "7", "--leverage", "5"),
        "UPDATE_TRADE",
        "leverage_updated",
    ),
}
ACTION_REASONS = {
    "open": "pull/117",
    "close": "pull/117",
    "cancel": "pull/117",
    "margin": "pull/118",
    "partial-close": "pull/118",
    "tpsl": "issues/173",
    "leverage": "pull/181",
}
REPO_URL = "https://github.com/SaiDotFun/open-sai"


def command_help(
    home: Home, runner: Runtime, group: str, command: str, flag: str | None = None
) -> str | None:
    result = home.invoke(runner, group, command, "--help")
    if (
        runner.rust
        and result.returncode == 2
        and any(
            f"unrecognized subcommand '{name}'" in result.stderr
            or f"Error: No such command '{name}'." in result.stderr.splitlines()
            for name in (group, command)
        )
    ):
        help_text = None
    else:
        assert result.returncode == 0 and result.stdout, (result.stdout, result.stderr)
        help_text = result.stdout
    if help_text is not None and (flag is None or flag in help_text):
        return help_text
    if (main := runner.main) is not None:
        assert command_help(home, main, group, command, flag) is None, (
            f"Rust candidate dropped shipped {group} {command} {flag or ''}".rstrip()
        )
    return None


def trade_help(home: Home, runner: Runtime, action_name: str) -> str:
    action = ACTIONS[action_name]
    flag = "--cancel-order" if action_name == "cancel" else None
    help_text = command_help(home, runner, "trade", action.args[0], flag)
    if help_text is None:
        assert runner.rust
        pytest.skip(
            f"Rust trade {action_name} awaits {REPO_URL}/{ACTION_REASONS[action_name]}"
        )
    return help_text


def assert_blocked(result: subprocess.CompletedProcess[str], key: str) -> None:
    assert result.returncode == 1 and not result.stderr, result
    failure = json.loads(result.stdout)["error"]
    assert failure["retryable"] is False
    assert key in failure.get("intentKeys", [failure.get("intentKey")])


def assert_trade_events(
    home: Home, key: str, statuses: list[str], *, acked: bool
) -> None:
    events = home.events(key)
    names = [event["event"] for event in events]
    assert names.count("intent.reserved") == 1
    assert names.count("intent.submitted") == int(acked)
    assert [
        event["status"] for event in events if event["event"] == "intent.status"
    ] == statuses


@pytest.mark.parametrize("action_name", ACTIONS)
@pytest.mark.parametrize(
    ("submitter", "reconciler", "retry_runtime"),
    [
        ("python", "python", "python"),
        ("python", "rust", "python"),
        ("python", "rust", "rust"),
        ("rust", "python", "python"),
    ],
)
def test_lost_admission_blocks_until_reconciliation(
    action_name: str,
    submitter: str,
    reconciler: str,
    retry_runtime: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    python_reference: Path | None,
) -> None:
    writer = runtime(submitter, request, python_reference)
    reader = runtime(reconciler, request, python_reference)
    retry = runtime(retry_runtime, request, python_reference)
    action = ACTIONS[action_name]
    replay = action.replay(lose_reply=True)
    with loopback(action.server, trade_replay=replay) as endpoints:
        home = Home(tmp_path / "home", endpoints)
        seed_home(home.path, endpoints, "session")
        trade_help(home, writer, action_name)
        if retry_runtime != submitter:
            trade_help(home, retry, action_name)
        args = ("trade", *action.args)
        error = home.json(writer, *args, code=5)["error"]
        key = error["intentKey"]
        assert error["code"] == "OUTCOME_UNKNOWN" and error["retryable"] is False
        home.local(reader, key, action.kind, "unknown")
        home.row(key, action.kind, action.entity, "unknown")
        assert_blocked(home.invoke(retry, "--json", *args), key)
        assert replay.attempts() == {key: 1}

        replay.phase = "pending"
        status = home.json(reader, "intent", "status", ADDRESS, "--key", key)
        assert status["outcome"] == "pending"
        home.local(writer, key, action.kind, "pending")
        home.row(key, action.kind, action.entity, "pending")
        assert_blocked(home.invoke(retry, "--json", *args), key)

        replay.phase = "filled"
        status = home.json(reader, "intent", "status", ADDRESS, "--key", key)
        assert status["matchedTradeHistory"][0]["intentKey"] == key
        assert status["matchedTradeHistory"][0]["tradeChangeType"] == action.change
        home.local(writer, key, action.kind, "terminal")
        home.row(key, action.kind, action.entity, "terminal")
        assert replay.attempts() == {key: 1} and replay.admitted_keys() == {key}
        assert not endpoints.errors
        assert_trade_events(home, key, ["pending", "terminal"], acked=False)


@pytest.mark.parametrize("action_name", ACTIONS)
@pytest.mark.parametrize("submitter", ["python", "rust"])
def test_trade_wait_replays_pending_then_receipt(
    action_name: str,
    submitter: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    python_reference: Path | None,
) -> None:
    writer = runtime(submitter, request, python_reference)
    action = ACTIONS[action_name]
    replay = action.replay(phase="pending", fill_on_second_poll=True)
    with loopback(action.server, trade_replay=replay) as endpoints:
        home = Home(tmp_path / "home", endpoints)
        seed_home(home.path, endpoints, "session")
        trade_help(home, writer, action_name)
        if command_help(home, writer, "trade", action.args[0], "--wait") is None:
            assert writer.rust
            pytest.skip(f"Rust trade --wait awaits {REPO_URL}/pull/122")
        result = home.json(writer, "trade", *action.args, "--wait")
        key = result["intentKey"]
        assert result["outcome"] == action.outcome
        assert result["addr"] == ADDRESS
        receipt = result["receipt"]
        assert receipt["requestTxHash"] == REQUEST_HASH
        assert receipt["requestBlock"] == 121
        assert receipt["action"]["kind"] == action.kind
        assert receipt["execution"]["source"] == "trade_history"
        assert receipt["execution"]["block"] == 122
        assert receipt["execution"]["tradeChangeType"] == action.change
        assert receipt["execution"]["marketOrderSequence"] == 49
        if action.outcome == "filled":
            assert receipt["fill"] == {
                "price": None,
                "sizeCollateralBaseUnits": None,
                "oraclePrice": "100.25",
                "escrowedCollateralBaseUnits": "4000000",
            }
        else:
            assert receipt["fill"] is None
        home.row(key, action.kind, action.entity, "terminal")
        assert replay.attempts() == {key: 1} and replay.admitted_keys() == {key}
        assert not endpoints.errors
        assert [
            item.label
            for item in endpoints.wire
            if item.label
            in {"recordUserIntent", "waitUserIntents", "intentTradeHistory"}
        ] == [
            "recordUserIntent",
            "waitUserIntents",
            "waitUserIntents",
            "intentTradeHistory",
        ]
        assert_trade_events(home, key, ["accepted", "pending", "terminal"], acked=True)


@pytest.mark.parametrize(
    ("submitter", "waiter"),
    [("python", "python"), ("python", "rust"), ("rust", "python")],
)
def test_wait_deadline_then_restart(
    submitter: str,
    waiter: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    python_reference: Path | None,
) -> None:
    writer = runtime(submitter, request, python_reference)
    reader = runtime(waiter, request, python_reference)
    action = ACTIONS["close"]
    replay = action.replay(phase="pending")
    release_wait = threading.Event()

    def hold_wait(request: WireRequest, auth: str | None) -> dict[str, Any] | None:
        if request.label == "waitUserIntents" and not release_wait.wait(10):
            raise ValueError("Test did not release the outcome reply")
        return replay(request, auth)

    with loopback(action.server, trade_replay=hold_wait) as endpoints:
        home = Home(tmp_path / "home", endpoints)
        seed_home(home.path, endpoints, "session")
        trade_help(home, writer, "close")
        if command_help(home, reader, "intent", "wait") is None:
            pytest.skip(f"Rust intent wait awaits {REPO_URL}/pull/122")
        first = home.json(writer, "trade", *action.args)
        key = first["intentKey"]
        pending = home.json(reader, "intent", "status", ADDRESS, "--key", key)
        assert pending["localStatus"] == "pending"
        home.row(key, action.kind, action.entity, "pending")
        try:
            timed_out = home.json(
                reader, "intent", "wait", key, "--timeout", "1", code=5
            )
        finally:
            release_wait.set()
        assert timed_out["intentKey"] == key and timed_out["outcome"] == "unknown"
        assert timed_out["receipt"]["execution"] is None
        home.local(writer, key, action.kind, "pending")
        assert_blocked(home.invoke(writer, "--json", "trade", *action.args), key)

        replay.phase = "filled"
        receipt = home.json(reader, "intent", "wait", key, "--timeout", "5")
        assert receipt["intentKey"] == key and receipt["outcome"] == "filled"
        assert receipt["receipt"]["execution"]["tradeChangeType"] == action.change
        home.row(key, action.kind, action.entity, "terminal")
        assert replay.attempts() == {key: 1} and replay.admitted_keys() == {key}
        assert not endpoints.errors
        assert_trade_events(home, key, ["accepted", "pending", "terminal"], acked=True)


@pytest.mark.parametrize(
    ("submitter", "reconciler"), [("python", "rust"), ("rust", "python")]
)
def test_submission_times_out_after_keeper_receives_mutation(
    submitter: str,
    reconciler: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    python_reference: Path | None,
) -> None:
    # Exercise the real transport deadline in the pinned mixed-runtime run.
    reader = runtime(reconciler, request, python_reference)
    writer = runtime(submitter, request, python_reference)
    replay = TradeReplay(hold_admission=True)
    with loopback("default", trade_replay=replay) as endpoints:
        home = Home(tmp_path / "home", endpoints)
        seed_home(home.path, endpoints, "session")
        trade_help(home, writer, "close")
        started = time.monotonic()
        try:
            error = home.json(writer, "trade", "close", "7", code=5)["error"]
            assert replay.arrived.is_set() and time.monotonic() - started >= 15
            assert error["code"] == "OUTCOME_UNKNOWN" and error["retryable"] is False
            key = error["intentKey"]
            home.row(key, replay.kind, replay.entity, "unknown")
            home.local(reader, key, replay.kind, "unknown")
            assert_blocked(home.invoke(writer, "--json", "trade", "close", "7"), key)
        finally:
            replay.release.set()
        replay.phase = "pending"
        status = home.json(reader, "intent", "status", ADDRESS, "--key", key)
        assert status["localStatus"] == "pending"
        home.row(key, replay.kind, replay.entity, "pending")
        assert replay.attempts() == {key: 1} and replay.admitted_keys() == {key}
        assert not endpoints.errors
        assert_trade_events(home, key, ["pending"], acked=False)


@asynccontextmanager
async def mcp_session(home: Home, runner: Runtime) -> AsyncGenerator[ClientSession]:
    command, *args = restricted([*runner.cli, "mcp", "serve"])
    params = StdioServerParameters(
        command=command,
        args=args,
        cwd=home.path,
        env=home.env,
    )
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with (
            stdio_client(params, errlog=stderr) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            async with asyncio.timeout(10):
                await session.initialize()
            yield session


async def tool(session: ClientSession, name: str, **params: Any) -> dict[str, Any]:
    async with asyncio.timeout(12):
        result = await session.call_tool(name, params)
    [item] = result.content
    assert isinstance(item, TextContent)
    return json.loads(item.text)


def rust_mcp_stub(runner: Runtime, name: str, result: dict[str, Any]) -> bool:
    # #120 exposes tool schemas before the signed-in handlers are wired.
    return runner.rust and result.get("error") == (
        f"Tool {name} is not implemented in the Rust Sai server"
    )


def require_mcp(home: Home, runner: Runtime) -> None:
    if command_help(home, runner, "mcp", "serve") is None:
        pytest.skip(f"Rust MCP stdio awaits {REPO_URL}/pull/120")


async def tool_names(session: ClientSession) -> set[str]:
    async with asyncio.timeout(10):
        return {item.name for item in (await session.list_tools()).tools}


async def assert_mcp_pending(
    home: Home,
    runner: Runtime,
    names: set[str],
    *,
    stub_args: dict[str, Any] | None = None,
) -> None:
    main = runner.main
    if main is None or command_help(home, main, "mcp", "serve") is None:
        return
    async with mcp_session(home, main) as baseline:
        shipped = names & await tool_names(baseline)
        if stub_args is None:
            assert not shipped, (
                f"Rust candidate dropped shipped MCP tools {sorted(shipped)}"
            )
        elif shipped:
            [name] = shipped
            safe_args = stub_args.copy()
            if "preview_digest" in safe_args:
                # A bad digest probes whether the handler is live without authorizing a trade.
                safe_args["preview_digest"] = "0" * 64
            result = await tool(baseline, name, **safe_args)
            assert rust_mcp_stub(main, name, result), (
                f"Rust candidate stubbed shipped MCP tool {name}"
            )


def mutation_state(home: Home, replay: TradeReplay) -> tuple[object, ...]:
    return replay.attempts(), replay.admitted_keys(), home.ledger_rows()


async def trade_tool(
    session: ClientSession,
    home: Home,
    replay: TradeReplay,
    runner: Runtime,
    name: str,
    **params: Any,
) -> dict[str, Any]:
    before = mutation_state(home, replay)
    result = await tool(session, name, **params)
    if rust_mcp_stub(runner, name, result):
        assert mutation_state(home, replay) == before, (
            f"MCP stub {name} changed trade state"
        )
        await assert_mcp_pending(home, runner, {name}, stub_args=params)
        assert mutation_state(home, replay) == before, (
            f"MCP baseline probe for {name} changed trade state"
        )
    return result


MCP_ARGS: dict[str, dict[str, Any]] = {
    "open": {
        "market_id": 3,
        "side": "long",
        "order_type": "market",
        "amount": "20",
        "leverage": "2",
        "slippage": "1",
    },
    "close": {"trade_id": 7},
    "cancel": {"trade_id": 7, "cancel_order": True},
    "margin": {"trade_id": 7, "direction": "add", "amount": "1"},
    "partial-close": {"trade_id": 7, "percent": 50},
    "tpsl": {"trade_id": 7, "take_profit": "110", "clear_stop_loss": True},
    "leverage": {"trade_id": 7, "leverage": "5"},
}


def mcp_action(action_name: str) -> str:
    return "close" if action_name == "cancel" else action_name.replace("-", "_")


async def mcp_preview(
    session: ClientSession,
    home: Home,
    replay: TradeReplay,
    runner: Runtime,
    action_name: str,
) -> dict[str, Any] | None:
    name = mcp_action(action_name)
    preview_name = f"preview_{name}"
    before = mutation_state(home, replay)
    missing = {preview_name, f"trade_{name}"} - await tool_names(session)
    if missing:
        assert runner.rust, f"Python MCP tools missing: {sorted(missing)}"
        await assert_mcp_pending(home, runner, missing)
        assert mutation_state(home, replay) == before
        return None
    preview = await trade_tool(
        session, home, replay, runner, preview_name, **MCP_ARGS[action_name]
    )
    if rust_mcp_stub(runner, preview_name, preview):
        return None
    assert preview["addr"] == ADDRESS and preview["dryRun"] is True
    assert preview["intent"]["kind"] == ACTIONS[action_name].kind
    assert preview["previewDigest"]
    return preview


@pytest.mark.parametrize("first_surface", ["cli", "mcp"])
@pytest.mark.parametrize("action_name", ACTIONS)
@pytest.mark.parametrize(
    ("cli_name", "mcp_name"),
    [("python", "python"), ("python", "rust"), ("rust", "python")],
)
def test_cli_and_mcp_overlap(
    first_surface: str,
    action_name: str,
    cli_name: str,
    mcp_name: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    python_reference: Path | None,
) -> None:
    cli = runtime(cli_name, request, python_reference)
    mcp = runtime(mcp_name, request, python_reference)
    reconciler = runtime(
        "rust" if python_reference is not None else "python", request, python_reference
    )
    action = ACTIONS[action_name]
    replay = action.replay(hold_admission=True)
    tool_name = f"trade_{mcp_action(action_name)}"
    with loopback(action.server, trade_replay=replay) as endpoints:
        home = Home(tmp_path / "home", endpoints)
        seed_home(home.path, endpoints, "session")
        trade_help(home, cli, action_name)
        require_mcp(home, mcp)
        admitted_by_cli: str | None = None

        async def overlap() -> str | None:
            nonlocal admitted_by_cli
            async with mcp_session(home, mcp) as session:
                preview = await mcp_preview(session, home, replay, mcp, action_name)
                if preview is None:
                    return None
                params = {
                    **MCP_ARGS[action_name],
                    "preview_digest": preview["previewDigest"],
                }
                if action_name == "open":
                    params["reference_price"] = preview["referencePrice"]

                async def cli_trade() -> dict[str, Any]:
                    result = await asyncio.to_thread(
                        home.invoke, cli, "--json", "trade", *action.args
                    )
                    assert result.returncode in (0, 1) and not result.stderr, result
                    data = json.loads(result.stdout)
                    return data["error"] if result.returncode else data

                async def mcp_trade() -> dict[str, Any]:
                    return await trade_tool(
                        session, home, replay, mcp, tool_name, **params
                    )

                start, contend = (
                    (cli_trade, mcp_trade)
                    if first_surface == "cli"
                    else (mcp_trade, cli_trade)
                )
                first = asyncio.create_task(start())
                arrived = asyncio.create_task(asyncio.to_thread(replay.arrived.wait, 8))
                try:
                    ready, _ = await asyncio.wait(
                        (first, arrived), return_when=asyncio.FIRST_COMPLETED
                    )
                    if first in ready:
                        result = first.result()
                        if rust_mcp_stub(mcp, tool_name, result):
                            return None
                        pytest.fail(
                            f"{first_surface} exited before admission: {result}"
                        )
                    assert arrived.result(), f"{first_surface} did not reach Keeper"
                    [key] = replay.admitted_keys()
                    if first_surface == "cli":
                        admitted_by_cli = key
                    home.row(key, replay.kind, replay.entity, "unknown")
                    sent = replay.intent(key).copy()
                    del sent["intentSchemaVersion"], sent["tsIntentInit"]
                    expected_intent = preview["intent"]
                    if action_name == "open":
                        expected_intent = {
                            **expected_intent,
                            "body": {
                                **expected_intent["body"],
                                "intentPrice": preview["referencePrice"],
                            },
                        }
                    assert sent == expected_intent
                    blocked = await contend()
                    if rust_mcp_stub(mcp, tool_name, blocked):
                        return None
                    expected = (
                        "RECENT_OPEN_PENDING"
                        if action_name == "open"
                        else "INVALID_REQUEST"
                    )
                    assert blocked["code"] == expected, blocked
                    assert key in blocked.get("message", blocked.get("error", ""))
                    if "intentKey" in blocked:
                        assert blocked["intentKey"] == key
                    assert replay.attempts() == {key: 1}
                finally:
                    replay.release.set()
                    await arrived
                    result = await first
                assert result["intentKey"] == key and result["status"] == "accepted"
                return key

        key = asyncio.run(overlap())
        if key is None:
            expected = {admitted_by_cli: 1} if admitted_by_cli is not None else {}
            assert replay.attempts() == expected and replay.admitted_keys() == set(
                expected
            )
            assert not endpoints.errors
            pytest.skip(
                f"Rust MCP {mcp_action(action_name)} awaits {REPO_URL}/issues/176"
            )
        home.row(key, replay.kind, replay.entity, "accepted")
        replay.phase = "pending"
        home.json(reconciler, "intent", "status", ADDRESS, "--key", key)
        home.row(key, replay.kind, replay.entity, "pending")
        assert replay.attempts() == {key: 1} and replay.admitted_keys() == {key}
        assert not endpoints.errors
        assert_trade_events(home, key, ["accepted", "pending"], acked=True)


@pytest.mark.parametrize(
    ("mcp_name", "switcher"),
    [("python", "python"), ("python", "rust"), ("rust", "python")],
)
def test_mcp_confirmation_rechecks_active_account(
    mcp_name: str,
    switcher: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    python_reference: Path | None,
) -> None:
    mcp = runtime(mcp_name, request, python_reference)
    cli = runtime(switcher, request, python_reference)
    replay = TradeReplay()
    with loopback("confirmation-account-change", trade_replay=replay) as endpoints:
        home = Home(tmp_path / "home", endpoints)
        seed_home(home.path, endpoints, "accounts")
        require_mcp(home, mcp)
        home.json(cli, "auth", "use", ADDRESS)

        async def confirm() -> bool:
            async with mcp_session(home, mcp) as session:
                preview = await mcp_preview(session, home, replay, mcp, "close")
                if preview is None:
                    return False
                await asyncio.to_thread(home.json, cli, "auth", "use", OTHER_ADDRESS)
                refused = await trade_tool(
                    session,
                    home,
                    replay,
                    mcp,
                    "trade_close",
                    trade_id=7,
                    preview_digest=preview["previewDigest"],
                )
                if rust_mcp_stub(mcp, "trade_close", refused):
                    return False
                assert refused["code"] == "PREVIEW_MISMATCH"
                assert refused["preview"]["addr"] == OTHER_ADDRESS
                assert refused["preview"]["previewDigest"] != preview["previewDigest"]
                return True

        confirmed = asyncio.run(confirm())
        assert replay.attempts() == {} and replay.admitted_keys() == set()
        assert not endpoints.errors
        assert not (home.path / LEDGER).exists()
        if not confirmed:
            pytest.skip(f"Rust MCP close confirmation awaits {REPO_URL}/issues/176")
