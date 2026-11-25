"""Check Rust CLI fixes against the Python CLI from the checkout."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from mcp_clients import SECRET, seed_clients
from scenarios import loopback


@pytest.fixture
def wave_clis(cli_pair: tuple[tuple[str, ...], tuple[str, ...]]) -> tuple[str, str]:
    return str(Path(sys.executable).with_name("sai")), cli_pair[1][0]


def run_cli(
    binary: str, home: Path, *args: str, overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(home),
        "SAI_HOME": str(home / "sai"),
        "PATH": os.defpath,
        "SAI_UPDATE_AUTO": "false",
        "SAI_LOG": "false",
        "PYTHONUTF8": "1",
    }
    env.update(overrides or {})
    return subprocess.run(
        [binary, *args],
        cwd=home,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize(
    "command",
    [("config", "list"), ("mcp", "serve"), ("doctor", "--offline", "--no-fix")],
    ids=["config", "mcp", "doctor"],
)
@pytest.mark.parametrize("flags", [(), ("--json",)], ids=["text", "json"])
def test_invalid_update_environment_exits_2(
    wave_clis: tuple[str, str],
    tmp_path: Path,
    command: tuple[str, ...],
    flags: tuple[str, ...],
) -> None:
    invalid = "synthetic-invalid-update-setting"
    message = "SAI_UPDATE_AUTO: Invalid value for update.auto: expected true or false"
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        home.mkdir()
        result = run_cli(
            binary, home, *flags, *command, overrides={"SAI_UPDATE_AUTO": invalid}
        )
        assert result.returncode == 2
        assert invalid not in result.stdout + result.stderr
        if flags:
            assert not result.stderr
            error = json.loads(result.stdout)["error"]
            assert error["code"] == "INVALID_REQUEST"
            assert message in error["message"]
        else:
            assert not result.stdout
            assert message in result.stderr


@pytest.mark.parametrize("dry_run", [(), ("--dry-run",)], ids=["apply", "dry-run"])
def test_install_reports_differing_claude_entry(
    wave_clis: tuple[str, str], tmp_path: Path, dry_run: tuple[str, ...]
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        home.mkdir()
        overrides = {"PATH": os.defpath}
        original = seed_clients(home, overrides, "retired")
        result = run_cli(
            binary,
            home,
            "--json",
            "mcp",
            "install",
            "claude",
            *dry_run,
            overrides=overrides,
        )
        assert (home / ".claude.json").read_bytes() == original[".claude.json"]
        assert not (home / "argv.jsonl").exists()
        assert result.returncode == 1
        assert SECRET not in result.stdout + result.stderr
        assert "token=private" not in result.stdout + result.stderr
        details = json.loads(result.stdout)["error"]["details"]
        assert details["reconnectNeeded"] is False
        [report] = details["results"]
        assert report["action"] == "failed"
        assert report["changed"] is False
        assert report["scope"] == "user"
        assert report["path"] == str(home / ".claude.json")
        assert "differs in command, args, env." in report["fix"]
        assert "test -f" in report["fix"]
        assert "claude mcp add --scope user sai" in report["fix"]


def test_doctor_validates_codex_repair_before_writing(
    wave_clis: tuple[str, str], tmp_path: Path
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        home.mkdir()
        overrides = {"PATH": os.defpath}
        original = seed_clients(home, overrides, "partial")
        config = home / ".codex/config.toml"
        before = config.stat()
        result = run_cli(
            binary, home, "--json", "doctor", "--offline", overrides=overrides
        )
        assert result.returncode == 1
        assert config.read_bytes() == original[".codex/config.toml"]
        after = config.stat()
        assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
            before.st_ino,
            before.st_mode,
            before.st_mtime_ns,
        )
        assert set(config.parent.iterdir()) == {config}
        calls = (home / "argv.jsonl").read_text()
        assert SECRET not in calls + result.stdout + result.stderr
        assert "token=private" not in calls + result.stdout + result.stderr
        previews = [
            call for line in calls.splitlines() if "-c" in (call := json.loads(line))
        ]
        assert len(previews) == 1
        assert 'mcp_servers.sai.args=["mcp", "serve"]' in previews[0]
        checks = json.loads(result.stdout)["error"]["details"]["checks"]
        [codex] = [check for check in checks if check["id"] == "mcp.codex"]
        assert codex["status"] == "fail"
        assert (
            "Codex rejected the proposed repair. The original Sai entry is unchanged."
            in codex["summary"]
        )
        assert "codex mcp add sai" in codex["fix"]
        assert "<original-launcher>" in codex["fix"]


@pytest.mark.parametrize("profile", ["config-only", "fake-ledger", "dedicated"])
@pytest.mark.parametrize("no_fix", [(), ("--no-fix",)], ids=["apply", "no-fix"])
def test_doctor_needs_trader_data_to_tighten_sai_home(
    wave_clis: tuple[str, str], tmp_path: Path, profile: str, no_fix: tuple[str, ...]
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        sai_home = home / "sai directory"
        sai_home.mkdir(parents=True, mode=0o755)
        config = sai_home / "config.toml"
        config.write_text('[unrelated]\nname = "fixture"\n')
        trader = sai_home / "trader"
        if profile != "config-only":
            trader.mkdir(mode=0o755)
            with sqlite3.connect(trader / "intents.sqlite3") as ledger:
                if profile == "dedicated":
                    ledger.execute(
                        "CREATE TABLE intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL,"
                        " account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,"
                        " entity TEXT NOT NULL)"
                    )
                else:
                    ledger.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        result = run_cli(
            binary,
            home,
            "--json",
            "doctor",
            "--offline",
            *no_fix,
            overrides={"SAI_HOME": str(sai_home)},
        )
        report = json.loads(result.stdout)
        checks = (
            report["error"]["details"]["checks"]
            if result.returncode
            else report["checks"]
        )
        [check] = [check for check in checks if check["id"] == "home"]
        assert config.read_text() == '[unrelated]\nname = "fixture"\n'
        if profile == "dedicated" and not no_fix:
            assert stat.S_IMODE(sai_home.stat().st_mode) == 0o700
            assert stat.S_IMODE(trader.stat().st_mode) == 0o700
            assert check["status"] == "fixed"
        else:
            assert stat.S_IMODE(sai_home.stat().st_mode) == 0o755
            assert check["status"] == "warn"
            if profile == "dedicated":
                assert check["fix"].startswith(f"chmod go-rwx '{sai_home}'; ")
                assert stat.S_IMODE(trader.stat().st_mode) == 0o755
            else:
                assert "moving the profile to a dedicated directory" in check["fix"]


def test_doctor_lcd_keeps_the_query_after_the_node_info_path(
    wave_clis: tuple[str, str], tmp_path: Path
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        home.mkdir()
        with loopback("normal", lcd_query=True) as endpoints:
            result = run_cli(
                binary,
                home,
                "--json",
                "doctor",
                "--no-fix",
                overrides={
                    "SAI_ENDPOINT": endpoints.keeper,
                    "SAI_WS_ENDPOINT": endpoints.ws,
                    "SAI_LCD_ENDPOINT": endpoints.http + "/?project=fixture",
                },
            )
            assert result.returncode == 0, result.stdout
            [lcd] = [
                check
                for check in json.loads(result.stdout)["checks"]
                if check["id"] == "lcd"
            ]
            assert lcd["status"] == "pass"
            assert sum(request.label == "node_info" for request in endpoints.wire) == 1
            assert not endpoints.errors
            assert "project=fixture" not in result.stdout + result.stderr
