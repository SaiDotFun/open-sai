"""`sai mcp install|list|uninstall` against fake Claude Code and Codex
programs and a temporary home; the real home directory is never touched."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from sai_cli import mcp_install
from sai_cli import sai_cli as cli
from sai_cli.sai_cli import main

# Built from parts so the retired-names test does not match this file.
OLD_COMMAND = "sai" + "-mcp"
SECRET = "sk-SECRET-do-not-print"
OTHER = "otherserver-private-name"

# Both fakes record argv to $FAKE_LOG and keep state where the real client
# would, so a second run sees the first run's change.
FAKE_CLAUDE = """
import json, os, sys
from pathlib import Path
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(sys.argv) + "\\n")
args = sys.argv[1:]
assert args[:2] == ["mcp", "add"] or args[:2] == ["mcp", "remove"], args
scope = args[args.index("--scope") + 1]
path = (
    Path.cwd() / ".mcp.json"
    if scope == "project"
    else Path(os.environ["HOME"]) / ".claude.json"
)
data = json.loads(path.read_text()) if path.exists() else {}
holder = data
if scope == "local":
    holder = data.setdefault("projects", {}).setdefault(str(Path.cwd()), {})
servers = holder.setdefault("mcpServers", {})
if args[1] == "remove":
    if "sai" not in servers:
        sys.stderr.write(f'No MCP server named "sai" in {scope} scope\\n')
        sys.exit(1)
    del servers["sai"]
else:
    dash = args.index("--")
    env = dict(
        a.split("=", 2)[1:] for a in args[:dash] if a.startswith("--env=")
    )
    command = args[dash + 1 :]
    servers["sai"] = {
        "type": "stdio", "command": command[0], "args": command[1:], "env": env
    }
path.write_text(json.dumps(data))
"""

FAKE_CODEX = """
import json, os, sys
from pathlib import Path
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(sys.argv) + "\\n")
args = sys.argv[1:]
path = Path(os.environ["CODEX_HOME"]) / "fake-state.json"
state = json.loads(path.read_text()) if path.exists() else {}
# Stands in for Codex's own project rules: a project entry Codex would load
# from this directory is given in ./fake-codex-project.json.
project = Path.cwd() / "fake-codex-project.json"
if args[:2] == ["mcp", "get"] and project.exists():
    data = json.loads(project.read_text())
    if "transport" not in data:
        data = {"transport": data}
    print(json.dumps({"name": "sai", "enabled": True, **data}))
elif args[:2] == ["mcp", "get"]:
    if "sai" not in state:
        sys.stderr.write("Error: No MCP server named 'sai' found.\\n")
        sys.exit(1)
    enabled = state.get("sai_enabled", True)
    print(json.dumps({"name": "sai", "enabled": enabled, "transport": state["sai"]}))
elif args[:2] == ["mcp", "remove"]:
    state.pop("sai", None)
    state.pop("sai_enabled", None)
elif args[:2] == ["mcp", "add"]:
    dash = args.index("--")
    env = dict(
        a.split("=", 2)[1:] for a in args[:dash] if a.startswith("--env=")
    )
    command = args[dash + 1 :]
    state["sai"] = {
        "type": "stdio", "command": command[0], "args": command[1:],
        "env": env, "env_vars": [], "cwd": None,
    }
else:
    sys.exit(2)
path.write_text(json.dumps(state))
"""


# Codex is asked twice: from CODEX_HOME for the user entry, then from the
# working directory for the entry it uses here.
GETS = [["mcp", "get", "sai", "--json"]] * 2
# After a change, Codex is asked again: both reads, then the working directory.
CHECK = [["mcp", "get", "sai", "--json"]] * 3


def write_exe(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return path


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.bin = root / "bin"
        self.codex_home = root / "codex-home"
        self.project = root / "project"
        self.log = root / "argv.log"
        self.sai = self.bin / "sai"

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        lines = self.log.read_text().splitlines()
        return [json.loads(line)[1:] for line in lines]

    def clear(self) -> None:
        self.log.unlink(missing_ok=True)

    def claude_json(self) -> dict[str, Any]:
        return json.loads((self.home / ".claude.json").read_text())

    def cursor_path(self) -> Path:
        return self.home / ".cursor" / "mcp.json"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    e = Env(tmp_path)
    for path in (e.home, e.bin, e.codex_home, e.project):
        path.mkdir()
    write_exe(e.sai, "print('fake sai')\n")
    write_exe(e.bin / "claude", FAKE_CLAUDE)
    write_exe(e.bin / "codex", FAKE_CODEX)
    for name in (
        "SAI_HOME",
        "CLAUDE_CONFIG_DIR",
        "UV_CACHE_DIR",
        "XDG_CACHE_HOME",
        "LOCALAPPDATA",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(e.home))
    monkeypatch.setenv("CODEX_HOME", str(e.codex_home))
    monkeypatch.setenv("PATH", str(e.bin))
    monkeypatch.setenv("FAKE_LOG", str(e.log))
    monkeypatch.chdir(e.project)
    monkeypatch.setattr(mcp_install, "running_prefix", lambda: str(tmp_path))
    monkeypatch.setattr(cli, "interactive", lambda: False)
    return e


def invoke(*args: str, stdin: str | None = None) -> Any:
    return CliRunner().invoke(main, ["--json", *args], input=stdin)


def payload(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout)


def actions(result: Any) -> dict[str, str]:
    return {r["client"]: r["action"] for r in payload(result)["results"]}


def failure(result: Any, exit_code: int = 1) -> str:
    assert result.exit_code == exit_code
    envelope = payload(result)
    assert set(envelope) == {"error"}
    error = envelope["error"]
    assert error["code"] == ("INVALID_REQUEST" if exit_code == 2 else "INTERNAL_ERROR")
    assert error["retryable"] is False
    return error["message"]


def serve(e: Env) -> list[str]:
    return [str(e.sai), "mcp", "serve"]


def test_install_runs_each_clients_add_command(env: Env):
    result = invoke("mcp", "install", "claude", "codex", "cursor")
    assert result.exit_code == 0, result.output
    assert actions(result) == {
        "claude": "added",
        "codex": "added",
        "cursor": "added",
    }
    assert payload(result)["reconnectNeeded"] is True
    sai = str(env.sai)
    assert env.calls() == [
        ["mcp", "add", "--scope", "user", "sai", "--", sai, "mcp", "serve"],
        *GETS,
        ["mcp", "add", "sai", "--", sai, "mcp", "serve"],
        *CHECK,
    ]
    assert env.claude_json()["mcpServers"]["sai"]["command"] == sai
    cursor = json.loads(env.cursor_path().read_text())
    assert cursor["mcpServers"]["sai"] == {"command": sai, "args": ["mcp", "serve"]}
    assert "enable" in result.stderr


def test_rerun_is_unchanged_and_runs_no_add(env: Env):
    assert invoke("mcp", "install", "claude", "codex", "cursor").exit_code == 0
    env.clear()
    result = invoke("mcp", "install", "claude", "codex", "cursor")
    assert result.exit_code == 0
    assert set(actions(result).values()) == {"unchanged"}
    assert payload(result)["reconnectNeeded"] is False
    assert env.calls() == GETS


def test_an_old_entry_is_removed_then_added(env: Env):
    (env.home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"sai": {"command": OLD_COMMAND, "args": []}}})
    )
    (env.codex_home / "fake-state.json").write_text(
        json.dumps({"sai": {"type": "stdio", "command": OLD_COMMAND, "args": []}})
    )
    result = invoke("mcp", "install", "claude", "codex")
    assert actions(result) == {"claude": "replaced", "codex": "replaced"}
    sai = str(env.sai)
    assert env.calls() == [
        ["mcp", "remove", "--scope", "user", "sai"],
        ["mcp", "add", "--scope", "user", "sai", "--", sai, "mcp", "serve"],
        *GETS,
        ["mcp", "remove", "sai"],
        ["mcp", "add", "sai", "--", sai, "mcp", "serve"],
        *CHECK,
    ]


def test_dry_run_runs_no_client_and_writes_nothing(env: Env):
    (env.home / ".cursor").mkdir()
    result = invoke("mcp", "install", "--all", "--dry-run")
    assert result.exit_code == 0
    rows = payload(result)["results"]
    assert {r["client"]: (r["action"], r["would"]) for r in rows} == {
        "claude": ("planned", "added"),
        "codex": ("planned", "added"),
        "cursor": ("planned", "added"),
    }
    assert rows[0]["argv"] == [
        [str(env.bin / "claude"), "mcp", "add", "--scope", "user", "sai"]
        + ["--", str(env.sai), "mcp", "serve"]
    ]
    assert payload(result)["reconnectNeeded"] is False
    # Only Codex's read-only `get`s ran.
    assert env.calls() == GETS
    assert not (env.home / ".claude.json").exists()
    assert not env.cursor_path().exists()


def test_cursor_keeps_other_servers_and_the_file_mode(env: Env):
    path = env.cursor_path()
    path.parent.mkdir()
    other = {"command": "npx", "env": {"TOKEN": SECRET}}
    path.write_text(json.dumps({"mcpServers": {OTHER: other}, "extra": 1}))
    path.chmod(0o640)
    assert invoke("mcp", "install", "cursor").exit_code == 0
    data = json.loads(path.read_text())
    assert data["mcpServers"][OTHER] == other
    assert data["extra"] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    result = invoke("mcp", "uninstall", "cursor")
    assert actions(result) == {"cursor": "removed"}
    data = json.loads(path.read_text())
    assert data["mcpServers"] == {OTHER: other}
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert [p.name for p in path.parent.iterdir()] == ["mcp.json"]


def test_new_cursor_file_is_private(env: Env):
    assert invoke("mcp", "install", "cursor").exit_code == 0
    assert stat.S_IMODE(env.cursor_path().stat().st_mode) == 0o600


def test_uninstall_runs_remove_and_reports_absent(env: Env):
    assert invoke("mcp", "install", "claude", "codex").exit_code == 0
    env.clear()
    result = invoke("mcp", "uninstall", "--all")
    assert result.exit_code == 0
    assert actions(result) == {
        "claude": "removed",
        "codex": "removed",
        "cursor": "absent",
    }
    assert env.calls() == [
        ["mcp", "remove", "--scope", "user", "sai"],
        *GETS,
        ["mcp", "remove", "sai"],
        *CHECK,
    ]
    env.clear()
    again = invoke("mcp", "uninstall", "claude", "codex", "--dry-run")
    assert actions(again) == {"claude": "absent", "codex": "absent"}
    assert env.calls() == GETS


def test_uv_cache_path_is_refused(env: Env, monkeypatch: pytest.MonkeyPatch):
    cache = env.root / "uv-cache"
    tool_bin = cache / "archive-v0" / "abc" / "bin"
    tool_bin.mkdir(parents=True)
    write_exe(tool_bin / "sai", "")
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    monkeypatch.setenv("PATH", f"{tool_bin}{os.pathsep}{env.bin}")
    result = invoke("mcp", "install", "claude")
    assert "uv tool install" in failure(result, 3)
    assert env.calls() == []


def test_default_uv_cache_is_refused_without_variables(env: Env, monkeypatch):
    tool_bin = env.home / ".cache" / "uv" / "archive-v0" / "x" / "bin"
    tool_bin.mkdir(parents=True)
    write_exe(tool_bin / "sai", "")
    monkeypatch.setenv("PATH", f"{tool_bin}{os.pathsep}{env.bin}")
    result = invoke("mcp", "install", "--all", "--dry-run")
    assert "inside the uv cache" in failure(result, 3)


def test_argv_after_dash_overrides_the_sai_on_path(env: Env):
    dev = ["uv", "run", "--directory", "/src/open-sai", "sai", "mcp", "serve"]
    result = invoke("mcp", "install", "claude", "--", *dev)
    assert result.exit_code == 0, result.output
    assert env.calls() == [
        ["mcp", "add", "--scope", "user", "sai", "--", *dev],
    ]
    assert payload(result)["results"][0]["command"] == dev


def test_install_warns_about_a_different_install(env: Env, monkeypatch):
    monkeypatch.setattr(mcp_install, "running_prefix", lambda: "/elsewhere")
    result = invoke("mcp", "install", "claude", "--dry-run")
    assert "different install" in result.stderr


def test_sai_home_is_the_only_env_carried(env: Env, monkeypatch):
    sai_home = env.root / "sai-home"
    monkeypatch.setenv("SAI_HOME", str(sai_home))
    monkeypatch.setenv("SOME_TOKEN", SECRET)
    result = invoke("mcp", "install", "claude", "codex", "cursor")
    assert result.exit_code == 0
    flag = f"--env=SAI_HOME={sai_home}"
    assert [c for c in env.calls() if c[1] == "add"] == [
        ["mcp", "add", "--scope", "user", "sai", flag, "--", *serve(env)],
        ["mcp", "add", "sai", flag, "--", *serve(env)],
    ]
    cursor = json.loads(env.cursor_path().read_text())["mcpServers"]["sai"]
    assert cursor["env"] == {"SAI_HOME": str(sai_home)}
    # A registration without SAI_HOME differs and is replaced.
    monkeypatch.delenv("SAI_HOME")
    again = invoke("mcp", "install", "claude", "codex", "cursor")
    assert set(actions(again).values()) == {"replaced"}
    assert "env" not in json.loads(env.cursor_path().read_text())["mcpServers"]["sai"]


def test_no_client_without_terminal_lists_and_changes_nothing(env: Env):
    (env.home / ".claude").mkdir()
    result = invoke("mcp", "install")
    assert result.exit_code == 0
    data = payload(result)
    assert data["command"] == serve(env)
    detected = {r["client"]: (r["detected"], r["via"]) for r in data["clients"]}
    assert detected == {
        "claude": (True, "cli"),
        "codex": (True, "cli"),
        "cursor": (False, None),
    }
    assert "Nothing changed" in result.stderr
    assert [c for c in env.calls() if c[1] != "get"] == []


def test_uninstall_without_client_or_terminal_is_a_usage_error(env: Env):
    assert invoke("mcp", "install", "codex").exit_code == 0
    env.clear()
    result = invoke("mcp", "uninstall")
    assert "codex (user)" in failure(result, 2)
    assert [c for c in env.calls() if c[1] != "get"] == []


def test_terminal_picker_registers_the_chosen_clients(env: Env, monkeypatch):
    monkeypatch.setattr(cli, "interactive", lambda: True)
    result = invoke("mcp", "install", stdin="2\n")
    assert result.exit_code == 0, result.output
    assert actions(result) == {"codex": "added"}


def test_unknown_client_is_a_usage_error(env: Env):
    result = invoke("mcp", "install", "vim")
    assert result.exit_code == 2
    assert env.calls() == []


def test_codex_has_no_project_scope(env: Env):
    result = invoke("mcp", "install", "codex", "claude", "--scope", "project")
    message = failure(result)
    assert "codex (project): failed" in message
    assert "claude (project): added" in message
    details = payload(result)["error"]["details"]
    assert details["reconnectNeeded"] is True
    assert [
        (row["client"], row["scope"], row["action"], row["changed"])
        for row in details["results"]
    ] == [
        ("codex", "project", "failed", False),
        ("claude", "project", "added", True),
    ]
    assert json.loads((env.project / ".mcp.json").read_text())["mcpServers"]["sai"]


def test_missing_client_cli_fails_that_client_only(env: Env):
    (env.bin / "claude").unlink()
    result = invoke("mcp", "install", "claude", "cursor")
    message = failure(result)
    assert "claude (user): failed" in message and "not on PATH" in message
    assert "cursor (user): added" in message


def test_list_reads_only_the_sai_entry_and_prints_no_secrets(env: Env):
    other = {
        "type": "http",
        "url": "https://secret.example/mcp",
        "headers": {"Authorization": f"Bearer {SECRET}"},
    }
    (env.home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    OTHER: other,
                    "sai": {
                        "command": OLD_COMMAND,
                        "args": [],
                        "env": {"TOKEN": SECRET},
                    },
                },
                "projects": {str(env.project): {"mcpServers": {OTHER: other}}},
            }
        )
    )
    (env.codex_home / "fake-state.json").write_text(
        json.dumps(
            {
                "sai": {
                    "type": "stdio",
                    "command": str(env.sai),
                    "args": ["mcp", "serve"],
                    "env": {"TOKEN": SECRET},
                }
            }
        )
    )
    env.cursor_path().parent.mkdir()
    env.cursor_path().write_text(json.dumps({"mcpServers": {OTHER: other}}))
    outputs = [
        invoke("mcp", "list"),
        invoke("mcp", "install", "--all", "--dry-run"),
        invoke("mcp", "install"),
        invoke("mcp", "uninstall"),
    ]
    rows = {r["client"]: r for r in payload(outputs[0])["clients"]}
    assert rows["claude"]["registeredCommand"] == [OLD_COMMAND]
    assert rows["claude"]["current"] is False
    assert rows["claude"]["scope"] == "user"
    # The codex entry runs the right command but carries an extra env name.
    assert rows["codex"]["current"] is False
    assert rows["codex"]["staleEnv"] == ["TOKEN"]
    assert rows["cursor"]["registered"] is False
    assert set(rows["claude"]) == {
        "client",
        "detected",
        "via",
        "registered",
        "current",
        "registeredCommand",
        "scope",
        "registrations",
        "staleEnv",
    }
    for result in outputs:
        text = result.stdout + result.stderr
        assert SECRET not in text
        assert OTHER not in text
        assert "secret.example" not in text
    # list is read-only: only Codex's get ran.
    assert {c[1] for c in env.calls()} == {"get"}


def test_unreadable_config_names_the_file_not_its_contents(env: Env):
    (env.home / ".claude.json").write_text(f'{{"broken": "{SECRET}"')
    result = invoke("mcp", "install", "claude")
    error = failure(result)
    assert ".claude.json" in error
    assert SECRET not in result.stdout + result.stderr


def test_client_errors_skip_warning_lines():
    out = "WARNING: could not create aliases\nError: config is locked\n"
    assert mcp_install.first_line(out) == "Error: config is locked"
    assert mcp_install.first_line("one\nlast words\n") == "last words"


def claude_entry(argv: list[str], env: dict[str, str] | None = None) -> dict:
    return {"type": "stdio", "command": argv[0], "args": argv[1:], "env": env or {}}


def test_extra_env_is_stale_and_replaced(env: Env):
    endpoint = "https://other-network.example/query"
    (env.home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sai": claude_entry(serve(env), {"SAI_ENDPOINT": endpoint})
                }
            }
        )
    )
    (env.codex_home / "fake-state.json").write_text(
        json.dumps(
            {
                "sai": {
                    "type": "stdio",
                    "command": str(env.sai),
                    "args": ["mcp", "serve"],
                    "env": {},
                    "env_vars": ["SAI_LCD_ENDPOINT"],
                }
            }
        )
    )
    listed = invoke("mcp", "list")
    rows = {r["client"]: r for r in payload(listed)["clients"]}
    assert rows["claude"]["current"] is False
    assert rows["claude"]["staleEnv"] == ["SAI_ENDPOINT"]
    assert rows["codex"]["staleEnv"] == ["SAI_LCD_ENDPOINT"]
    assert endpoint not in listed.stdout + listed.stderr
    result = invoke("mcp", "install", "claude", "codex")
    assert actions(result) == {"claude": "replaced", "codex": "replaced"}
    assert endpoint not in result.stdout + result.stderr
    assert env.claude_json()["mcpServers"]["sai"]["env"] == {}
    again = invoke("mcp", "install", "claude", "codex")
    assert set(actions(again).values()) == {"unchanged"}


def test_disabled_codex_entry_is_not_current_and_is_replaced(env: Env):
    (env.codex_home / "fake-state.json").write_text(
        json.dumps(
            {
                "sai": {
                    "type": "stdio",
                    "command": str(env.sai),
                    "args": ["mcp", "serve"],
                },
                "sai_enabled": False,
            }
        )
    )
    row = payload(invoke("mcp", "list"))["clients"][1]
    assert (row["client"], row["current"], row["disabled"]) == ("codex", False, True)
    result = invoke("mcp", "install", "codex")
    assert actions(result) == {"codex": "replaced"}
    assert ["mcp", "remove", "sai"] in env.calls()
    row = payload(invoke("mcp", "list"))["clients"][1]
    assert row["current"] is True
    assert "disabled" not in row


def test_list_shows_every_scope_and_the_effective_one(env: Env):
    old = claude_entry([OLD_COMMAND])
    (env.home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"sai": claude_entry(serve(env))}})
    )
    (env.project / ".mcp.json").write_text(json.dumps({"mcpServers": {"sai": old}}))
    row = payload(invoke("mcp", "list"))["clients"][0]
    assert row["client"] == "claude"
    assert (row["scope"], row["current"]) == ("project", False)
    assert row["registrations"] == [
        {
            "scope": "project",
            "command": [OLD_COMMAND],
            "current": False,
            "effective": True,
        },
        {"scope": "user", "command": serve(env), "current": True, "effective": False},
    ]
    result = invoke("mcp", "install", "claude")
    only = payload(result)["results"][0]
    assert (only["action"], only["scope"], only["shadowedBy"]) == (
        "unchanged",
        "user",
        "project",
    )


def test_uninstall_removes_every_scope_sai_is_in(env: Env):
    (env.home / ".claude.json").write_text(
        json.dumps(
            {
                "projects": {
                    str(env.project): {"mcpServers": {"sai": claude_entry(serve(env))}}
                }
            }
        )
    )
    (env.project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sai": claude_entry(serve(env))}})
    )
    cursor_project = env.project / ".cursor" / "mcp.json"
    cursor_project.parent.mkdir()
    cursor_project.write_text(json.dumps({"mcpServers": {"sai": {"command": "x"}}}))
    result = invoke("mcp", "uninstall", "claude", "cursor")
    assert result.exit_code == 0, result.output
    got = [(r["client"], r["scope"], r["action"]) for r in payload(result)["results"]]
    assert got == [
        ("claude", "local", "removed"),
        ("claude", "project", "removed"),
        ("cursor", "project", "removed"),
    ]
    assert env.calls() == [
        ["mcp", "remove", "--scope", "local", "sai"],
        ["mcp", "remove", "--scope", "project", "sai"],
    ]
    assert json.loads(cursor_project.read_text())["mcpServers"] == {}
    rows = payload(invoke("mcp", "list"))["clients"]
    assert [r["registered"] for r in rows] == [False, False, False]


def test_uninstall_scope_limits_the_removal(env: Env):
    (env.project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sai": claude_entry(serve(env))}})
    )
    result = invoke("mcp", "uninstall", "claude", "--scope", "user")
    assert [(r["scope"], r["action"]) for r in payload(result)["results"]] == [
        ("user", "absent")
    ]
    assert env.calls() == []


def test_cursor_merges_again_after_a_concurrent_change(env: Env, monkeypatch):
    path = env.cursor_path()
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {"first": {"command": "a"}}}))
    writes = []

    def concurrent(target: Path) -> None:
        if not writes:
            writes.append(1)
            target.write_text(
                json.dumps({"mcpServers": {"first": {"command": "a"}, "late": {}}})
            )

    monkeypatch.setattr(mcp_install, "before_replace", concurrent)
    assert actions(invoke("mcp", "install", "cursor")) == {"cursor": "added"}
    assert set(json.loads(path.read_text())["mcpServers"]) == {"first", "late", "sai"}


def test_cursor_gives_up_when_the_file_keeps_changing(env: Env, monkeypatch):
    path = env.cursor_path()
    path.parent.mkdir()
    path.write_text("{}")
    count = iter(range(100))

    def concurrent(target: Path) -> None:
        target.write_text(json.dumps({"mcpServers": {f"s{next(count)}": {}}}))

    monkeypatch.setattr(mcp_install, "before_replace", concurrent)
    result = invoke("mcp", "install", "cursor")
    assert "kept changing" in failure(result)
    assert "sai" not in json.loads(path.read_text())["mcpServers"]
    assert [p.name for p in path.parent.iterdir()] == ["mcp.json"]


def test_registered_args_are_hidden_unless_they_are_the_expected_command(env: Env):
    wrapper = ["/opt/bin/wrap", "--url", f"https://user:{SECRET}@host.example/mcp"]
    (env.home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"sai": claude_entry(wrapper)}})
    )
    (env.codex_home / "fake-state.json").write_text(
        json.dumps(
            {
                "sai": {
                    "type": "stdio",
                    "command": str(env.sai),
                    "args": ["mcp", "serve"],
                }
            }
        )
    )
    listed = invoke("mcp", "list")
    rows = {r["client"]: r for r in payload(listed)["clients"]}
    assert rows["claude"]["registeredCommand"] == ["wrap"]
    assert rows["claude"]["args"] == "<hidden>"
    assert rows["claude"]["registrations"][0]["args"] == "<hidden>"
    assert rows["codex"]["registeredCommand"] == serve(env)
    assert "args" not in rows["codex"]
    outputs = [
        listed,
        invoke("mcp", "uninstall"),
        invoke("mcp", "uninstall", "claude", "--dry-run"),
        invoke("mcp", "uninstall", "claude"),
    ]
    removed = payload(outputs[-1])["results"][0]
    assert (removed["action"], removed["command"], removed["args"]) == (
        "removed",
        ["wrap"],
        "<hidden>",
    )
    for result in outputs:
        assert SECRET not in result.stdout + result.stderr
        assert "host.example" not in result.stdout + result.stderr


def codex_transport(argv: list[str], env: dict[str, str] | None = None) -> dict:
    """The transport shape real Codex and the fake both report."""
    return {
        "type": "stdio",
        "command": argv[0],
        "args": argv[1:],
        "env": env or {},
        "env_vars": [],
        "cwd": None,
    }


def codex_project(
    env: Env, argv: list[str], extra: dict[str, str] | None = None
) -> None:
    (env.project / "fake-codex-project.json").write_text(
        json.dumps(codex_transport(argv, extra))
    )


def codex_user(env: Env, argv: list[str], extra: dict[str, str] | None = None) -> None:
    (env.codex_home / "fake-state.json").write_text(
        json.dumps({"sai": codex_transport(argv, extra)})
    )


def test_codex_project_override_is_effective_and_not_edited(env: Env):
    codex_project(env, ["old-wrapper", "--token", SECRET])
    listed = invoke("mcp", "list")
    row = payload(listed)["clients"][1]
    assert row["client"] == "codex"
    assert (row["scope"], row["current"]) == ("project", False)
    assert row["registrations"] == [
        {
            "scope": "project",
            "command": ["old-wrapper"],
            "args": "<hidden>",
            "current": False,
            "effective": True,
        }
    ]
    install = invoke("mcp", "install", "codex")
    assert ".codex/config.toml" in failure(install)
    uninstall = invoke("mcp", "uninstall", "codex")
    assert "codex (project): failed" in failure(uninstall)
    # Removing only the user entry still reports the override.
    assert invoke("mcp", "uninstall", "codex", "--scope", "user").exit_code == 1
    assert [c for c in env.calls() if c[1] != "get"] == []
    for result in (listed, install, uninstall):
        assert SECRET not in result.stdout + result.stderr


def test_codex_project_answer_equal_to_user_is_no_override(env: Env):
    assert invoke("mcp", "install", "codex").exit_code == 0
    codex_project(env, serve(env))
    row = payload(invoke("mcp", "list"))["clients"][1]
    assert [r["scope"] for r in row["registrations"]] == ["user"]
    assert row["current"] is True
    # Both gets ran: one from CODEX_HOME, one from the project directory.
    gets = [c for c in env.calls() if c[1] == "get"]
    assert len(gets) >= 2


def test_shadowed_by_names_only_a_higher_precedence_scope(env: Env):
    (env.home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"sai": claude_entry([OLD_COMMAND])}})
    )
    result = invoke("mcp", "install", "claude", "--scope", "project")
    only = payload(result)["results"][0]
    assert only["action"] == "added"
    assert "shadowedBy" not in only


def test_codex_override_differing_only_in_env_values_is_found(env: Env):
    """Same command and env keys, different SAI_ENDPOINT values."""
    stale = "https://stale-endpoint.example/query"
    codex_user(env, serve(env), {"SAI_ENDPOINT": "https://user.example/query"})
    codex_project(env, serve(env), {"SAI_ENDPOINT": stale})
    listed = invoke("mcp", "list")
    row = payload(listed)["clients"][1]
    assert [r["scope"] for r in row["registrations"]] == ["project", "user"]
    install = invoke("mcp", "install", "codex")
    assert "codex (user): failed" in failure(install)
    assert [c for c in env.calls() if c[1] != "get"] == []
    for result in (listed, install):
        assert "example" not in result.stdout + result.stderr


def test_codex_identical_project_entry_fails_the_post_check(env: Env):
    """User and project entries are equal, so they read as one entry; only
    asking Codex again after the change shows the project entry remains."""
    old = ["/opt/old/sai", "mcp", "serve"]
    codex_user(env, old)
    codex_project(env, old)
    uninstall = invoke("mcp", "uninstall", "codex")
    message = failure(uninstall)
    assert (
        "codex (user): failed: a project .codex/config.toml entry is still active"
        in message
    )
    # The user entry was removed, so clients elsewhere need to reconnect.
    assert "Client registrations changed." in message
    codex_user(env, old)
    install = invoke("mcp", "install", "codex")
    assert "failed: a project .codex/config.toml entry is still active" in failure(
        install
    )


def test_claude_post_check_catches_an_add_that_did_not_land(env: Env):
    write_exe(env.bin / "claude", "import sys\nsys.exit(0)\n")
    result = invoke("mcp", "install", "claude")
    assert "claude does not show the new sai entry" in failure(result)


def test_codex_project_tool_filter_is_an_override(env: Env):
    """Same transport, but the project limits which Sai tools Codex offers."""
    codex_user(env, serve(env))
    (env.project / "fake-codex-project.json").write_text(
        json.dumps(
            {"transport": codex_transport(serve(env)), "enabled_tools": ["market_list"]}
        )
    )
    row = payload(invoke("mcp", "list"))["clients"][1]
    assert row["registrations"][0]["scope"] == "project"
    assert row["registrations"][0]["extraSettings"] == ["enabled_tools"]
    assert row["current"] is False
    result = invoke("mcp", "install", "codex")
    message = failure(result)
    assert "codex (user): failed" in message
    assert "Client registrations changed." not in message


def test_reconnect_is_needed_when_a_remove_ran_before_a_failed_add(env: Env):
    (env.home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"sai": claude_entry([OLD_COMMAND])}})
    )
    fails_add = FAKE_CLAUDE.replace(
        "args = sys.argv[1:]\n",
        "args = sys.argv[1:]\nif args[1] == 'add':\n    sys.exit(3)\n",
    )
    write_exe(env.bin / "claude", fails_add)
    result = invoke("mcp", "install", "claude")
    message = failure(result)
    assert "claude (user): failed" in message
    assert "Client registrations changed." in message
    details = payload(result)["error"]["details"]
    assert details["reconnectNeeded"] is True
    [row] = details["results"]
    assert row["action"] == "failed" and row["changed"] is True
