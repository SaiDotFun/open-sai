"""Check bare commands against the current Python CLI and explicit help."""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from sai_cli.sai_cli import main
from sai_cli.tests.test_bare_help import required_commands

with Path(__file__).with_name("cases.toml").open("rb") as manifest:
    BARE_LEAVES = [
        tuple(case["args"])
        for case in tomllib.load(manifest)["case"]
        if case["name"].startswith("bare-help-")
        or case["name"] == "account-balances-missing-address"
    ]


def invoke(binary: str, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    home.mkdir(exist_ok=True)
    return subprocess.run(
        [binary, *args],
        cwd=home,
        env={
            "HOME": str(home),
            "SAI_HOME": str(home / "sai"),
            "SAI_LOG": "false",
            "SAI_UPDATE_AUTO": "false",
            "SAI_ENDPOINT": "http://127.0.0.1:1",
            "SAI_WS_ENDPOINT": "ws://127.0.0.1:1",
            "SAI_LCD_ENDPOINT": "http://127.0.0.1:1",
            "PATH": os.defpath,
            "TERM": "dumb",
            "PYTHONUTF8": "1",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.fixture
def binaries(cli_pair: tuple[tuple[str, ...], tuple[str, ...]]) -> tuple[str, str]:
    return str(Path(sys.executable).with_name("sai")), cli_pair[1][0]


def test_manifest_covers_every_required_python_command() -> None:
    assert sorted(BARE_LEAVES) == sorted(required_commands(main))


@pytest.mark.parametrize("args", BARE_LEAVES, ids=" ".join)
def test_bare_leaf_prints_full_help_and_json_usage(
    binaries: tuple[str, str], tmp_path: Path, args: tuple[str, ...]
) -> None:
    errors = []
    for label, binary in zip(("python", "rust"), binaries, strict=True):
        home = tmp_path / label
        help_result = invoke(binary, home, *args, "--help")
        bare = invoke(binary, home, *args)
        machine = invoke(binary, home, "--json", *args)

        assert help_result.returncode == 0
        assert not help_result.stderr
        assert "Usage:" in help_result.stdout and "--help" in help_result.stdout
        assert bare.returncode == machine.returncode == 2
        assert bare.stdout == machine.stderr == ""
        assert bare.stderr == help_result.stdout
        error = json.loads(machine.stdout)["error"]
        assert error["code"] == "INVALID_REQUEST"
        assert error["message"].startswith("Missing ")
        errors.append(error)
    assert errors[0] == errors[1]


@pytest.mark.parametrize(
    "group",
    [
        "account",
        "auth",
        "config",
        "intent",
        "logs",
        "market",
        "mcp",
        "skill",
        "trade",
        "watch",
    ],
)
def test_bare_group(binaries: tuple[str, str], tmp_path: Path, group: str) -> None:
    for label, binary in zip(("python", "rust"), binaries, strict=True):
        home = tmp_path / label
        help_result = invoke(binary, home, group, "--help")
        assert help_result.returncode == 0
        assert help_result.stderr == "" and f"Usage: sai {group}" in help_result.stdout

        bare = invoke(binary, home, group)
        if group == "mcp":
            assert bare.returncode == 0
            assert bare.stderr == "" and "Usage: sai mcp" in bare.stdout
        else:
            assert bare.returncode == 2
            assert bare.stdout == ""
            assert bare.stderr.startswith(f"Usage: sai {group} ")
            assert f"Try 'sai {group} --help' for help." in bare.stderr
            expected = "Invalid arguments to auth." if group == "auth" else "Options:"
            assert expected in bare.stderr


def test_root_help_and_json_usage(binaries: tuple[str, str], tmp_path: Path) -> None:
    for label, binary in zip(("python", "rust"), binaries, strict=True):
        home = tmp_path / label
        help_result = invoke(binary, home, "--help")
        assert help_result.returncode == 0
        assert help_result.stderr == "" and "Usage: sai " in help_result.stdout

        machine = invoke(binary, home, "--json")
        assert machine.returncode == 2
        assert machine.stderr == ""
        assert json.loads(machine.stdout) == {
            "error": {
                "code": "INVALID_REQUEST",
                "message": "Pass a command when using --json.",
                "retryable": False,
            }
        }


def test_bare_root_starts_repl(binaries: tuple[str, str], tmp_path: Path) -> None:
    for label, binary in zip(("python", "rust"), binaries, strict=True):
        home = tmp_path / label
        bare = invoke(binary, home)
        assert bare.returncode == 0
        assert bare.stderr == ""
        assert "Enter a command, 'help', or 'exit'." in bare.stdout


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("config", "set", "update.auto"), "Missing argument 'VALUE'."),
        (("trade", "open", "--market", "1"), "Missing option '--side'."),
    ],
)
def test_partial_input_reports_the_missing_parameter(
    binaries: tuple[str, str], tmp_path: Path, args: tuple[str, ...], message: str
) -> None:
    for label, binary in zip(("python", "rust"), binaries, strict=True):
        partial = invoke(binary, tmp_path / label, *args)
        assert partial.returncode == 2
        assert partial.stdout == ""
        assert message in partial.stderr
        assert "Options:" not in partial.stderr
