"""Exercise deliberately wrong candidates through the public driver."""

from __future__ import annotations

import os
import shlex
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from compare import MIN_SQLITE_SCHEMA, SQLITE_SCHEMA_REQUIREMENT

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
pytestmark = [
    pytest.mark.parity_namespace,
    pytest.mark.skipif(
        sys.platform != "linux",
        reason="Parity candidates require Linux network namespaces",
    ),
    pytest.mark.skipif(
        sqlite3.sqlite_version_info < MIN_SQLITE_SCHEMA,
        reason=SQLITE_SCHEMA_REQUIREMENT,
    ),
]


def run_driver(
    case: str, env: dict[str, str], *options: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HERE / "parity.py"), "--case", case, *options],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )


def write_mutant(tmp_path: Path, mutant: str) -> Path:
    candidate = tmp_path / mutant
    source = (HERE / "mutant_cli.py").read_text(encoding="utf-8")
    candidate.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
    candidate.chmod(0o700)
    return candidate


def run_mutant(
    tmp_path: Path, mutant: str, case: str
) -> subprocess.CompletedProcess[str]:
    candidate = write_mutant(tmp_path, mutant)
    return run_driver(
        case,
        {
            **os.environ,
            "SAI_PARITY_BIN": str(candidate),
            "HTTPS_PROXY": "http://127.0.0.1:1",
        },
    )


@pytest.mark.parametrize(
    ("mutant", "case", "message"),
    [
        ("history-order", "account-history", "Keeper HTTP GraphQL request differs"),
        (
            "history-projection",
            "account-history",
            "Keeper HTTP GraphQL request differs",
        ),
        ("history-operation", "account-history", "Keeper HTTP GraphQL request differs"),
        ("ws-projection", "watch-price", "WebSocket GraphQL request differs"),
        ("stdout-key-order", "market-list", "stdout bytes differ"),
        ("stderr-duplicate", "account-balances", "stderr bytes differ"),
        ("ledger-milliseconds", "trade-concurrent-close", "Unix timestamp in seconds"),
        ("ledger-rowid", "intent-local-reserved", "intents.sqlite3.schema"),
        ("ledger-index", "intent-local-reserved", "intents.sqlite3.schema"),
        ("ledger-version", "intent-local-reserved", "intents.sqlite3.user_version"),
        (
            "config-comment",
            "config-set-existing",
            "changed text outside the edited value",
        ),
        ("config-inline", "config-unset", "changed text outside the edited value"),
        (
            "config-format",
            "config-set-existing",
            "changed text outside the edited value",
        ),
        (
            "config-order",
            "config-set-existing",
            "changed text outside the edited value",
        ),
        ("network-ipv4", "config-list", "attempted a non-loopback network connection"),
        ("network-ipv6", "config-list", "attempted a non-loopback network connection"),
        (
            "stale-log",
            "market-prices",
            "Log timestamp is outside the invocation window",
        ),
    ],
)
def test_wrong_candidate_fails(
    tmp_path: Path, mutant: str, case: str, message: str
) -> None:
    run = run_mutant(tmp_path, mutant, case)
    assert run.returncode == 1, run.stdout + run.stderr
    assert "| DIFF |" in run.stdout
    assert message in run.stdout


def test_driver_invokes_the_explicit_python_reference(tmp_path: Path) -> None:
    reference = tmp_path / "python-sai"
    reference.write_text(
        f"#!{sys.executable}\nraise SystemExit(37)\n", encoding="utf-8"
    )
    reference.chmod(0o700)
    env = dict(os.environ)
    env.pop("SAI_PARITY_BIN", None)
    run = run_driver("version", env, "--python-reference", str(reference))
    assert run.returncode == 1, run.stdout + run.stderr
    assert "expected reference exit 0, got 37" in run.stdout


def test_equivalent_candidate_passes(tmp_path: Path) -> None:
    run = run_mutant(tmp_path, "history-format", "account-history")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "| PASS |" in run.stdout


def test_doctor_offline_rejects_trader_creation_in_both_clis(tmp_path: Path) -> None:
    # A matching side effect would pass the comparison without the case's state assertion.
    mutant = write_mutant(tmp_path, "doctor-create-trader")
    env = {**os.environ, "SAI_C9A_PYTHON": str(mutant)}
    env.pop("SAI_PARITY_BIN", None)
    run = run_driver("doctor-offline", env)
    assert run.returncode == 1, run.stdout + run.stderr
    for side in ("reference", "candidate"):
        assert (
            f"{side} Persisted state differs from expectation at trader" in run.stdout
        )


def test_explicit_reference_takes_precedence_over_lane_pin(tmp_path: Path) -> None:
    reference = write_mutant(tmp_path, "doctor-create-trader")
    env = {**os.environ, "SAI_C9A_PYTHON": str(tmp_path / "unused")}
    env.pop("SAI_PARITY_BIN", None)

    run = run_driver("doctor-offline", env, "--reference", str(reference))

    assert run.returncode == 1, run.stdout + run.stderr
    assert "reference Persisted state differs from expectation at trader" in run.stdout


def test_stale_index_rejects_changed_error_code(tmp_path: Path) -> None:
    mutant = write_mutant(tmp_path, "stale-index-code")
    env = {**os.environ, "SAI_PARITY_PYTHON": str(mutant)}
    env.pop("SAI_PARITY_BIN", None)
    run = run_driver("trade-partial-close-index-lag-before-minimum", env)
    assert run.returncode == 1, run.stdout + run.stderr
    assert "reference Stdout differs from expectation at error.code" in run.stdout


def test_reference_uses_real_uv_with_home_dependent_shim(tmp_path: Path) -> None:
    fake = tmp_path / "uv"
    home = tmp_path / "original-home"
    home.mkdir()
    (home / ".configured-uv").touch()
    native = shlex.quote(os.environ["UV"])
    fake.write_text(
        f'#!/bin/sh\ntest -f "$HOME/.configured-uv" || exit 71\nexec {native} "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o700)
    env = {**os.environ, "HOME": str(home), "PATH": str(tmp_path)}
    env.pop("SAI_PARITY_BIN", None)
    real = run_driver("version", env)
    assert real.returncode == 0, real.stdout + real.stderr
    assert "| PASS |" in real.stdout

    env.pop("UV", None)
    fake_only = run_driver("version", env)
    assert fake_only.returncode == 2
    assert (
        "Could not resolve a uv executable that runs with an isolated HOME"
        in fake_only.stderr
    )
    assert "| Command |" not in fake_only.stdout


def test_candidate_cannot_connect_to_host_unix_socket(tmp_path: Path) -> None:
    with socket.socket(socket.AF_UNIX) as host:
        host.bind(str(tmp_path / "unix-socket.sock"))
        host.listen()
        run = run_mutant(tmp_path, "unix-socket", "config-list")
        host.setblocking(False)
        with pytest.raises(BlockingIOError):
            host.accept()
    assert run.returncode == 1, run.stdout + run.stderr
    assert "candidate attempted a blocked host socket" in run.stdout


def test_candidate_cannot_send_to_host_unix_datagram_peer(tmp_path: Path) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as host:
        host.bind(str(tmp_path / "unix-datagram-pair.sock"))
        run = run_mutant(tmp_path, "unix-datagram-pair", "config-list")
        host.setblocking(False)
        with pytest.raises(BlockingIOError):
            host.recv(64)
    assert run.returncode == 1, run.stdout + run.stderr
    assert "candidate attempted a blocked host socket" in run.stdout
