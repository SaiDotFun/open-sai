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
from trade_replay import SESSION, TradeReplay

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
