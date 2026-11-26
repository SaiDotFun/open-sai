"""Check help for commands with required Click parameters."""

import json
from collections.abc import Iterator
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from sai_cli.sai_cli import main


def required_commands(
    group: click.Group, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[str, ...]]:
    for name, command in group.commands.items():
        path = (*prefix, name)
        if isinstance(command, click.Group):
            yield from required_commands(command, path)
        elif any(param.required for param in command.params):
            yield path


@pytest.mark.parametrize("path", list(required_commands(main)), ids=" ".join)
def test_bare_command_matches_full_help(path: tuple[str, ...], tmp_path: Path) -> None:
    runner = CliRunner(env={"SAI_HOME": str(tmp_path)})
    help_result = runner.invoke(main, [*path, "--help"])
    bare = runner.invoke(main, path)
    machine = runner.invoke(main, ["--json", *path])

    assert help_result.exit_code == 0
    assert bare.exit_code == machine.exit_code == 2
    assert bare.stdout == machine.stderr == ""
    assert bare.stderr == help_result.stdout
    error = json.loads(machine.stdout)["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert error["message"].startswith("Missing ")


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["config", "set", "update.auto"], "Missing argument 'VALUE'"),
        (["trade", "open", "--market", "1"], "Missing option '--side'"),
    ],
)
def test_partial_command_reports_missing_parameter(
    args: list[str], error: str, tmp_path: Path
) -> None:
    result = CliRunner(env={"SAI_HOME": str(tmp_path)}).invoke(main, args)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert error in result.stderr
    assert "Options:" not in result.stderr


@pytest.mark.parametrize("json_output", [False, True])
def test_repl_bare_command_and_partial_command(
    json_output: bool, tmp_path: Path
) -> None:
    runner = CliRunner(env={"SAI_HOME": str(tmp_path)})
    command = "--json trade open" if json_output else "trade open"
    result = runner.invoke(
        main, input=f"{command}\nconfig set update.auto\nhelp config\nexit\n"
    )

    assert result.exit_code == 0
    assert "Usage: sai config" in result.stdout
    assert "Missing argument 'VALUE'" in result.stderr
    if json_output:
        _, start, payload = result.stdout.partition('{"error":')
        assert start
        error = json.loads(start + payload.splitlines()[0])["error"]
        assert error["code"] == "INVALID_REQUEST"
        assert "Missing option '--market'" in error["message"]
        assert "Usage: sai trade open" not in result.stderr
    else:
        help_result = runner.invoke(main, ["trade", "open", "--help"], prog_name="sai")
        assert help_result.stdout in result.stderr
        assert "Missing option '--market'" not in result.stderr


def test_margin_cap_help_discloses_quote_scope() -> None:
    result = CliRunner().invoke(main, ["trade", "margin", "--help"])
    assert result.exit_code == 0, result.output
    help_text = " ".join(result.stdout.split())
    assert "--max-collateral-usd DECIMAL Cap the margin quote only." in help_text
    assert "Actual collateral in USD can exceed this cap." in help_text
