"""Shared-home contention and restart against a loopback Keeper."""

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
from collections import defaultdict
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from scenarios import ADDRESS, Endpoints, child_env, loopback, seed_home
from trade_replay import REQUEST_HASH, SESSION, TradeReplay

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

    @property
    def rust(self) -> bool:
        return self.name == "rust"


def runtime(name: str, request: pytest.FixtureRequest, python: Path | None) -> Runtime:
    if name == "rust":
        if python is None:
            pytest.skip(f"set SAI_PYTHON_REFERENCE to a checkout of {REFERENCE}")
        _, native = request.getfixturevalue("cli_pair")
        return Runtime(name, native)
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
            [*runner.cli, *args],
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
        args,
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
        assert replay.counts() == {} and not endpoints.errors
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
    "open": "#117",
    "close": "#117",
    "cancel": "#117",
    "margin": "#118",
    "partial-close": "#118",
    "tpsl": "R1-03",
    "leverage": "R1-03",
}


def command_help(home: Home, runner: Runtime, group: str, command: str) -> str | None:
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
        return None
    assert result.returncode == 0 and result.stdout, (result.stdout, result.stderr)
    return result.stdout


def trade_help(home: Home, runner: Runtime, action_name: str) -> str:
    action = ACTIONS[action_name]
    help_text = command_help(home, runner, "trade", action.args[0])
    if help_text is None or (
        action_name == "cancel" and "--cancel-order" not in help_text
    ):
        assert runner.rust
        pytest.skip(f"Rust trade {action_name} awaits {ACTION_REASONS[action_name]}")
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
    ("submitter", "reconciler"),
    [("python", "python"), ("python", "rust"), ("rust", "python")],
)
def test_lost_admission_blocks_until_reconciliation(
    action_name: str,
    submitter: str,
    reconciler: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    python_reference: Path | None,
) -> None:
    writer = runtime(submitter, request, python_reference)
    reader = runtime(reconciler, request, python_reference)
    action = ACTIONS[action_name]
    replay = action.replay(lose_reply=True)
    with loopback(action.server, trade_replay=replay) as endpoints:
        home = Home(tmp_path / "home", endpoints)
        seed_home(home.path, endpoints, "session")
        trade_help(home, writer, action_name)
        args = ("trade", *action.args)
        error = home.json(writer, *args, code=5)["error"]
        key = error["intentKey"]
        assert error["code"] == "OUTCOME_UNKNOWN" and error["retryable"] is False
        home.local(reader, key, action.kind, "unknown")
        home.row(key, action.kind, action.entity, "unknown")
        assert_blocked(home.invoke(writer, "--json", *args), key)

        replay.phase = "pending"
        status = home.json(reader, "intent", "status", ADDRESS, "--key", key)
        assert status["outcome"] == "pending"
        home.local(writer, key, action.kind, "pending")
        home.row(key, action.kind, action.entity, "pending")
        assert_blocked(home.invoke(writer, "--json", *args), key)

        replay.phase = "filled"
        status = home.json(reader, "intent", "status", ADDRESS, "--key", key)
        assert status["matchedTradeHistory"][0]["intentKey"] == key
        assert status["matchedTradeHistory"][0]["tradeChangeType"] == action.change
        home.local(writer, key, action.kind, "terminal")
        home.row(key, action.kind, action.entity, "terminal")
        assert replay.counts() == {key: 1} and not endpoints.errors
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
        if "--wait" not in trade_help(home, writer, action_name):
            assert writer.rust
            pytest.skip("Rust trade --wait awaits #122")
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
        assert replay.counts() == {key: 1} and not endpoints.errors
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
    with loopback(action.server, trade_replay=replay) as endpoints:
        home = Home(tmp_path / "home", endpoints)
        seed_home(home.path, endpoints, "session")
        trade_help(home, writer, "close")
        if command_help(home, reader, "intent", "wait") is None:
            pytest.skip("Rust intent wait awaits #122")
        first = home.json(writer, "trade", *action.args)
        key = first["intentKey"]
        wait = ("intent", "wait", key, "--timeout", "1")
        timed_out = home.json(reader, *wait, code=5)
        assert timed_out["intentKey"] == key and timed_out["outcome"] == "unknown"
        assert timed_out["receipt"]["execution"] is None
        home.local(writer, key, action.kind, "pending")
        assert_blocked(home.invoke(writer, "--json", "trade", *action.args), key)

        replay.phase = "filled"
        receipt = home.json(reader, "intent", "wait", key, "--timeout", "5")
        assert receipt["intentKey"] == key and receipt["outcome"] == "filled"
        assert receipt["receipt"]["execution"]["tradeChangeType"] == action.change
        home.row(key, action.kind, action.entity, "terminal")
        assert replay.counts() == {key: 1} and not endpoints.errors
        assert_trade_events(home, key, ["accepted", "pending", "terminal"], acked=True)


def test_submission_times_out_after_keeper_receives_mutation(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    python_reference: Path | None,
) -> None:
    # Exercise the real transport deadline in the pinned mixed-runtime run.
    reader = runtime("rust", request, python_reference)
    writer = runtime("python", request, python_reference)
    replay = TradeReplay(hold_admission=True)
    with loopback("default", trade_replay=replay) as endpoints:
        home = Home(tmp_path / "home", endpoints)
        seed_home(home.path, endpoints, "session")
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
        assert replay.counts() == {key: 1} and not endpoints.errors
        assert_trade_events(home, key, ["pending"], acked=False)
