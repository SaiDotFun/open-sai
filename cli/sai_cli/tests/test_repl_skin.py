"""Check the REPL skin's Sai branding, history location, and skill."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from sai_cli.sai_cli import main, repl, repl_args
from sai_cli.utils.repl_skin import ReplSkin

REPO = Path(__file__).resolve().parents[3]
PACKAGE_SKILL = REPO / "cli" / "sai_cli" / "skills" / "SKILL.md"
REPO_SKILL = REPO / "skills" / "sai" / "SKILL.md"
# Split so that searching the repository for the name finds only attribution.
UPSTREAM_NAME = "cli-" + "anything"


def test_history_lives_under_sai_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    skin = ReplSkin("sai", version="0.1.0")
    history = Path(skin.history_file)
    assert history == tmp_path / "trader" / "history"
    assert history.parent.is_dir()
    assert history.parent.stat().st_mode & 0o777 == 0o700


def test_explicit_history_file_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path / "home"))
    skin = ReplSkin("sai", history_file=str(tmp_path / "h"))
    assert skin.history_file == str(tmp_path / "h")
    assert not (tmp_path / "home").exists()


def test_skill_identity_and_install_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    skin = ReplSkin("sai", version="0.1.0")
    assert skin.skill_id == "sai"
    assert skin.skill_install_cmd == (
        "npx skills add SaiDotFun/open-sai --skill sai -g -y"
    )
    assert skin.skill_path is not None
    assert Path(skin.skill_path).resolve() == REPO_SKILL.resolve()


def test_banner_shows_sai_brand(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    ReplSkin("sai", version="0.1.0").print_banner()
    out = capsys.readouterr().out
    assert "Sai · Trader CLI" in out
    assert "--skill sai" in out
    assert UPSTREAM_NAME not in out.lower()


@pytest.mark.parametrize("path", [PACKAGE_SKILL, REPO_SKILL])
def test_skill_is_named_sai(path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\nname: sai\n")


def test_packaged_skill_is_a_link_to_the_repository_skill():
    assert PACKAGE_SKILL.parent.is_symlink()
    assert PACKAGE_SKILL.read_bytes() == REPO_SKILL.read_bytes()


def test_repl_help_maps_to_click_help():
    assert repl_args("help") == ["--help"]
    assert repl_args("help trade open") == ["trade", "open", "--help"]
    assert repl_args("market list") == ["market", "list"]


def test_repl_writes_history_and_shows_help(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    ctx = click.Context(
        main,
        obj={"flags": {}, "settings": None, "json": True},
    )
    with create_pipe_input() as pipe:
        pipe.send_text("help\rhelp trade\r")
        pipe.close()
        repl(ctx, pt_input=pipe, pt_output=DummyOutput())
    out = capsys.readouterr().out
    assert "Usage: sai" in out
    assert "partial-close" in out
    history = (tmp_path / "trader" / "history").read_text(encoding="utf-8")
    assert "+help\n" in history
    assert "+help trade\n" in history


def test_repl_history_excludes_import_and_rejected_auth_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    marker = "synthetic-repl-credential-marker"
    ctx = click.Context(main, obj={"flags": {}, "settings": None, "json": True})
    with create_pipe_input() as pipe:
        pipe.send_text(
            f"auth import {marker}\r"
            f"auth --{marker} import\r"
            f"auth use '{marker}\r"
            "auth import --help\r"
            "auth list\r"
        )
        pipe.close()
        repl(ctx, pt_input=pipe, pt_output=DummyOutput())

    history = (tmp_path / "trader" / "history").read_text(encoding="utf-8")
    assert marker not in history and "auth import" not in history
    assert "+auth list\n" in history


def test_history_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    history = tmp_path / "trader" / "history"
    history.parent.mkdir(parents=True)
    history.write_text("", encoding="utf-8")
    history.chmod(0o644)
    with create_pipe_input() as pipe:
        ReplSkin("sai").create_prompt_session(input=pipe, output=DummyOutput())
    assert history.stat().st_mode & 0o777 == 0o600
