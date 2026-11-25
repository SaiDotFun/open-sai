"""Check Rust CLI fixes against the Python CLI from the checkout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp_clients import SECRET, seed_clients


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
