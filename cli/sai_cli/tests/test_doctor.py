"""`sai doctor`: local and network checks, output, exit codes, no secrets."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from click.testing import CliRunner
from sai_cli import doctor
from sai_cli.core import keeper, log, margin, settings, storage, stream, update, updater
from sai_cli.sai_cli import main
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve
from websockets.typing import Subprotocol

ADDR = "0x" + "4803" * 10
OTHER = "0x" + "c" * 40
ENDPOINT = keeper.MAINNET_HTTP


def b64(value: dict[str, Any]) -> str:
    raw = json.dumps(value).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


HEADER = b64({"alg": "ES256K", "typ": "JWT", "kid": "doctor-test"})
PAYLOAD = b64({"sub": ADDR, "purpose": "user_intent", "nonce": "q7Zr2"})
SIGNATURE = "c2lnbmF0dXJlLW5ldmVyLXByaW50ZWQtYnktZG9jdG9y"
TOKEN = f"{HEADER}.{PAYLOAD}.{SIGNATURE}"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    for name in ("SAI_ENDPOINT", "SAI_WS_ENDPOINT", "SAI_LCD_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def save(hours: float = 96, endpoint: str = ENDPOINT, addr: str = ADDR) -> None:
    storage.save_session(
        {
            "token": TOKEN,
            "evmAddress": addr,
            "endpoint": endpoint,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
        }
    )


def endpoints(
    endpoint: str | None = None,
    ws_override: str | None = None,
    lcd_endpoint: str | None = None,
) -> settings.Settings:
    """Settings with the given endpoints as flags, ignoring env and file."""
    flags = {
        "keeper.endpoint": endpoint,
        "keeper.ws_endpoint": ws_override,
        "lcd.endpoint": lcd_endpoint,
    }
    return settings.resolve(flags, {}, {})


def ctx(
    endpoint: str = ENDPOINT, fix: bool = False, offline: bool = False, **config: Any
) -> doctor.Context:
    """A report-only context unless FIX is set."""
    return doctor.context(
        endpoints(endpoint, **config),
        path_env="",
        fix=fix,
        offline=offline,
    )


def one(fn, context: doctor.Context) -> doctor.Check:
    result = fn(context)
    assert isinstance(result, doctor.Check)
    return result


def snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every path under ROOT with its mode and file bytes."""
    return {
        str(path): (
            path.stat().st_mode,
            path.read_bytes() if path.is_file() else None,
        )
        for path in sorted(root.rglob("*"))
    }


def run_cli(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(main, list(args), catch_exceptions=False)
    return result.exit_code, result.output


@pytest.mark.parametrize(
    ("content", "expected", "exit_code", "summary"),
    [
        (b"[update]\nauto = false\n", "pass", 0, "Config file settings are valid"),
        (
            b"[future]\nprivate_setting = 'private_value'\n",
            "warn",
            0,
            "unknown settings",
        ),
        (b"[update\n", "fail", 1, "not valid TOML"),
        (
            b"[keeper]\nendpoint = 'https://[private_value'\n",
            "fail",
            2,
            "keeper.endpoint",
        ),
        (b"[update]\nauto = 'private_value'\n", "fail", 1, "update.auto"),
        (b"[logs]\nenabled = false\nkeep_days = 0\n", "fail", 1, "logs.keep_days"),
        (b"\xff", "fail", 1, "UTF-8"),
        pytest.param(
            b"[logs]\nkeep_days = " + b"9" * 5000,
            "fail",
            1,
            "not valid TOML",
            id="oversized-TOML-integer",
        ),
    ],
)
def test_config_check_reports_file_errors_without_echoing_values(
    isolated_home, monkeypatch, content, expected, exit_code, summary
):
    path = isolated_home / "config.toml"
    path.write_bytes(content)
    # File validation still applies to keys shadowed by an environment value.
    monkeypatch.setenv("SAI_UPDATE_AUTO", "false")
    code, output = run_cli("--json", "doctor", "--offline", "--no-fix")
    report = json.loads(output)
    if expected == "fail":
        assert report["error"]["code"] == "INVALID_REQUEST"
        report = report["error"]["details"]
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["config"]["status"] == expected
    assert summary in checks["config"]["summary"]
    assert "home" in checks
    assert code == exit_code
    assert "private_setting" not in output and "private_value" not in output
    assert path.read_bytes() == content


def test_unusable_config_skips_checks_that_need_resolved_settings(isolated_home):
    (isolated_home / "config.toml").write_text("[keeper\n")
    code, output = run_cli("--json", "doctor", "--no-fix")
    report = json.loads(output)["error"]["details"]
    checks = {check["id"]: check for check in report["checks"]}
    assert code == 1
    for name in ("session", "intents", "keeper", "websocket", "lcd"):
        assert checks[name]["status"] == "skipped"


def test_config_warns_about_invalid_logging_override(monkeypatch):
    monkeypatch.setenv("SAI_LOG", "private-invalid-boolean")

    code, output = run_cli("--json", "doctor", "--offline", "--no-fix")

    assert code == 0
    checks = {check["id"]: check for check in json.loads(output)["checks"]}
    assert checks["config"]["status"] == "warn"
    assert "SAI_LOG" in checks["config"]["summary"]
    assert "logs.enabled" in checks["config"]["summary"]
    assert "private-invalid-boolean" not in output


# Session


def test_no_fix_on_empty_home_creates_nothing(isolated_home):
    code, output = run_cli("doctor", "--offline", "--no-fix")
    assert code == 0
    assert list(isolated_home.iterdir()) == []
    assert "does not exist yet" in output


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_empty_home_gets_a_private_trader_directory(isolated_home):
    code, output = run_cli("--json", "doctor", "--offline")
    assert code == 0
    root = isolated_home / "trader"
    assert root.stat().st_mode & 0o777 == 0o700
    assert list(root.iterdir()) == []
    home = next(c for c in json.loads(output)["checks"] if c["id"] == "home")
    assert home["status"] == "fixed"
    assert home["changes"] == [f"created {root} with mode 700"]


def test_empty_home_warns_signed_out(isolated_home):
    code, output = run_cli("doctor", "--offline")
    assert code == 0
    assert "Signed out; MCP trading tools are unavailable" in output
    assert "No intent ledger yet" in output
    assert output.count("Skipped with --offline") == 3


def test_valid_session_passes():
    save()
    check = one(doctor.check_session, ctx())
    assert check.status == "pass"
    assert check.details["addr"] == ADDR


def test_expired_session_fails():
    save(hours=-1)
    check = one(doctor.check_session, ctx())
    assert check.status == "fail"
    assert "expired" in check.summary
    assert check.fix == f"sai --endpoint {ENDPOINT} auth import --addr {ADDR}"
    assert "Foundry keystore" in check.summary


def test_session_for_other_endpoint_fails():
    save(endpoint="https://user:pw@other.example/query?key=abc")
    check = one(doctor.check_session, ctx())
    assert check.status == "fail"
    assert check.details["endpoint"] == "https://other.example"
    assert "https://other.example" in check.summary
    assert "/query" not in check.summary
    assert "pw" not in check.summary and "key=abc" not in check.summary


def test_session_without_an_endpoint_reports_a_missing_endpoint():
    storage.save_session(
        {
            "token": TOKEN,
            "evmAddress": ADDR,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=48)).isoformat(),
        }
    )
    code, output = run_cli("--json", "doctor", "--offline", "--no-fix")

    assert code == 1
    checks = json.loads(output)["error"]["details"]["checks"]
    check = next(check for check in checks if check["id"] == "session")
    assert check["status"] == "fail"
    assert "has no stored Keeper endpoint" in check["summary"]
    assert "auth login NAME" in check["fix"]
    assert check["details"]["addr"] == ADDR
    assert check["details"]["endpoint"] is None
    assert check["details"]["endpointMatches"] is False


@pytest.mark.parametrize(("hours", "unit"), [(0.5, "min"), (48, " h")])
def test_session_expiring_within_three_days_warns(hours, unit):
    save(hours=hours)
    check = one(doctor.check_session, ctx())
    assert check.status == "warn"
    assert "expires in" in check.summary and unit in check.summary


@pytest.mark.parametrize(
    "envelope",
    [{"version": 1, "salt": "x"}, {"version": 2, "backend": "file"}],
)
def test_passphrase_session_fails(isolated_home, envelope):
    root = isolated_home / "trader"
    root.mkdir(mode=0o700)
    (root / "session.json").write_text(json.dumps(envelope))
    (root / "session.json").chmod(0o600)
    check = one(doctor.check_session, ctx())
    assert check.status == "fail"
    assert "passphrase" in check.summary


def test_unreadable_session_fails(isolated_home):
    root = isolated_home / "trader"
    root.mkdir(mode=0o700)
    (root / "session.json").write_text("not json")
    check = one(doctor.check_session, ctx())
    assert check.status == "fail"
    assert "unreadable" in check.summary


def loosen(home: Path) -> Path:
    save()
    ledger([])
    root = home / "trader"
    root.chmod(0o755)
    (root / "sessions.json").chmod(0o644)
    (root / "intents.sqlite3").chmod(0o640)
    return root


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_no_fix_reports_loose_modes_without_changing_them(isolated_home):
    root = loosen(isolated_home)
    before = snapshot(isolated_home)
    check = one(doctor.check_home, ctx())
    assert check.status == "warn"
    assert check.fix == (
        f"chmod go-rwx {root}; chmod go-rwx {root / 'sessions.json'};"
        f" chmod go-rwx {root / 'intents.sqlite3'}"
    )
    assert snapshot(isolated_home) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
@pytest.mark.parametrize("default_home", [False, True])
@pytest.mark.parametrize("trader_mode", [0o700, 0o775])
@pytest.mark.parametrize("evidence", ["sessions", "ledger"])
def test_doctor_repairs_a_dedicated_existing_sai_home(
    isolated_home, default_home, trader_mode, evidence, monkeypatch
):
    if default_home:
        monkeypatch.setenv("HOME", str(isolated_home))
        isolated_home = isolated_home / ".sai"
        monkeypatch.delenv("SAI_HOME")
    if evidence == "sessions":
        save()
    else:
        storage.db().close()
    wheels = updater.private_cache()
    (wheels / update.wheel_name("0.1.0")).touch()
    isolated_home.chmod(0o755)
    trader = isolated_home / "trader"
    trader.chmod(trader_mode)
    expected_modes = [{"path": str(isolated_home), "mode": "755"}]
    expected_fix = f"chmod go-rwx {isolated_home}"
    expected_changes = [f"chmod {isolated_home} 755 -> 700"]
    if trader_mode != 0o700:
        expected_modes.append({"path": str(trader), "mode": f"{trader_mode:o}"})
        expected_fix += f"; chmod go-rwx {trader}"
        expected_changes.insert(0, f"chmod {trader} {trader_mode:o} -> 700")

    code, output = run_cli("--json", "doctor", "--offline", "--no-fix")
    assert code == 0
    check = next(c for c in json.loads(output)["checks"] if c["id"] == "home")
    assert check["status"] == "warn"
    assert check["details"]["looseModes"] == expected_modes
    assert check["fix"] == expected_fix
    assert isolated_home.stat().st_mode & 0o777 == 0o755
    assert trader.stat().st_mode & 0o777 == trader_mode

    code, output = run_cli("--json", "doctor", "--offline")
    assert code == 0
    check = next(c for c in json.loads(output)["checks"] if c["id"] == "home")
    assert check["status"] == "fixed"
    assert check["changes"] == expected_changes
    assert isolated_home.stat().st_mode & 0o777 == 0o700
    assert trader.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
def test_doctor_leaves_a_home_with_only_foreign_config_at_0755(isolated_home):
    config = isolated_home / "config.toml"
    config.write_text("[foreign]\nsetting = true\n")
    isolated_home.chmod(0o755)

    # A second fix run sees the first run's empty trader directory and logs.
    for flags in [("--no-fix",), (), ()]:
        code, output = run_cli("--json", "doctor", "--offline", *flags)
        assert code == 0
        check = next(c for c in json.loads(output)["checks"] if c["id"] == "home")
        assert check["status"] == "warn"
        assert "moving the profile to a dedicated directory" in check["fix"]
        assert isolated_home.stat().st_mode & 0o777 == 0o755
        assert config.read_text() == "[foreign]\nsetting = true\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("sessions.lock", b""),
        ("sessions.lock", b"\0"),
        ("sessions.json", b""),
        ("sessions.json", b'{"saved_filters":[]}'),
        ("sessions.json", b'{"version":3,"nonce":"x","ciphertext":"x"}'),
        ("session.json", b""),
        ("intents.sqlite3", b""),
        ("intents.sqlite3", b"not a database"),
        ("history", b"config list\n"),
    ],
)
def test_doctor_needs_more_than_trader_artifacts(
    isolated_home, monkeypatch, filename, content
):
    (isolated_home / "config.toml").write_text("[foreign]\nsetting = true\n")
    isolated_home.chmod(0o755)
    monkeypatch.setenv("SAI_LOG", "0")
    trader = isolated_home / "trader"

    code, output = run_cli("--json", "auth", "logout")
    assert code == 0 and json.loads(output)["removed"] is False
    assert not trader.exists()

    trader.mkdir(mode=0o700)
    artifact = trader / filename
    artifact.write_bytes(content)
    artifact.chmod(0o600)
    for fix in (False, True):
        check = one(doctor.check_home, ctx(fix=fix))
        assert check.status == "warn"
        assert check.fix == doctor.SAI_HOME_MODE_FIX
        assert check.changes == []
        assert isolated_home.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
@pytest.mark.parametrize(
    "schema",
    [
        "CREATE TABLE saved_filters (name TEXT)",
        (
            "CREATE TABLE intents (id INTEGER, endpoint TEXT NOT NULL,"
            " account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,"
            " entity TEXT NOT NULL)"
        ),
    ],
)
def test_doctor_leaves_a_foreign_sqlite_ledger_unmodified(isolated_home, schema):
    (isolated_home / "config.toml").write_text("[foreign]\nsetting = true\n")
    isolated_home.chmod(0o755)
    trader = isolated_home / "trader"
    trader.mkdir(mode=0o700)
    path = trader / "intents.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(schema)
    connection.close()
    path.chmod(0o600)
    before = path.read_bytes()

    for flags in [("--no-fix",), (), ()]:
        code, output = run_cli("--json", "doctor", "--offline", *flags)
        assert code == 1
        checks = {c["id"]: c for c in json.loads(output)["error"]["details"]["checks"]}
        assert checks["home"]["status"] == "warn"
        assert checks["home"]["fix"] == doctor.SAI_HOME_MODE_FIX
        assert checks["home"]["changes"] == []
        assert checks["intents"]["status"] == "fail"
        assert isolated_home.stat().st_mode & 0o777 == 0o755
        assert path.read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
@pytest.mark.parametrize("log_enabled", ["0", "1"])
@pytest.mark.parametrize(
    ("directory", "filename", "content", "exit_code", "check_id", "sai_ledger"),
    [
        ("trader", "sessions.json", '{"saved_filters":[]}', 1, "home", False),
        ("logs", "application.log", "application startup\n", 0, "logs", False),
        ("logs", None, '{"other_application":"request"}\n', 0, "logs", True),
    ],
)
def test_doctor_reports_foreign_subdirectories_without_tightening(
    isolated_home,
    monkeypatch,
    log_enabled,
    directory,
    filename,
    content,
    exit_code,
    check_id,
    sai_ledger,
):
    monkeypatch.setenv("SAI_LOG", log_enabled)
    if sai_ledger:
        storage.db().close()
    if filename is None:
        filename = log.file_name("cli", datetime.now(UTC).date())
    (isolated_home / "config.toml").write_text("[foreign]\nsetting = true\n")
    isolated_home.chmod(0o755)
    child = isolated_home / directory
    child.mkdir(mode=0o755)
    artifact = child / filename
    artifact.write_text(content)
    artifact.chmod(0o644)

    for flags in [("--no-fix",), ()]:
        code, output = run_cli("--json", "doctor", "--offline", *flags)
        assert code == exit_code
        assert log.flush()
        report = json.loads(output)
        if exit_code:
            report = report["error"]["details"]
        check = next(c for c in report["checks"] if c["id"] == check_id)
        assert check["status"] == "warn"
        assert check["fix"] == doctor.SAI_HOME_MODE_FIX
        assert check["changes"] == []
        assert isolated_home.stat().st_mode & 0o777 == 0o755
        assert child.stat().st_mode & 0o777 == 0o755
        assert artifact.stat().st_mode & 0o777 == 0o644
        assert artifact.read_text() == content


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
def test_doctor_logging_leaves_a_foreign_archive_alone(isolated_home, monkeypatch):
    monkeypatch.setenv("SAI_LOG", "1")
    root = isolated_home / "logs"
    root.mkdir(mode=0o700)
    archive = root / "cli-2020-01-01.jsonl"
    contents = '{"other_application":"archived request"}\n'
    archive.write_text(contents)
    archive.chmod(0o600)

    assert run_cli("doctor", "--offline")[0] == 0
    assert log.flush()
    assert archive.read_text() == contents


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
@pytest.mark.parametrize(
    ("key", "expected_code"), [("keeper.endpoint", 0), ("bogus", 2)]
)
def test_doctor_preserves_a_foreign_home_with_sai_logs(
    isolated_home, key, expected_code
):
    (isolated_home / "config.toml").write_text("[foreign]\nsetting = true\n")
    isolated_home.chmod(0o755)
    code, _ = run_cli("--json", "config", "get", key)
    assert code == expected_code and log.flush()
    assert log.log_files()
    assert not (isolated_home / "trader").exists()

    for flags in [("--no-fix",), (), ()]:
        code, output = run_cli("--json", "doctor", "--offline", *flags)
        assert code == 0
        check = next(c for c in json.loads(output)["checks"] if c["id"] == "home")
        assert check["status"] == "warn"
        assert check["fix"] == doctor.SAI_HOME_MODE_FIX
        assert isolated_home.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
def test_doctor_preserves_a_home_with_only_a_cached_sai_wheel(isolated_home):
    wheels = updater.private_cache()
    (wheels / update.wheel_name("0.1.0")).touch()
    isolated_home.chmod(0o755)

    code, output = run_cli("--json", "doctor", "--offline")
    assert code == 0
    check = next(c for c in json.loads(output)["checks"] if c["id"] == "home")
    assert check["status"] == "warn"
    assert isolated_home.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
@pytest.mark.parametrize(
    ("mode", "layout"),
    [
        (0o2770, "ledger"),
        (0o2755, "ledger"),
        (0o0770, "ledger"),
        (0o0757, "ledger"),
        (0o0755, "mixed-cache"),
        (0o0755, "setgid-cache"),
        (0o0755, "empty"),
    ],
)
def test_doctor_reports_and_preserves_an_existing_shared_or_ambiguous_root(
    isolated_home, mode, layout
):
    if layout != "empty":
        storage.db().close()
    if layout == "mixed-cache":
        other_app = isolated_home / "cache" / "other-app.db"
        other_app.parent.mkdir()
        other_app.write_text("unrelated data")
    if layout == "setgid-cache":
        cache = isolated_home / "cache"
        cache.mkdir()
        cache.chmod(0o2755)
    isolated_home.chmod(mode)

    code, output = run_cli("--json", "doctor", "--offline")
    assert code == 0
    check = next(c for c in json.loads(output)["checks"] if c["id"] == "home")
    assert check["status"] == "warn"
    assert check["details"]["looseModes"][0] == {
        "path": str(isolated_home),
        "mode": f"{mode:o}",
    }
    assert isolated_home.stat().st_mode & 0o7777 == mode
    if layout == "empty":
        # Doctor creates an empty trader directory on its first run. That
        # directory and diagnostic logs still leave the root ambiguous.
        code, output = run_cli("--json", "doctor", "--offline")
        assert code == 0
        check = next(c for c in json.loads(output)["checks"] if c["id"] == "home")
        assert check["status"] == "warn"
        assert check["changes"] == []
        assert isolated_home.stat().st_mode & 0o7777 == mode


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
def test_doctor_gives_shared_root_guidance_in_both_modes(isolated_home):
    ledger_path = old_ledger(isolated_home)
    ledger_path.chmod(0o640)
    trader = ledger_path.parent
    trader.chmod(0o755)
    history = trader / "history"
    history.touch()
    history.chmod(0o644)
    isolated_home.chmod(0o2770)
    root_advice = (
        "This Sai home may be shared. Stop all Sai CLI and MCP processes before moving"
        " the profile to a dedicated directory. Keep the trader data, including the intent"
        " ledger. Set SAI_HOME to the new directory for the CLI and in every MCP client"
        " configuration before restarting clients or resuming trading."
    )

    code, output = run_cli("--json", "doctor", "--offline", "--no-fix")
    assert code == 0
    check = next(c for c in json.loads(output)["checks"] if c["id"] == "home")
    assert check["status"] == "warn"
    assert check["fix"] == root_advice
    assert history.stat().st_mode & 0o777 == 0o644
    assert isolated_home.stat().st_mode & 0o7777 == 0o2770

    code, output = run_cli("--json", "doctor", "--offline")
    assert code == 0
    check = next(c for c in json.loads(output)["checks"] if c["id"] == "home")
    assert check["status"] == "warn"
    assert check["fix"] == root_advice
    assert check["changes"] == []
    assert user_version(ledger_path) == storage.SCHEMA_VERSION
    assert ledger_path.stat().st_mode & 0o777 == 0o640
    assert trader.stat().st_mode & 0o777 == 0o755
    assert history.stat().st_mode & 0o777 == 0o644
    assert isolated_home.stat().st_mode & 0o7777 == 0o2770


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_fix_makes_the_directory_and_files_private(isolated_home):
    root = loosen(isolated_home)
    session_bytes = (root / "sessions.json").read_bytes()
    check = one(doctor.check_home, ctx(fix=True))
    assert check.status == "fixed"
    assert check.changes == [
        f"chmod {root} 755 -> 700",
        f"chmod {root / 'sessions.json'} 644 -> 600",
        f"chmod {root / 'intents.sqlite3'} 640 -> 600",
    ]
    assert root.stat().st_mode & 0o777 == 0o700
    for name in ("sessions.json", "intents.sqlite3"):
        assert (root / name).stat().st_mode & 0o777 == 0o600
    assert (root / "sessions.json").read_bytes() == session_bytes
    assert one(doctor.check_home, ctx(fix=True)).status == "pass"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
@pytest.mark.parametrize("mixed", [False, True])
def test_fix_keeps_owner_bits_and_preserves_a_mixed_trader(isolated_home, mixed):
    storage.db().close()
    root = isolated_home / "trader"
    history = root / "history"
    history.write_text("")
    history.chmod(0o754)
    tool = root / "notes.sh"
    if mixed:
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
    check = one(doctor.check_home, ctx(fix=True))
    if mixed:
        assert check.fix == doctor.SAI_HOME_MODE_FIX
        assert check.changes == []
        assert history.stat().st_mode & 0o777 == 0o754
        assert tool.stat().st_mode & 0o777 == 0o755
    else:
        assert check.changes == [f"chmod {history} 754 -> 700"]
        assert history.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_trader_directory_is_never_written(isolated_home, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    outside.chmod(0o755)
    child = outside / "session.json"
    child.write_text("{}")
    child.chmod(0o755)
    (isolated_home / "trader").symlink_to(outside)
    before = snapshot(outside)
    code, output = run_cli("--json", "doctor", "--offline")
    assert code == 1
    error = json.loads(output)["error"]
    assert error["code"] == "INTERNAL_ERROR"
    assert "symlink" in error["message"] and "intents:" in error["message"]
    assert outside.stat().st_mode & 0o777 == 0o755
    assert snapshot(outside) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_ledger_is_not_migrated(isolated_home, tmp_path_factory):
    target = old_ledger(tmp_path_factory.mktemp("elsewhere"))
    root = isolated_home / "trader"
    root.mkdir(mode=0o700)
    (root / "intents.sqlite3").symlink_to(target)
    check = one(doctor.check_intents, ctx(fix=True))
    assert check.status == "fail"
    assert "symlink" in check.summary
    assert user_version(target) == 0
    assert one(doctor.check_home, ctx(fix=True)).status == "fail"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes and symlinks")
@pytest.mark.parametrize("mode", [0o755, 0o2770])
def test_doctor_checks_root_independently_of_a_symlinked_history(
    isolated_home, tmp_path_factory, mode
):
    storage.db().close()
    target = tmp_path_factory.mktemp("elsewhere") / "history"
    target.write_text("outside history")
    target.chmod(0o644)
    history = isolated_home / "trader" / "history"
    history.symlink_to(target)
    isolated_home.chmod(mode)

    code, output = run_cli("--json", "doctor", "--offline")
    assert code == 1
    checks = json.loads(output)["error"]["details"]["checks"]
    check = next(c for c in checks if c["id"] == "home")
    assert check["status"] == "fail"
    assert check["details"]["symlinks"] == [str(history)]
    assert check["details"]["looseModes"] == [
        {"path": str(isolated_home), "mode": f"{mode:o}"}
    ]
    assert check["changes"] == []
    assert isolated_home.stat().st_mode & 0o7777 == mode
    assert history.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_tighten_refuses_a_path_swapped_for_a_symlink(isolated_home, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    target = outside / "secret"
    target.write_text("x")
    target.chmod(0o644)
    root = isolated_home / "trader"
    root.mkdir(mode=0o700)
    (root / "session.json").symlink_to(target)
    changes, failed, eligible = doctor.tighten(root, doctor.MANAGED)
    assert changes == []
    assert not eligible
    assert failed == [
        f"Could not open {root / 'session.json'} safely; inspect it by hand"
    ]
    assert target.stat().st_mode & 0o777 == 0o644
    linked = isolated_home / "linked"
    linked.symlink_to(outside)
    outside.chmod(0o755)
    assert doctor.tighten(linked, doctor.MANAGED) == (
        [],
        [f"Could not open {linked} safely; inspect it by hand"],
        False,
    )
    assert outside.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard links")
@pytest.mark.parametrize("root_mode", [0o700, 0o755])
def test_hard_linked_history_is_refused_in_both_modes(
    isolated_home, tmp_path_factory, root_mode
):
    outside = tmp_path_factory.mktemp("outside") / "shared"
    outside.write_text("x")
    outside.chmod(0o644)
    root = isolated_home / "trader"
    root.mkdir(mode=0o700)
    os.link(outside, root / "history")
    isolated_home.chmod(root_mode)
    advice = f"{root / 'history'} has other hard links; fix by hand"
    if root_mode == 0o755:
        advice = f"{doctor.SAI_HOME_MODE_FIX} {advice}"
    for fix in (False, True):
        check = one(doctor.check_home, ctx(fix=fix))
        assert check.status == "fail"
        assert check.fix == advice
        assert check.changes == []
        assert outside.stat().st_mode & 0o777 == 0o644
        assert isolated_home.stat().st_mode & 0o777 == root_mode


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard links")
def test_hard_linked_ledger_is_not_migrated(isolated_home, tmp_path_factory):
    outside = old_ledger(tmp_path_factory.mktemp("elsewhere"))
    root = isolated_home / "trader"
    root.mkdir(mode=0o700)
    os.link(outside, root / "intents.sqlite3")
    check = one(doctor.check_intents, ctx(fix=True))
    assert check.status == "fail"
    assert "hard links" in check.summary
    assert user_version(outside) == 0


def test_ledger_replaced_before_a_write_is_not_written(isolated_home, monkeypatch):
    path = old_ledger(isolated_home)
    replacement = isolated_home / "replacement.sqlite3"
    replacement.write_bytes(path.read_bytes())
    read = doctor.read_ledger

    def read_then_swap(target):
        result = read(target)
        os.replace(replacement, path)
        return result

    monkeypatch.setattr(doctor, "read_ledger", read_then_swap)
    check = one(doctor.check_intents, ctx(fix=True))
    assert check.status == "fail"
    assert "changed during the check" in check.summary
    assert user_version(path) == 0


# Intents


def ledger(rows: list[tuple[str, str, str, str]], version: int | None = None) -> Path:
    with storage.db() as connection:
        for key, endpoint, account, status in rows:
            connection.execute(
                "INSERT INTO intents (id, endpoint, account, kind, status, entity,"
                " created_at) VALUES (?, ?, ?, 'OPEN_TRADE', ?, '', ?)",
                (key, endpoint, account.lower(), status, time.time()),
            )
        if version is not None:
            connection.execute(f"PRAGMA user_version = {version}")
    return storage.home() / "intents.sqlite3"


def test_clean_ledger_passes():
    ledger([("k1", ENDPOINT, ADDR, "executed")])
    check = one(doctor.check_intents, ctx())
    assert check.status == "pass"
    assert check.details["unknown"] == 0


def test_ledger_from_newer_sai_fails_and_is_not_touched(isolated_home):
    path = ledger([], version=storage.SCHEMA_VERSION + 1)
    before = path.read_bytes()
    check = one(doctor.check_intents, ctx())
    assert check.status == "fail"
    assert "schema" in check.summary
    assert path.read_bytes() == before


def test_unknown_intent_for_signed_in_account_fails():
    save()
    ledger(
        [
            ("20261118T000000000000-a", ENDPOINT, ADDR, "unknown"),
            ("20261118T000000000000-b", ENDPOINT, OTHER, "unknown"),
        ]
    )
    check = one(doctor.check_intents, ctx())
    assert check.status == "fail"
    assert check.summary == "1 unknown intent blocks trading for 0x4803…"
    assert check.fix is not None
    assert check.fix.startswith('sai --endpoint "${SAI_RECONCILE_ENDPOINT:?')
    assert check.fix.endswith(f"intent status {ADDR} --key 20261118T000000000000-a")
    assert check.details["otherAccounts"] == {"unknown": 1, "accounts": 1}


def test_unknown_intents_for_other_accounts_warn_with_counts_only():
    save()
    ledger(
        [
            ("k1", ENDPOINT, OTHER, "unknown"),
            ("k2", ENDPOINT, OTHER, "unknown"),
            ("k3", "https://other.example/query", ADDR, "unknown"),
        ]
    )
    check = one(doctor.check_intents, ctx())
    assert check.status == "warn"
    assert check.summary == (
        "3 unknown intents for 2 accounts not signed in here; they do not"
        " block this session"
    )
    rendered = json.dumps(check.to_json())
    assert OTHER.lower() not in rendered and "k1" not in rendered


def test_signed_out_unknowns_never_block():
    ledger([("k1", ENDPOINT, ADDR, "unknown")])
    assert one(doctor.check_intents, ctx()).status == "warn"


def test_corrupt_ledger_fails(isolated_home):
    root = isolated_home / "trader"
    root.mkdir(mode=0o700)
    (root / "intents.sqlite3").write_bytes(b"not a database" * 512)
    check = one(doctor.check_intents, ctx())
    assert check.status == "fail"
    assert "unreadable" in check.summary


def old_ledger(home: Path) -> Path:
    """A schema-0 ledger from before `created_at`."""
    root = home / "trader"
    root.mkdir(mode=0o700)
    path = root / "intents.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL,"
            " account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,"
            " entity TEXT NOT NULL DEFAULT '')"
        )
    connection.close()
    path.chmod(0o600)
    return path


def user_version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()
    return version


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_doctor_reports_root_repair_before_ledger_migration(
    isolated_home,
):
    path = old_ledger(isolated_home)
    path.parent.chmod(0o770)
    isolated_home.chmod(0o755)

    code, output = run_cli("--json", "doctor", "--offline")
    assert code == 0
    checks = {c["id"]: c for c in json.loads(output)["checks"]}
    assert checks["home"]["status"] == "fixed"
    assert checks["home"]["changes"] == [
        f"chmod {path.parent} 770 -> 700",
        f"chmod {isolated_home} 755 -> 700",
    ]
    assert checks["intents"]["status"] == "fixed"
    assert user_version(path) == storage.SCHEMA_VERSION
    assert isolated_home.stat().st_mode & 0o777 == 0o700


def test_no_fix_warns_about_an_old_ledger(isolated_home):
    path = old_ledger(isolated_home)
    check = one(doctor.check_intents, ctx())
    assert check.status == "warn"
    assert user_version(path) == 0


def test_fix_migrates_an_old_ledger(isolated_home):
    path = old_ledger(isolated_home)
    check = one(doctor.check_intents, ctx(fix=True))
    assert check.status == "fixed"
    assert check.changes == [
        f"migrated the ledger from schema 0 to {storage.SCHEMA_VERSION}"
    ]
    assert user_version(path) == storage.SCHEMA_VERSION


def test_no_fix_reads_a_wal_ledger_without_writing(isolated_home):
    path = ledger([("k1", ENDPOINT, ADDR, "executed")])
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.close()
    assert names(path.parent) == ["intents.sqlite3"]
    before = snapshot(isolated_home)
    code, _output = run_cli("doctor", "--offline", "--no-fix")
    assert code == 0
    assert snapshot(isolated_home) == before


def test_no_fix_reads_rows_from_a_live_wal_without_touching_it():
    save()
    path = ledger([])
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO intents (id, endpoint, account, kind, status, entity,"
            " created_at) VALUES ('k-wal', ?, ?, 'OPEN_TRADE', 'unknown', '', 0)",
            (ENDPOINT, ADDR.lower()),
        )
        before = snapshot(path.parent)
        check = one(doctor.check_intents, ctx())
        after = snapshot(path.parent)
    finally:
        writer.close()
    assert check.details["blocking"] == ["k-wal"]
    assert after == before


def add_unknown(key: str, account: str = ADDR, endpoint: str = ENDPOINT) -> None:
    with storage.db() as connection:
        connection.execute(
            "INSERT INTO intents (id, endpoint, account, kind, status, entity,"
            " created_at) VALUES (?, ?, ?, 'OPEN_TRADE', 'unknown', '', ?)",
            (key, endpoint, account.lower(), time.time()),
        )
    connection.close()


def test_snapshot_retries_when_the_ledger_changes_during_the_copy(monkeypatch):
    save()
    path = ledger([])
    copy = doctor.shutil.copyfileobj
    writes = iter(["k-late"])

    def copy_then_write(source, target):
        result = copy(source, target)
        key = next(writes, None)
        if key:
            time.sleep(0.01)
            add_unknown(key)
        return result

    monkeypatch.setattr(doctor.shutil, "copyfileobj", copy_then_write)
    check = one(doctor.check_intents, ctx())
    assert check.details["blocking"] == ["k-late"]
    assert path.is_file()


def test_ledger_that_keeps_changing_warns(monkeypatch):
    ledger([])
    copy = doctor.shutil.copyfileobj
    count = iter(range(100))

    def copy_then_write(source, target):
        result = copy(source, target)
        time.sleep(0.01)
        add_unknown(f"k{next(count)}", account=OTHER)
        return result

    monkeypatch.setattr(doctor.shutil, "copyfileobj", copy_then_write)
    check = one(doctor.check_intents, ctx())
    assert check.status == "warn"
    assert "changed while reading" in check.summary


def names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_naive_session_expiry_fails_the_session_check_and_continues(isolated_home):
    storage.save_session(
        {
            "token": TOKEN,
            "evmAddress": ADDR,
            "endpoint": ENDPOINT,
            "expiresAt": "2099-01-01T00:00:00",
        }
    )
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    code, output = result.exit_code, result.stderr
    assert code == 1
    assert result.stdout == ""
    assert "fail    session" in output and "invalid" in output
    assert "pass    intents" in output


# Install


def fake_sai(directory: Path) -> Path:
    directory.mkdir()
    path = directory / "sai"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name == "nt", reason="POSIX executables")
def test_two_installs_on_path_warn(tmp_path):
    first = fake_sai(tmp_path / "a")
    fake_sai(tmp_path / "b")
    path_env = os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])
    context = doctor.context(endpoints(), path_env=path_env)
    check = one(doctor.check_install, context)
    assert check.status == "warn"
    assert str(first.resolve()) in check.summary
    assert len(check.details["onPath"]) == 2


def test_install_reports_package_version():
    check = one(doctor.check_install, ctx())
    assert check.details["version"] == doctor.info.version()
    assert check.status in ("pass", "warn")


def with_install(kind: str) -> doctor.Context:
    return dataclasses.replace(ctx(), install=update.Install(kind, "/prefix"))


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        (update.UVX, "runs from a uvx cache"),
        (update.UV_TOOL_OTHER, "is not a uv tool install from SaiDotFun/open-sai"),
        (update.OTHER, "is not a uv tool install from SaiDotFun/open-sai"),
        (update.CHECKOUT, "runs from a source checkout"),
    ],
)
def test_install_warns_for_installs_sai_update_cannot_change(kind, text):
    check = one(doctor.check_install, with_install(kind))
    assert check.status == "warn"
    assert text in check.summary
    assert check.details["kind"] == kind
    assert check.fix is not None and update.INSTALL_COMMAND in check.fix


def test_install_reports_the_detected_kind():
    check = one(doctor.check_install, ctx())
    assert check.details["kind"] == update.install_kind().kind


def version_check(kind: str = update.UV_TOOL_RELEASE) -> doctor.Check:
    return one(doctor.check_version, with_install(kind))


def test_version_passes_without_a_recorded_check():
    check = version_check()
    assert check.status == "pass"
    assert "no release check is recorded yet" in check.summary
    assert check.details["latest"] is None


def test_version_passes_when_up_to_date():
    current = update.tag_for(doctor.info.version())
    updater.write_state({"latest": current, "checkedAt": update.now()})
    check = version_check()
    assert check.status == "pass"
    assert "up to date" in check.summary


def test_version_warns_when_behind():
    updater.write_state({"latest": "sai-cli/v99.0.0", "checkedAt": update.now()})
    check = version_check()
    assert check.status == "warn"
    assert "is behind the latest release 99.0.0" in check.summary
    assert check.fix == "Update: `sai update`"
    unofficial = version_check(update.CHECKOUT)
    assert unofficial.fix == f"Update: {update.INSTALL_COMMAND}"


def test_version_warns_when_the_last_update_failed():
    result = {
        "status": updater.FAILED,
        "to": "99.0.0",
        "error": "Could not update to 99.0.0: the package does not import.",
    }
    updater.write_state(
        {"latest": "sai-cli/v99.0.0", "checkedAt": update.now(), "result": result}
    )
    check = version_check()
    assert check.status == "warn"
    assert "The last update failed" in check.summary
    assert "does not import" in check.summary
    assert check.fix == "Retry: `sai update`"
    assert check.details["lastUpdate"] == result


def test_version_ignores_a_failure_the_install_has_moved_past():
    result = {"status": updater.FAILED, "to": "0.0.1", "error": "old"}
    updater.write_state({"result": result})
    assert version_check().status == "pass"


# Network stubs


class Stub:
    def __init__(
        self, enabled: bool = True, network: str | None = "cataclysm-1"
    ) -> None:
        self.enabled = enabled
        self.network = network
        self.intents: list[dict[str, Any]] = []
        # Seconds to drip the userIntents reply over, one byte at a time.
        self.intents_drip = 0.0
        # Called before each userIntents reply.
        self.on_intents: Any = None
        self.queries: list[str] = []
        self.paths: list[str] = []
        self.lcd_paths: list[str] = []
        self.reflect_lcd_network: Callable[[str], str] | None = None


@pytest.fixture
def http_stub() -> Iterator[tuple[str, Stub]]:
    """Serve Keeper's trading route on POST and LCD node_info on GET."""
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, value: dict[str, Any]) -> None:
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def drip(self, value: dict[str, Any]) -> None:
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            end = time.monotonic() + state.intents_drip
            sent = 0
            while time.monotonic() < end and sent < len(body) - 1:
                self.wfile.write(body[sent : sent + 1])
                self.wfile.flush()
                sent += 1
                time.sleep(0.1)
            time.sleep(max(end - time.monotonic(), 0))
            self.wfile.write(body[sent:])

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.queries.append(body["query"])
            state.paths.append(self.path)
            if "userIntents" in body["query"]:
                if state.on_intents:
                    state.on_intents()
                if state.intents_drip:
                    self.drip({"data": {"userIntents": state.intents}})
                else:
                    self.reply({"data": {"userIntents": state.intents}})
                return
            assert "oneClickTrading" in body["query"]
            route = {
                "enabled": state.enabled,
                "chainId": "cataclysm-1",
                "contractAddress": "nibi1x",
            }
            self.reply({"data": {"oneClickTrading": route}})

        def do_GET(self):
            state.lcd_paths.append(self.path)
            assert urlsplit(self.path).path.endswith(margin.NODE_INFO_PATH)
            network = (
                state.reflect_lcd_network(self.path)
                if state.reflect_lcd_network is not None
                else state.network
            )
            info = {} if network is None else {"network": network}
            self.reply({"default_node_info": info})

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()
    thread.join()


def ws_stub(
    ack: bool,
    *,
    close_with_target: bool = False,
    process_response: Callable[[ServerConnection, Request, Response], Response]
    | None = None,
) -> tuple[str, Any]:
    release = threading.Event()

    def handler(connection: ServerConnection) -> None:
        connection.recv()
        if close_with_target:
            assert connection.request is not None
            connection.close(code=1008, reason=connection.request.path)
            return
        if ack:
            connection.send(json.dumps({"type": "connection_ack"}))
        release.wait(10)

    server = serve(
        handler,
        "127.0.0.1",
        0,
        subprotocols=[Subprotocol("graphql-transport-ws")],
        process_response=process_response,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        release.set()
        server.shutdown()
        thread.join()

    return f"ws://127.0.0.1:{server.socket.getsockname()[1]}/query", stop


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_network_checks_pass_against_stubs(http_stub):
    base, state = http_stub
    ws, stop = ws_stub(ack=True)
    try:
        checks = doctor.run_checks(
            ctx(
                f"{base}/query",
                ws_override=ws,
                lcd_endpoint=f"{base}/fixture-lcd-path/?key=fixture-lcd-query",
            )
        )
    finally:
        stop()
    by_id = {check.id: check for check in checks}
    assert [check.id for check in checks] == [
        "install",
        "version",
        "home",
        "logs",
        "session",
        "intents",
        "keeper",
        "websocket",
        "lcd",
    ]
    assert by_id["keeper"].status == "pass"
    assert by_id["websocket"].status == "pass"
    assert by_id["lcd"].status == "pass"
    assert by_id["lcd"].details == {"endpoint": base, "matchesKeeperChain": True}
    assert "matches Keeper chain cataclysm-1" in by_id["lcd"].summary
    assert state.lcd_paths == [
        f"/fixture-lcd-path{margin.NODE_INFO_PATH}?key=fixture-lcd-query"
    ]


@pytest.mark.parametrize("network", [None, "a" * 51, "cataclysm-1\n"])
def test_lcd_with_an_invalid_network_fails(http_stub, network):
    base, state = http_stub
    state.network = network
    check = one(doctor.check_lcd, ctx(lcd_endpoint=base))
    assert check.status == "fail"
    assert "invalid chain ID" in check.summary
    assert check.details == {"endpoint": base}


@pytest.mark.parametrize(
    ("reflect", "status", "summary"),
    [
        pytest.param(lambda target: target, "fail", "invalid chain ID", id="target"),
        pytest.param(
            lambda target: urlsplit(target).path.split("/")[1],
            "warn",
            "LCD network differs from Keeper chain cataclysm-1",
            id="path-segment",
        ),
        pytest.param(
            lambda target: parse_qs(urlsplit(target).query)["key"][0],
            "warn",
            "LCD network differs from Keeper chain cataclysm-1",
            id="query-key",
        ),
    ],
)
def test_doctor_does_not_echo_a_reflected_lcd_network(
    http_stub, isolated_home, reflect, status, summary
):
    base, state = http_stub
    state.reflect_lcd_network = reflect
    path_key, query_key = "fixture-private-lcd-path", "fixture-private-lcd-query"
    target = f"/{path_key}{margin.NODE_INFO_PATH}?key={query_key}"
    ws, stop = ws_stub(ack=True)
    try:
        for flags in ((), ("--json",)):
            code, output = run_cli(
                *flags,
                "--endpoint",
                base + "/query",
                "doctor",
                "--ws-endpoint",
                ws,
                "--lcd-endpoint",
                f"{base}/{path_key}?key={query_key}",
            )
            assert code == (1 if status == "fail" else 0)
            assert summary in output
            assert path_key not in output and query_key not in output
            if flags:
                report = json.loads(output)
                if status == "fail":
                    report = report["error"]["details"]
                lcd = next(check for check in report["checks"] if check["id"] == "lcd")
                assert lcd["status"] == status
                details = {"endpoint": base}
                if status == "warn":
                    details["matchesKeeperChain"] = False
                assert lcd["details"] == details
    finally:
        stop()
    assert state.lcd_paths == [target, target]
    assert log.flush(5)
    logs = "".join(
        path.read_text() for path in (isolated_home / "logs").glob("*.jsonl")
    )
    assert path_key not in logs and query_key not in logs
    results = [json.loads(line) for line in logs.splitlines()]
    lcd_results = [
        check
        for result in results
        if result["event"] == "doctor.result"
        for check in result["checks"]
        if check["id"] == "lcd"
    ]
    assert len(lcd_results) == 2
    assert all(
        row["status"] == status and summary in row["summary"] for row in lcd_results
    )


@pytest.fixture
def drip_server() -> Iterator[str]:
    """An HTTP server that sends headers, then one body byte per 0.3 s."""
    stop = threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)

    def drip(conn: socket.socket) -> None:
        with conn:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
            while not stop.wait(0.3):
                try:
                    conn.sendall(b" ")
                except OSError:
                    return

    def accept() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            threading.Thread(target=drip, args=(conn,), daemon=True).start()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    stop.set()
    thread.join()
    listener.close()


def test_network_checks_stop_at_the_deadline(drip_server, monkeypatch):
    monkeypatch.setattr(doctor, "NETWORK_DEADLINE", 1.0)
    start = time.monotonic()
    checks = doctor.run_checks(ctx(f"{drip_server}/query", lcd_endpoint=drip_server))
    assert time.monotonic() - start < 2.5
    keeper_check = next(check for check in checks if check.id == "keeper")
    assert keeper_check.status == "fail"
    assert keeper_check.summary == "No answer within 1 s"


def deaf_ws_server(ack: bool = True) -> tuple[str, threading.Event]:
    """A WebSocket server that ignores the close; with ACK false it also
    ignores connection_init."""
    stop = threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def serve_one() -> None:
        conn, _ = listener.accept()
        with conn, listener:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            key = next(
                line.split(b":", 1)[1].strip()
                for line in request.split(b"\r\n")
                if line.lower().startswith(b"sec-websocket-key:")
            )
            accept = base64.b64encode(
                hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
            )
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
                + accept
                + b"\r\nSec-WebSocket-Protocol: graphql-transport-ws\r\n\r\n"
            )
            conn.recv(4096)  # connection_init
            if ack:
                reply = json.dumps({"type": "connection_ack"}).encode()
                conn.sendall(bytes([0x81, len(reply)]) + reply)
            stop.wait(15)

    threading.Thread(target=serve_one, daemon=True).start()
    return f"ws://127.0.0.1:{listener.getsockname()[1]}/query", stop


def test_handshake_close_fits_in_the_timeout():
    ws, stop = deaf_ws_server()
    start = time.monotonic()
    try:
        stream.handshake(ws, 0.5)
    finally:
        stop.set()
    assert time.monotonic() - start < 1.5


def test_handshake_without_ack_fails_within_the_timeout():
    ws, stop = deaf_ws_server(ack=False)
    start = time.monotonic()
    try:
        with pytest.raises(stream.DISCONNECTS):
            stream.handshake(ws, 0.5)
    finally:
        stop.set()
    assert time.monotonic() - start < 1.0


def test_trading_disabled_warns(http_stub):
    base, state = http_stub
    state.enabled = False
    check = one(doctor.check_keeper, ctx(f"{base}/query"))
    assert check.status == "warn"
    assert "trading is disabled" in check.summary


def test_lcd_on_another_chain_warns(http_stub):
    base, state = http_stub
    state.network = "nibiru-testnet-2"
    ws, stop = ws_stub(ack=True)
    try:
        checks = doctor.run_checks(
            ctx(f"{base}/query", ws_override=ws, lcd_endpoint=base)
        )
    finally:
        stop()
    lcd = next(check for check in checks if check.id == "lcd")
    assert lcd.status == "warn"
    assert "LCD network differs from Keeper chain cataclysm-1" in lcd.summary
    assert lcd.details == {"endpoint": base, "matchesKeeperChain": False}


def test_silent_websocket_fails_within_the_timeout():
    ws, stop = ws_stub(ack=False)
    start = time.monotonic()
    try:
        check = one(doctor.check_websocket, ctx(ws_override=ws))
    finally:
        stop()
    # The handshake gives up after NETWORK_TIMEOUT, well before the stub
    # lets go at 10 s; the rest is slack for a loaded machine.
    assert time.monotonic() - start < doctor.NETWORK_TIMEOUT + 4
    assert check.status == "fail"
    assert "connection_ack" in check.summary


def test_doctor_omits_peer_websocket_close_reason(http_stub):
    base, _state = http_stub
    ws, stop = ws_stub(ack=False, close_with_target=True)
    path_key, query_key = "fixture-relative-path", "fixture-relative-query"
    origin = ws.removesuffix("/query")
    endpoint = f"{origin}/{path_key}?key={query_key}"
    try:
        for flags in ((), ("--json",)):
            code, output = run_cli(
                *flags,
                "--endpoint",
                f"{base}/query",
                "doctor",
                "--no-fix",
                "--ws-endpoint",
                endpoint,
                "--lcd-endpoint",
                base,
            )
            assert code == 1
            assert f"{origin} sent no connection_ack" in output
            assert "ConnectionClosedError (close code 1008)" in output
            assert path_key not in output and query_key not in output
    finally:
        stop()


@pytest.mark.parametrize(
    ("status", "diagnostic"),
    [
        (HTTPStatus.SWITCHING_PROTOCOLS, "InvalidHeaderFormat"),
        (HTTPStatus.FORBIDDEN, "InvalidStatus (HTTP status 403)"),
    ],
)
def test_doctor_omits_peer_websocket_handshake_text(http_stub, status, diagnostic):
    base, _state = http_stub
    path_key, query_key = "fixture-handshake-path", "fixture-handshake-query"

    def respond(
        connection: ServerConnection, request: Request, reply: Response
    ) -> Response:
        if status == HTTPStatus.FORBIDDEN:
            return connection.respond(status, request.path)
        del reply.headers["Sec-WebSocket-Protocol"]
        reply.headers["Sec-WebSocket-Protocol"] = request.path
        return reply

    ws, stop = ws_stub(ack=False, process_response=respond)
    origin = ws.removesuffix("/query")
    try:
        for flags in ((), ("--json",)):
            code, output = run_cli(
                *flags,
                "--endpoint",
                f"{base}/query",
                "doctor",
                "--no-fix",
                "--ws-endpoint",
                f"{origin}/{path_key}?key={query_key}",
                "--lcd-endpoint",
                base,
            )
            assert code == 1
            assert f"{origin} sent no connection_ack: {diagnostic}" in output
            assert path_key not in output and query_key not in output
    finally:
        stop()


def test_unreachable_endpoints_fail_and_exit_1():
    dead = f"http://127.0.0.1:{free_port()}"
    code, output = run_cli(
        "--endpoint",
        f"{dead}/query",
        "doctor",
        "--lcd-endpoint",
        dead,
    )
    assert code == 1
    for line in ("fail    keeper", "fail    websocket", "fail    lcd"):
        assert line in output


def test_plain_text_remote_endpoint_fails():
    check = one(doctor.check_keeper, ctx("http://keeper.example/query"))
    assert check.status == "fail"
    assert "HTTPS" in check.summary


def test_offline_makes_no_connections(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("doctor --offline opened a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    code, output = run_cli("doctor", "--offline")
    assert code == 0
    assert "skipped" in output


def test_crashing_check_becomes_a_failure(monkeypatch):
    def boom(_ctx):
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(doctor, "LOCAL_CHECKS", [("boom", boom)])
    checks = doctor.run_checks(ctx(offline=True))
    assert checks[0].status == "fail"
    assert TOKEN not in checks[0].summary


# Reconciling unknown intents


KEY = "20261118T000000000000-r"


def blocked_on(endpoint: str) -> None:
    save(endpoint=endpoint)
    ledger([(KEY, endpoint, ADDR, "unknown")])


@pytest.mark.parametrize(
    "matches", [False, True], ids=["same-origin-mismatch", "match"]
)
def test_doctor_shows_only_endpoint_origins_in_reports_and_hints(
    http_stub, matches, request
):
    base, state = http_stub
    path_key, query_key = "fixture-keeper-path", "fixture-keeper-query"
    lcd_path_key, lcd_query_key = "fixture-lcd-path", "fixture-lcd-query"
    stored_path_key = "fixture-stored-path"
    route = f"/{path_key}/query?key={query_key}"
    endpoint = base + route
    if matches:
        blocked_on(endpoint)
    else:
        save(endpoint=f"{base}/{stored_path_key}/query?key={query_key}")
    ws, stop = ws_stub(ack=True)
    request.addfinalizer(stop)
    ws_origin = ws.removesuffix("/query")
    for flags in ((), ("--json",)):
        code, output = run_cli(
            *flags,
            "--endpoint",
            endpoint,
            "doctor",
            "--no-fix",
            "--ws-endpoint",
            ws_origin + route,
            "--lcd-endpoint",
            f"{base}/{lcd_path_key}?key={lcd_query_key}",
        )
        assert code == 1
        assert ws_origin in output
        for secret in (
            path_key,
            query_key,
            stored_path_key,
            lcd_path_key,
            lcd_query_key,
        ):
            assert secret not in output
        assert "${SAI_RECONCILE_ENDPOINT:?" in output
        if flags:
            checks = {
                check["id"]: check
                for check in json.loads(output)["error"]["details"]["checks"]
            }
            assert checks["keeper"]["details"]["endpoint"] == base
            session = checks["session"]["details"]
            assert session["endpoint"] == base and session["endpointMatches"] is matches
            assert checks["websocket"]["status"] == "pass"
            assert checks["websocket"]["details"]["endpoint"] == ws_origin
            assert checks["lcd"]["status"] == "pass"
            assert checks["lcd"]["details"]["endpoint"] == base
            fix = checks["intents" if matches else "session"]["fix"]
            command = (
                f"intent status {ADDR} --key {KEY}" if matches else "auth login NAME"
            )
            assert command in fix
    assert state.paths == [route, route]


def ledger_status(key: str) -> str:
    with sqlite3.connect(storage.home() / "intents.sqlite3") as connection:
        status = connection.execute(
            "SELECT status FROM intents WHERE id=?", (key,)
        ).fetchone()[0]
    connection.close()
    return status


def test_fix_reconciles_a_blocking_intent_with_keeper(http_stub):
    base, state = http_stub
    endpoint = f"{base}/query"
    blocked_on(endpoint)
    state.intents = [
        {
            "intentKey": KEY,
            "evmAddress": ADDR,
            "execActor": "server",
            "lifecycleStep": "exec_fail",
        }
    ]
    check = one(doctor.check_intents, ctx(endpoint, fix=True))
    assert check.status == "fixed"
    assert check.changes == [f"reconciled intent {KEY} with Keeper"]
    assert check.summary == f"Reconciled intent {KEY} with Keeper"
    assert ledger_status(KEY) == "terminal"
    assert all("mutation" not in query for query in state.queries)


def test_late_keeper_answer_never_writes_the_ledger(http_stub, monkeypatch):
    base, state = http_stub
    endpoint = f"{base}/query"
    blocked_on(endpoint)
    state.intents = [
        {
            "intentKey": KEY,
            "evmAddress": ADDR,
            "execActor": "server",
            "lifecycleStep": "exec_fail",
        }
    ]
    state.intents_drip = 1.5
    monkeypatch.setattr(doctor, "NETWORK_DEADLINE", 0.5)
    start = time.monotonic()
    check = one(doctor.check_intents, ctx(endpoint, fix=True))
    assert time.monotonic() - start < 1.2
    assert check.status == "fail"
    assert check.details["reconcileError"] == "no answer within 0.5 s"
    time.sleep(1.5)  # the answer arrives; the worker must not write it
    assert ledger_status(KEY) == "unknown"


def test_new_unknown_intent_during_reconcile_still_blocks(http_stub):
    base, state = http_stub
    endpoint = f"{base}/query"
    blocked_on(endpoint)
    state.intents = [
        {
            "intentKey": KEY,
            "evmAddress": ADDR,
            "execActor": "server",
            "lifecycleStep": "exec_fail",
        }
    ]
    state.on_intents = lambda: add_unknown("k-new", endpoint=endpoint)
    check = one(doctor.check_intents, ctx(endpoint, fix=True))
    assert check.status == "fail"
    assert check.details["blocking"] == ["k-new"]
    assert check.changes == [f"reconciled intent {KEY} with Keeper"]


def test_locked_ledger_write_gives_up_at_the_deadline(http_stub, monkeypatch):
    base, state = http_stub
    endpoint = f"{base}/query"
    blocked_on(endpoint)
    state.intents = [
        {
            "intentKey": KEY,
            "evmAddress": ADDR,
            "execActor": "server",
            "lifecycleStep": "exec_fail",
        }
    ]
    holder = sqlite3.connect(storage.home() / "intents.sqlite3", isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        outcome = doctor.reconcile_keys(endpoint, ADDR, [KEY], 1.0)
        took = time.monotonic() - start
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert took < 2
    assert "error" in outcome
    time.sleep(0.5)
    assert ledger_status(KEY) == "unknown"


def test_intent_keeper_cannot_settle_still_fails(http_stub):
    base, state = http_stub
    endpoint = f"{base}/query"
    blocked_on(endpoint)
    state.intents = []  # no record yet, but too recent to mark absent
    check = one(doctor.check_intents, ctx(endpoint, fix=True))
    assert check.status == "fail"
    assert check.changes == []
    assert ledger_status(KEY) == "unknown"


def test_offline_or_no_fix_never_reconciles(http_stub):
    base, state = http_stub
    endpoint = f"{base}/query"
    blocked_on(endpoint)
    for context in (ctx(endpoint, fix=True, offline=True), ctx(endpoint)):
        assert one(doctor.check_intents, context).status == "fail"
    assert state.queries == []


# Output


def test_json_report_shape_and_exit_code():
    code, output = run_cli("--json", "doctor", "--offline")
    assert code == 0
    report = json.loads(output)
    assert report["schemaVersion"] == 1
    assert report["overall"] == "warn"
    assert set(report["sai"]) == {"version", "install"}
    datetime.fromisoformat(report["generatedAt"])
    for check in report["checks"]:
        assert set(check) == {
            "id",
            "status",
            "summary",
            "fix",
            "changes",
            "details",
            "durationMs",
        }
        assert check["status"] in doctor.STATUSES
        assert isinstance(check["durationMs"], int)

    save(hours=-1)
    code, output = run_cli("--json", "doctor", "--offline")
    assert code == 1
    error = json.loads(output)["error"]
    assert error["code"] == "INTERNAL_ERROR" and error["retryable"] is False
    assert "expired" in error["message"]
    assert "Foundry keystore" in error["message"]
    details = error["details"]
    assert details["schemaVersion"] == 1 and details["overall"] == "fail"
    checks = {check["id"]: check for check in details["checks"]}
    assert checks["session"]["status"] == "fail"
    recovery = checks["session"]["fix"]
    assert recovery == f"sai --endpoint {ENDPOINT} auth import --addr {ADDR}"
    assert checks["keeper"]["status"] == "skipped"


def test_overall_ignores_skipped():
    checks = [doctor.Check("a", "pass", ""), doctor.Check("b", "skipped", "")]
    assert doctor.overall(checks) == "pass"
    checks.append(doctor.Check("c", "warn", ""))
    assert doctor.overall(checks) == "warn"


def test_session_token_never_printed(http_stub):
    base, _state = http_stub
    save(hours=-1, endpoint=f"{base}/query?token={TOKEN}")
    ledger([("k1", f"{base}/query", ADDR, "unknown")])
    ws, stop = ws_stub(ack=True)
    try:
        outputs = [
            run_cli(
                *prefix,
                "--endpoint",
                f"{base}/query",
                "doctor",
                "--ws-endpoint",
                f"{ws}?token={TOKEN}",
                "--lcd-endpoint",
                f"{base}?token={TOKEN}",
            )[1]
            for prefix in ([], ["--json"])
        ]
    finally:
        stop()
    for output in outputs:
        assert "session" in output
        for secret in (TOKEN, HEADER, PAYLOAD, SIGNATURE):
            assert secret not in output


def test_redaction_covers_rendered_output():
    save()
    check = doctor.Check("x", "fail", f"leaked {TOKEN}", f"fix {TOKEN}")
    assert TOKEN not in doctor.render_text([check])
    assert TOKEN not in doctor.render_json(doctor.report([check]))


# Refactors


@pytest.mark.parametrize(
    ("url", "secure", "ok"),
    [
        ("https://keeper.example/query", "https", True),
        ("http://127.0.0.1:8080/query", "https", True),
        ("http://keeper.example/query", "https", False),
        ("https://u:p@keeper.example/query", "https", False),
        ("wss://keeper.example/query", "wss", True),
        ("ws://localhost/query", "wss", True),
        ("ws://keeper.example/query", "wss", False),
        ("https://keeper.example/query", "wss", False),
    ],
)
def test_check_url(url, secure, ok):
    if ok:
        keeper.check_url(url, "Test", secure)
    else:
        with pytest.raises(ValueError, match="Test"):
            keeper.check_url(url, "Test", secure)


def test_check_ws_refuses_credentials():
    with pytest.raises(stream.StreamError, match="Credentials"):
        stream.check_ws("wss://u:p@keeper.example/query")


def test_request_and_get_json_pass_the_timeout(monkeypatch):
    seen: list[float] = []

    class Response:
        status_code = 200

        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"data": {}}

    def record(*_args, timeout, **_kwargs):
        seen.append(timeout)
        return Response()

    monkeypatch.setattr(keeper.httpx, "post", record)
    monkeypatch.setattr(margin.httpx, "get", record)
    keeper.request("https://keeper.example/query", keeper.ROUTE)
    keeper.request("https://keeper.example/query", keeper.ROUTE, timeout=5)
    margin._get_json("https://lcd.example/x")
    margin._get_json("https://lcd.example/x", timeout=5)
    assert seen == [20, 5, 20, 5]


def test_handshake_succeeds_on_ack():
    ws, stop = ws_stub(ack=True)
    try:
        stream.handshake(ws, 5)
    finally:
        stop()
