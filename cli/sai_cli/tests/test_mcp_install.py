"""`sai mcp install|list|uninstall` against fake Claude Code and Codex
programs and a temporary home; the real home directory is never touched."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import tomlkit
from click.testing import CliRunner
from sai_cli import mcp_install
from sai_cli import sai_cli as cli
from sai_cli.core import log, updater
from sai_cli.sai_cli import main
from test_update import GIT, make_prefix

# Built from parts so the retired-names test does not match this file.
OLD_COMMAND = "sai" + "-mcp"
SECRET = "sk-SECRET-do-not-print"
OTHER = "otherserver-private-name"

# Both fakes record argv to $FAKE_LOG and keep state where the real client
# would, so a second run sees the first run's change.
FAKE_CLAUDE = """
import json, os, shutil, subprocess, sys
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
    git = shutil.which("git")
    root = Path.cwd()
    if git is not None:
        found = subprocess.run([git, "rev-parse", "--show-toplevel"], capture_output=True)
        if found.returncode == 0:
            root = Path(os.fsdecode(found.stdout.strip()))
    holder = data.setdefault("projects", {}).setdefault(str(root), {})
servers = holder.setdefault("mcpServers", {})
name = args[args.index("--scope") + 2]
if args[1] == "remove":
    if name not in servers:
        sys.stderr.write(f'No MCP server named "{name}" in {scope} scope\\n')
        sys.exit(1)
    del servers[name]
else:
    dash = args.index("--")
    env = dict(
        a.split("=", 2)[1:] for a in args[:dash] if a.startswith("--env=")
    )
    command = args[dash + 1 :]
    servers[name] = {
        "type": "stdio", "command": command[0], "args": command[1:], "env": env
    }
path.write_text(json.dumps(data))
"""

FAKE_CODEX = """
import json, os, sys
from pathlib import Path
import tomlkit
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(sys.argv) + "\\n")
args = sys.argv[1:]
assert args[:2] == ["mcp", "get"], args
name = args[2]
config = Path(os.environ["CODEX_HOME"]) / "config.toml"
state = tomlkit.parse(config.read_text()).unwrap() if config.exists() else {}
# Stands in for Codex's own project rules: a project entry Codex would load
# from this directory is given in ./fake-codex-project.json.
project = Path.cwd() / "fake-codex-project.json"
if name == "sai" and project.exists():
    data = json.loads(project.read_text())
    if "transport" not in data:
        data = {"transport": data}
else:
    entry = state.get("mcp_servers", {}).get(name)
    if entry is None:
        sys.stderr.write(f"Error: No MCP server named '{name}' found.\\n")
        sys.exit(1)
    transport = dict(entry)
    data = {"enabled": transport.pop("enabled", True), "transport": transport}
    for key in ("enabled_tools", "disabled_tools"):
        if key in transport:
            data[key] = transport.pop(key)
data["transport"] = {
    "type": "stdio", "env": {}, "env_vars": [], "cwd": None, **data["transport"]
}
print(json.dumps({"name": name, "enabled": True, **data}))
"""


# Codex is asked twice: from CODEX_HOME for the user entry, then from the
# working directory for the entry it uses here.
GETS = [["mcp", "get", "sai", "--json"]] * 2
LEGACY_GETS = [["mcp", "get", OLD_COMMAND, "--json"]] * 2
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

    def codex_path(self) -> Path:
        return self.codex_home / "config.toml"

    def write_codex(self, entries: dict[str, Any]) -> None:
        self.codex_path().write_text(tomlkit.dumps({"mcp_servers": entries}))


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


def write_doctor_entries(env: Env, name: str, entry: dict[str, Any]) -> dict[str, Any]:
    unrelated = {"url": "https://private.example/mcp", "headers": {"X-Token": SECRET}}
    entries = {name: entry, OTHER: unrelated}
    document = json.dumps({"mcpServers": entries})
    (env.home / ".claude.json").write_text(document)
    env.cursor_path().parent.mkdir()
    env.cursor_path().write_text(document)
    (env.codex_home / "config.toml").write_text(tomlkit.dumps({"mcp_servers": entries}))
    return unrelated


def doctor_entries(env: Env) -> dict[str, dict[str, Any]]:
    return {
        "claude": env.claude_json()["mcpServers"],
        "codex": tomlkit.parse((env.codex_home / "config.toml").read_text()).unwrap()[
            "mcp_servers"
        ],
        "cursor": json.loads(env.cursor_path().read_text())["mcpServers"],
    }


def write_project_mcp_denials(project: Path) -> dict[Path, bytes]:
    files = {
        project / ".claude" / "settings.local.json": {
            "enableAllProjectMcpServers": True,
            "disabledMcpjsonServers": [OLD_COMMAND],
        },
        project / ".cursor" / "cli.json": {
            "permissions": {"deny": [f"Mcp({OLD_COMMAND}:*)"]},
        },
    }
    original = {path: json.dumps(data).encode() for path, data in files.items()}
    for path, content in original.items():
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
    return original


def mcp_checks(result: Any) -> dict[str, dict[str, Any]]:
    report = payload(result)
    if "error" in report:
        report = report["error"]["details"]
    return {
        check["id"].split(".")[1]: check
        for check in report["checks"]
        if check["id"].startswith("mcp.")
    }


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("stable", "pass"),
        ("other-absolute", "warn"),
        ("legacy-pinned", "warn"),
        ("missing", "fail"),
        ("uv-cache", "fail"),
        ("legacy-command", "fail"),
        ("legacy-name", "warn"),
        ("legacy-name-stale", "fail"),
    ],
)
def test_doctor_reports_claude_and_repairs_codex_and_cursor_registrations(
    env: Env, monkeypatch: pytest.MonkeyPatch, kind: str, expected: str
):
    monkeypatch.setenv("SAI_HOME", str(env.root / "sai-home"))
    command = str(env.sai)
    if kind in ("other-absolute", "legacy-pinned", "missing", "uv-cache"):
        root = env.home / (".cache/uv" if kind == "uv-cache" else kind)
        root.mkdir(parents=True)
        path = root / "sai"
        if kind != "missing":
            write_exe(path, "raise RuntimeError('doctor must not run the server')")
        command = str(path)
    suffix = ["--endpoint", "https://private.example/query?token=" + SECRET]
    args = ["mcp", "serve", *suffix]
    if kind in ("legacy-command", "legacy-name-stale"):
        command, args = OLD_COMMAND, suffix
    name = (
        OLD_COMMAND
        if kind in ("legacy-name", "legacy-name-stale", "legacy-pinned")
        else "sai"
    )
    entry = {
        "command": command,
        "args": args,
        "env": {"SAI_HOME": str(env.root / "sai-home"), "CUSTOM": SECRET},
        "enabled_tools": ["market_list"],
    }
    unrelated = write_doctor_entries(env, name, entry)
    files = [
        env.home / ".claude.json",
        env.cursor_path(),
        env.codex_home / "config.toml",
    ]
    before = [path.read_bytes() for path in files]
    report = invoke("doctor", "--offline", "--no-fix")
    assert report.exit_code == (1 if expected == "fail" else 0), report.output
    checks = mcp_checks(report)
    assert set(checks) == set(mcp_install.CLIENTS)
    claude_status = "warn" if kind == "stable" else expected
    assert checks["claude"]["status"] == claude_status
    assert checks["codex"]["status"] == checks["cursor"]["status"] == expected
    assert [path.read_bytes() for path in files] == before
    assert {call[1] for call in env.calls()} == {"get"}

    result = invoke("doctor", "--offline")
    assert result.exit_code == (1 if expected == "fail" else 0), result.output
    status = (
        "warn" if name == OLD_COMMAND else ("fixed" if expected == "fail" else expected)
    )
    checks = mcp_checks(result)
    assert {name: check["status"] for name, check in checks.items()} == {
        "claude": claude_status,
        "codex": status,
        "cursor": status,
    }
    if expected != "fail":
        assert [path.read_bytes() for path in files] == before
    else:
        assert files[0].read_bytes() == before[0]
        assert not checks["claude"].get("changes")
        if name == OLD_COMMAND:
            assert f"--scope user --repair-existing {name}" in checks["claude"]["fix"]
        else:
            assert f"claude mcp remove --scope user {name}" in checks["claude"]["fix"]
            assert f"claude mcp add --scope user {name}" in checks["claude"]["fix"]
        entries = doctor_entries(env)
        for registrations in (entries["codex"], entries["cursor"]):
            assert registrations == {
                name: {
                    **entry,
                    "command": str(env.sai),
                    "args": ["mcp", "serve", *suffix],
                },
                OTHER: unrelated,
            }
        assert checks["codex"]["changes"] and checks["cursor"]["changes"]
        again = invoke("doctor", "--offline")
        current = "warn" if name == OLD_COMMAND else "pass"
        assert {name: check["status"] for name, check in mcp_checks(again).items()} == {
            "claude": "fail",
            "codex": current,
            "cursor": current,
        }
    assert log.flush()
    output = report.output + result.output
    output += "".join(path.read_text() for path in log.directory().glob("*.jsonl"))
    for private_value in (SECRET, OTHER, "private.example"):
        assert private_value not in output


def test_doctor_preserves_denied_registration_names(env: Env):
    policies = write_project_mcp_denials(env.project)
    entry = {"command": OLD_COMMAND, "args": [], "env": {"CUSTOM": SECRET}}
    denied_codex = {**entry, "enabled": False, "enabled_tools": []}
    claude = env.project / ".mcp.json"
    cursor = env.project / ".cursor" / "mcp.json"
    env.cursor_path().parent.mkdir()
    for path in (claude, cursor):
        path.write_text(json.dumps({"mcpServers": {OLD_COMMAND: entry}}))
    original_claude = claude.read_bytes()
    env.write_codex({OLD_COMMAND: denied_codex})

    result = invoke("doctor", "--offline")

    assert result.exit_code == 1, result.output
    checks = mcp_checks(result)
    assert set(checks) == set(mcp_install.CLIENTS)
    for name, check in checks.items():
        if name == "claude":
            assert check["status"] == "fail"
            assert not check.get("changes")
            assert f"--scope project --repair-existing {OLD_COMMAND}" in check["fix"]
        else:
            assert check["status"] == "warn"
            assert check["changes"]
        assert OLD_COMMAND in check["summary"]
    assert claude.read_bytes() == original_claude
    expected = {**entry, "command": str(env.sai), "args": ["mcp", "serve"]}
    assert json.loads(cursor.read_text())["mcpServers"] == {OLD_COMMAND: expected}
    assert tomlkit.parse(env.codex_path().read_text()).unwrap()["mcp_servers"] == {
        OLD_COMMAND: {**expected, "enabled": False, "enabled_tools": []},
    }
    assert all(path.read_bytes() == content for path, content in policies.items())
    assert SECRET not in result.output


@pytest.mark.parametrize("canonical_exists", [False, True])
def test_mcp_list_and_uninstall_all_include_repaired_legacy_entries(
    env: Env, canonical_exists: bool
):
    entry = {"command": OLD_COMMAND, "args": [], "env": {"CUSTOM": SECRET}}
    unrelated = write_doctor_entries(env, OLD_COMMAND, entry)
    repaired = invoke(
        "mcp", "install", "--client", "claude", "--repair-existing", OLD_COMMAND
    )
    assert repaired.exit_code == 0, repaired.output
    assert invoke("doctor", "--offline").exit_code == 0
    if canonical_exists:
        entries = doctor_entries(env)
        canonical = claude_entry(serve(env))
        for client in entries:
            entries[client]["sai"] = canonical
        env.write_codex(entries["codex"])
        for path, client in (
            (env.home / ".claude.json", "claude"),
            (env.cursor_path(), "cursor"),
        ):
            path.write_text(json.dumps({"mcpServers": entries[client]}))

    listed = invoke("mcp", "list")
    for row in payload(listed)["clients"]:
        assert row["registered"]
        assert row.get("registeredName", "sai") == (
            "sai" if canonical_exists else OLD_COMMAND
        )
        assert any(reg.get("name") == OLD_COMMAND for reg in row["registrations"])

    planned = invoke("mcp", "uninstall", "--all", "--dry-run")
    names = {"sai", OLD_COMMAND} if canonical_exists else {OLD_COMMAND}
    plan = payload(planned)["results"]
    assert {(row["client"], row.get("name", "sai")) for row in plan} == {
        (client, name) for client in mcp_install.CLIENTS for name in names
    }
    assert {(row["action"], row["would"]) for row in plan} == {("planned", "removed")}
    assert all(OLD_COMMAND in entries for entries in doctor_entries(env).values())

    removed = invoke("mcp", "uninstall", "--all")
    assert removed.exit_code == 0, removed.output
    assert actions(removed) == dict.fromkeys(mcp_install.CLIENTS, "removed")
    assert {row.get("name", "sai") for row in payload(removed)["results"]} == names
    assert all(
        entries == {OTHER: unrelated} for entries in doctor_entries(env).values()
    )
    for result in (listed, planned, removed):
        assert SECRET not in result.output and OTHER not in result.output


def test_doctor_reports_exact_claude_repairs_for_each_scope(env: Env, monkeypatch):
    home = str(env.root / "sai-home")
    monkeypatch.setenv("SAI_HOME", home)
    suffix = ["--endpoint", "https://private.example/" + SECRET]
    old = claude_entry([OLD_COMMAND, *suffix], {"SAI_HOME": home, "CUSTOM": SECRET})
    scopes = {"mcpServers": {"sai": old, OTHER: {"command": "untouched"}}}
    user_file = env.home / ".claude.json"
    user_file.write_text(
        json.dumps(
            {
                **scopes,
                "projects": {
                    str(env.project): {
                        "mcpServers": {
                            OLD_COMMAND: old,
                            "sai": claude_entry(serve(env)),
                        }
                    }
                },
                "preference": True,
            }
        )
    )
    project_file = env.project / ".mcp.json"
    project_file.write_text(json.dumps(scopes))
    before = [user_file.read_bytes(), project_file.read_bytes()]

    result = invoke("doctor", "--offline")

    check = mcp_checks(result)["claude"]
    assert result.exit_code == 1, result.output
    assert check["status"] == "fail" and not check.get("changes")
    assert [user_file.read_bytes(), project_file.read_bytes()] == before
    commands = check["fix"].split("`")[1::2]
    for scope in ("user", "project", "local"):
        assert any(
            f"claude mcp remove --scope {scope} sai" in command for command in commands
        )
        add = (
            f"claude mcp add --scope {scope} sai --env=SAI_HOME={home}"
            f" -- {shlex.quote(str(env.sai))} mcp serve"
        )
        assert any(add in command for command in commands)
    assert (
        f"sai mcp install --client claude --scope local --repair-existing {OLD_COMMAND}"
        in commands
    )
    assert {call[1] for call in env.calls()} == {"get"}
    for private_value in (SECRET, OTHER, "private.example"):
        assert private_value not in result.output


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal delivery")
def test_claude_repair_stages_a_complete_entry_before_atomic_publication(
    env: Env, monkeypatch
):
    marker = env.root / "staged-config.json"
    monkeypatch.setenv("SAI_HOME", str(env.root / "current-profile"))
    pinned_home = str(env.root / "private-pinned-profile=v1")
    suffix = ["--endpoint", "https://private.example/" + SECRET]
    entry = claude_entry(
        [OLD_COMMAND, *suffix], {"SAI_HOME": pinned_home, "CUSTOM": SECRET}
    )
    original = {"mcpServers": {OLD_COMMAND: entry, OTHER: {"command": "untouched"}}}
    config = env.home / ".claude.json"
    config.write_text(json.dumps(original))
    # Observe the actual os.replace syscall boundary without modifying Sai.
    code = """
import signal
import sys
from pathlib import Path

from sai_cli.sai_cli import main

config, marker = map(Path, sys.argv[1:3])

def interrupt_write(event, args):
    if event == "os.rename" and Path(args[1]) == config:
        marker.write_bytes(Path(args[0]).read_bytes())
        signal.raise_signal(signal.SIGINT)

sys.addaudithook(interrupt_write)
main.main(
    ["mcp", "install", "--client", "claude", "--repair-existing", sys.argv[3]],
    prog_name="sai",
)
"""

    result = subprocess.run(
        [sys.executable, "-c", code, str(config), str(marker), OLD_COMMAND],
        check=False,
        capture_output=True,
        text=True,
        cwd=env.project,
        timeout=15,
    )

    assert result.returncode != 0
    assert json.loads(marker.read_text()) == {
        "mcpServers": {
            OLD_COMMAND: {
                **entry,
                "command": str(env.sai),
                "args": ["mcp", "serve", *suffix],
            },
            OTHER: {"command": "untouched"},
        }
    }
    assert env.claude_json() == original
    assert not list(config.parent.glob(".claude.json.*"))
    output = result.stdout + result.stderr
    assert pinned_home not in output and SECRET not in output and OTHER not in output


@pytest.mark.parametrize(
    ("expected", "actual", "status"),
    [
        (None, None, "pass"),
        ("current", "current", "pass"),
        ("current", None, "warn"),
        (None, "different", "warn"),
        ("current", "different", "warn"),
        (None, "", "warn"),
        (None, 17, "warn"),
    ],
)
def test_doctor_compares_sai_home_without_printing_or_rewriting_it(
    env: Env,
    monkeypatch,
    expected,
    actual,
    status,
):
    profiles = {
        "current": str(env.root / "current-profile"),
        "different": str(env.root / "private-pinned-profile"),
    }
    if expected is not None:
        monkeypatch.setenv("SAI_HOME", profiles[expected])
    variables = {} if actual is None else {"SAI_HOME": profiles.get(actual, actual)}
    path = env.home / ".claude.json"
    original = json.dumps({"mcpServers": {"sai": claude_entry(serve(env), variables)}})
    path.write_text(original)

    result = invoke("doctor", "--offline")

    check = mcp_checks(result)["claude"]
    assert check["status"] == status
    assert ("SAI_HOME" in check["summary"]) == (status == "warn")
    assert profiles["different"] not in result.output
    assert profiles["current"] not in check["summary"]
    if expected == "current" and status == "warn":
        assert f"--env=SAI_HOME={profiles['current']}" in check["fix"]
    assert path.read_text() == original


def test_doctor_warns_for_absent_entries_and_leaves_custom_entries_alone(env: Env):
    env.cursor_path().parent.mkdir()
    env.cursor_path().write_text(
        json.dumps({"mcpServers": {"sai": {"command": "missing-custom-wrapper"}}})
    )
    result = invoke("doctor", "--offline")
    assert result.exit_code == 1
    assert {name: check["status"] for name, check in mcp_checks(result).items()} == {
        "claude": "warn",
        "codex": "warn",
        "cursor": "fail",
    }
    assert {call[1] for call in env.calls()} == {"get"}
    assert "missing-custom-wrapper" in env.cursor_path().read_text()


@pytest.mark.parametrize(
    ("args", "status"),
    [
        (["--help", "mcp", "serve"], "fail"),
        (["--version", "mcp", "serve"], "fail"),
        (["--skill", "mcp", "serve"], "fail"),
        (["mcp", "serve", "--help"], "fail"),
        (["mcp", "serve", "extra"], "fail"),
        (["mcp", "serve", 3], "fail"),
        (["--endpoint", "mcp", "serve"], "fail"),
        (["mcp", "serve", "--endpoint"], "fail"),
        (["mcp", "serve", "--endpoint=invalid-value"], "fail"),
        (["--endpoint=https://example.invalid", "mcp", "serve"], "pass"),
        (["mcp", "serve", "--endpoint", "https://example.invalid"], "pass"),
        (["--json", "mcp", "serve"], "pass"),
        (["--json", "--endpoint", "https://example.invalid", "mcp", "serve"], "pass"),
        (["--as", "another-account", "--json", "mcp", "serve"], "pass"),
        (["--json", "--version", "mcp", "serve"], "fail"),
        (["--json", "--help", "mcp", "serve"], "fail"),
        (["--json", "--", "mcp", "--", "serve", "--"], "pass"),
        (["mcp", "serve", "--json"], "fail"),
        (["--json=true", "mcp", "serve"], "fail"),
    ],
)
def test_doctor_validates_the_full_server_command(
    env: Env, args: list[Any], status: str
):
    env.cursor_path().parent.mkdir()
    original = json.dumps(
        {"mcpServers": {"sai": {"command": str(env.sai), "args": args}}}
    )
    env.cursor_path().write_text(original)

    result = invoke("doctor", "--offline")

    assert mcp_checks(result)["cursor"]["status"] == status
    assert env.cursor_path().read_text() == original
    assert "example.invalid" not in result.output


@pytest.mark.parametrize("action", ["doctor", "install", "uninstall"])
def test_codex_edits_only_sais_table(env: Env, action: str):
    path = env.codex_path()
    prefix = (
        "# Keep the header and line endings\r\n"
        "model  =   'a-model'\r\n\r\n"
        f'[ mcp_servers . "{OTHER}" ]  # managed\r\n'
        f"command  = '{env.sai}'\r\n"
        "enabled_tools    =    [] # Empty blocks every tool\r\n"
        "args = [ 'private-arg' ,]  \r\n\r\n"
    )
    suffix = "\r\n# Work profile follows\r\n[profiles.work]  # trailing section\r\nmodel = 'b-model'   \r\n"
    old = (
        f'[mcp_servers.sai]\r\ncommand = "{OLD_COMMAND}"\r\nargs = []\r\n'
        f'[mcp_servers.sai.env]\r\nPRIVATE = "{SECRET}"\r\n'
    )
    path.write_bytes((prefix + old + suffix).encode())
    path.chmod(0o640)

    args = ("doctor", "--offline") if action == "doctor" else ("mcp", action, "codex")
    result = invoke(*args)

    assert result.exit_code == 0, result.output
    assert {call[1] for call in env.calls()} == {"get"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    content = path.read_bytes()
    assert content.startswith(prefix.encode()) and content.endswith(suffix.encode())
    entries = tomlkit.parse(content.decode())["mcp_servers"]
    assert entries[OTHER]["enabled_tools"] == []
    if action == "uninstall":
        assert content == (prefix + suffix).encode()
    else:
        assert entries["sai"]["command"] == str(env.sai)
        assert entries["sai"]["args"] == ["mcp", "serve"]
        if action == "doctor":
            assert entries["sai"]["env"] == {"PRIVATE": SECRET}
        else:
            assert "env" not in entries["sai"]
    assert OTHER not in result.output
    assert "private-arg" not in result.output


@pytest.mark.parametrize("with_sai", [False, True])
def test_codex_install_leaves_dotted_root_keys_in_place(
    env: Env, monkeypatch, with_sai: bool
):
    monkeypatch.setenv("SAI_HOME", str(env.root / "sai-home"))
    source = (
        f'mcp_servers."{OTHER}".command = "outer"\n'
        + ('mcp_servers.sai.command = "old"\n' if with_sai else "")
        + f'mcp_servers."{OTHER}".enabled_tools  =   [] # empty\n'
        + "other_root  = 'keep this'\n"
    )
    env.codex_path().write_text(source)

    result = invoke("mcp", "install", "codex")

    assert result.exit_code == 0, result.output
    updated = env.codex_path().read_text()
    doc = tomlkit.parse(updated).unwrap()
    assert doc["other_root"] == "keep this"
    assert doc["mcp_servers"][OTHER] == {"command": "outer", "enabled_tools": []}
    assert doc["mcp_servers"]["sai"]["env"] == {"SAI_HOME": str(env.root / "sai-home")}
    if with_sai:

        def outside_sai(text: str) -> str:
            return "".join(
                line
                for line in text.splitlines(keepends=True)
                if not line.startswith("mcp_servers.sai.")
            )

        assert outside_sai(updated) == outside_sai(source)
    else:
        assert updated.startswith(source)


def test_codex_install_keeps_an_inline_servers_table(env: Env):
    entry = f'{OTHER} = {{command = "outer", enabled_tools  =  []}}'
    source = f"mcp_servers = {{ {entry} }} # keep this\n"
    env.codex_path().write_text(source)

    result = invoke("mcp", "install", "codex")

    assert result.exit_code == 0, result.output
    updated = env.codex_path().read_text()
    assert entry in updated and "# keep this" in updated
    assert tomlkit.parse(updated)["mcp_servers"]["sai"]["command"] == str(env.sai)
    assert invoke("mcp", "uninstall", "codex").exit_code == 0
    assert env.codex_path().read_text() == source


@pytest.mark.parametrize(
    "layout", ["inline-child", "outer-inline", "dotted-child", "root-dotted"]
)
def test_doctor_repairs_codex_legacy_inline_and_dotted_registrations(
    env: Env, layout: str
):
    other = f'"{OTHER}"  = {{command = "outer", enabled_tools = []}}'
    old = f'"{OLD_COMMAND}"'
    entry = f'{{command = "{OLD_COMMAND}", env = {{CUSTOM = "{SECRET}"}}, enabled_tools = []}}'
    if layout == "inline-child":
        source = f"[mcp_servers]\n{other} # keep this\n{old} = {entry}\n"
        untouched = f"{other} # keep this\n"
    elif layout == "outer-inline":
        source = f"mcp_servers = {{ {other}, {old} = {entry} }} # keep this\n"
        untouched = other
    else:
        prefix = "mcp_servers." if layout == "root-dotted" else ""
        header = "" if prefix else "[mcp_servers]\n"
        unrelated = f'{prefix}"{OTHER}".enabled_tools  =  [] # keep this\n'
        source = (
            f'{header}{prefix}"{OTHER}".command = "outer"\n'
            f'{prefix}{old}.command = "{OLD_COMMAND}"\n'
            f"{unrelated}"
            f'{prefix}{old}.env.CUSTOM = "{SECRET}"\n'
            f"{prefix}{old}.enabled_tools = []\n"
        )
        untouched = unrelated
    env.codex_path().write_text(source)

    result = invoke("doctor", "--offline")

    assert result.exit_code == 0, result.output
    check = mcp_checks(result)["codex"]
    assert check["status"] == "warn" and check["changes"]
    assert OLD_COMMAND in check["summary"]
    updated = env.codex_path().read_text()
    assert untouched in updated
    parsed = tomlkit.parse(updated).unwrap()["mcp_servers"]
    assert parsed == {
        OTHER: {"command": "outer", "enabled_tools": []},
        OLD_COMMAND: {
            "command": str(env.sai),
            "args": ["mcp", "serve"],
            "env": {"CUSTOM": SECRET},
            "enabled_tools": [],
        },
    }
    assert SECRET not in result.output and OTHER not in result.output


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks and modes")
@pytest.mark.parametrize("client", ["codex", "cursor"])
def test_doctor_updates_symlink_referent_and_keeps_link(env: Env, client: str):
    entry = {"command": OLD_COMMAND, "args": []}
    if client == "codex":
        config = env.codex_path()
        original = tomlkit.dumps({"mcp_servers": {"sai": entry}})
    else:
        config = env.cursor_path()
        original = json.dumps({"mcpServers": {"sai": entry}})
    config.parent.mkdir(exist_ok=True)
    referent = env.root / "managed-client-config"
    referent.write_text(original)
    referent.chmod(0o640)
    config.symlink_to(referent)

    result = invoke("doctor", "--offline")

    assert mcp_checks(result)[client]["status"] == "fixed", result.output
    assert config.is_symlink() and config.readlink() == referent
    assert str(env.sai) in referent.read_text()
    assert stat.S_IMODE(referent.stat().st_mode) == 0o640


def test_doctor_reports_same_stale_codex_project_entry_after_repairing_user(env: Env):
    entry = {"command": OLD_COMMAND, "args": []}
    override = env.project / "fake-codex-project.json"
    override.write_text(json.dumps(entry))
    (env.codex_home / "config.toml").write_text(
        tomlkit.dumps({"mcp_servers": {"sai": entry}})
    )

    result = invoke("doctor", "--offline")

    check = mcp_checks(result)["codex"]
    assert result.exit_code == 1
    assert check["status"] == "fail"
    assert "project registration is still active" in check["summary"]
    assert check["changes"]
    assert json.loads(override.read_text()) == entry
    config = tomlkit.parse((env.codex_home / "config.toml").read_text()).unwrap()
    assert config["mcp_servers"]["sai"]["command"] == str(env.sai)


@pytest.mark.skipif(os.name == "nt", reason="POSIX uv launcher symlink")
def test_doctor_repairs_codex_with_a_normalized_stable_launcher(
    env: Env, monkeypatch: pytest.MonkeyPatch
):
    prefix = make_prefix(env.root / "uv-tool", GIT, receipt=True)
    executable = prefix / "sai"
    env.sai.rename(executable)
    env.sai.symlink_to(executable)
    intermediate = env.root / "share"
    intermediate.mkdir()
    receipt_launcher = intermediate / ".." / "bin" / "sai"
    with (prefix / "uv-receipt.toml").open("a") as receipt:
        receipt.write(
            f'entrypoints = [{{ name = "sai", install-path = {json.dumps(str(receipt_launcher))} }}]\n'
        )
    monkeypatch.setattr(sys, "prefix", str(prefix))
    env.write_codex({"sai": claude_entry([OLD_COMMAND])})

    repaired = invoke("doctor", "--offline")

    assert mcp_checks(repaired)["codex"]["status"] == "fixed", repaired.output
    entries = tomlkit.parse(env.codex_path().read_text()).unwrap()["mcp_servers"]
    assert entries["sai"]["command"] == str(env.sai)
    for launcher in (env.sai, receipt_launcher):
        if launcher == receipt_launcher:
            env.write_codex({"sai": claude_entry([str(launcher), "mcp", "serve"])})
        listed = invoke("mcp", "list")
        assert listed.exit_code == 0, listed.output
        codex = next(
            row for row in payload(listed)["clients"] if row["client"] == "codex"
        )
        assert codex["current"] and codex["registrations"][0]["current"]
        assert codex["registeredCommand"] == serve(env)
        assert mcp_checks(invoke("doctor", "--offline"))["codex"]["status"] == "pass"


def test_install_uses_each_clients_registration_backend(env: Env):
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
        *LEGACY_GETS,
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


def test_install_reports_old_claude_entry_and_still_replaces_codex(env: Env):
    config = env.home / ".claude.json"
    config.write_text(
        json.dumps({"mcpServers": {"sai": {"command": OLD_COMMAND, "args": []}}})
    )
    before = config.read_bytes()
    env.write_codex({"sai": {"type": "stdio", "command": OLD_COMMAND, "args": []}})
    result = invoke("mcp", "install", "claude", "codex")
    assert "differs from the requested settings" in failure(result)
    details = payload(result)["error"]["details"]
    assert [row["action"] for row in details["results"]] == ["failed", "replaced"]
    assert details["results"][1]["changed"]
    assert config.read_bytes() == before
    assert env.calls() == GETS + CHECK
    env.clear()
    explicit = invoke("mcp", "install", "claude", "--repair-existing", "sai")
    assert "differs from the requested settings" in failure(explicit)
    assert config.read_bytes() == before
    assert not env.calls()


@pytest.mark.parametrize("repair_first", [False, True])
def test_install_preserves_working_codex_and_cursor_aliases(
    env: Env, repair_first: bool
):
    routing = ["--endpoint", f"https://private.example/{SECRET}"]
    argv = [OLD_COMMAND, *routing] if repair_first else [*serve(env), *routing]
    write_doctor_entries(env, OLD_COMMAND, claude_entry(argv, {"SAI_HOME": SECRET}))
    if repair_first:
        report = invoke("doctor", "--offline")
        assert mcp_checks(report)["claude"]["status"] == "fail"
        entries = doctor_entries(env)
        for client in ("codex", "cursor"):
            assert entries[client][OLD_COMMAND]["command"] == str(env.sai)

    paths = (env.codex_path(), env.cursor_path())
    before = [path.read_bytes() for path in paths]
    env.clear()
    for flags in ((), ("--dry-run",)):
        result = invoke("mcp", "install", "codex", "cursor", *flags)
        assert result.exit_code == 0, result.output
        summary = payload(result)
        assert not summary["reconnectNeeded"]
        assert [
            (row["client"], row["name"], row["scope"], row["action"])
            for row in summary["results"]
        ] == [
            (client, OLD_COMMAND, "user", "unchanged") for client in ("codex", "cursor")
        ]
        assert SECRET not in result.output
        assert [path.read_bytes() for path in paths] == before
    assert env.calls() == (GETS + LEGACY_GETS) * 2


def test_user_install_reports_the_cursor_project_alias(env: Env):
    project = env.project / ".cursor" / "mcp.json"
    project.parent.mkdir()
    project.write_text(
        json.dumps({"mcpServers": {OLD_COMMAND: claude_entry(serve(env))}})
    )

    result = invoke("mcp", "install", "cursor")

    assert result.exit_code == 0, result.output
    [row] = payload(result)["results"]
    assert (row["action"], row["name"], row["scope"]) == (
        "unchanged",
        OLD_COMMAND,
        "project",
    )
    assert not env.cursor_path().exists()


@pytest.mark.parametrize(
    "settings_path",
    [
        "home/.claude/settings.json",
        "project/.claude/settings.json",
        "project/.claude/settings.local.json",
    ],
)
def test_claude_install_reports_a_disabled_project_alias(env: Env, settings_path: str):
    subprocess.run(
        [
            str(env.bin / "claude"),
            "mcp",
            "add",
            "--scope",
            "project",
            OLD_COMMAND,
            f"--env=CUSTOM={SECRET}",
            "--",
            *serve(env),
        ],
        check=True,
        capture_output=True,
    )
    config = env.project / ".mcp.json"
    original = config.read_bytes()
    policy = env.root / settings_path
    policy.parent.mkdir(exist_ok=True)
    policy.write_text(
        json.dumps(
            {
                "enabledMcpjsonServers": [OLD_COMMAND],
                "disabledMcpjsonServers": [OLD_COMMAND],
            }
        )
    )
    policy_before = policy.read_bytes()
    env.clear()

    result = invoke("mcp", "install", "claude")

    message = failure(result)
    assert "claude (project): failed" in message
    assert f"{OLD_COMMAND} registration is disabled in claude" in message
    assert "Review MCP approvals" in message
    [row] = payload(result)["error"]["details"]["results"]
    assert row["name"] == OLD_COMMAND and not row["changed"]
    assert SECRET not in result.output
    assert config.read_bytes() == original
    assert policy.read_bytes() == policy_before
    assert not (env.home / ".claude.json").exists()
    assert not env.calls()


@pytest.mark.parametrize("dry_run", [False, True])
def test_claude_install_leaves_a_disabled_canonical_project_entry_in_place(
    env: Env, dry_run: bool
):
    config = env.project / ".mcp.json"
    entry = claude_entry(
        [*serve(env), "--endpoint", f"https://private.example/{SECRET}"]
    )
    config.write_text(json.dumps({"mcpServers": {"sai": entry}}))
    policy = env.project / ".claude" / "settings.local.json"
    policy.parent.mkdir()
    policy.write_text(json.dumps({"disabledMcpjsonServers": ["sai"]}))
    before = config.read_bytes(), policy.read_bytes()
    flags = ("--dry-run",) if dry_run else ()

    result = invoke("mcp", "install", "claude", "--scope", "project", *flags)

    assert "Claude Code has disabled the project sai registration" in failure(result)
    details = payload(result)["error"]["details"]
    assert not details["reconnectNeeded"]
    [row] = details["results"]
    assert row["scope"] == "project" and not row["changed"] and "argv" not in row
    assert (config.read_bytes(), policy.read_bytes()) == before
    assert not env.calls()
    assert SECRET not in result.output


@pytest.mark.skipif(
    os.name == "nt", reason="Claude uses worktree-local settings on Windows"
)
@pytest.mark.parametrize("name", [OLD_COMMAND, "sai"])
def test_claude_install_honors_settings_from_the_main_checkout(
    env: Env, monkeypatch: pytest.MonkeyPatch, name: str
):
    git = shutil.which("git", path=os.defpath)
    assert git is not None
    monkeypatch.setenv("PATH", os.pathsep.join([str(env.bin), str(Path(git).parent)]))
    linked = env.root / "linked worktree"
    commands = (
        ["init", "--quiet"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "Initial commit",
        ],
        ["worktree", "add", "--quiet", "--detach", str(linked)],
    )
    for args in commands:
        subprocess.run(
            [git, "-C", str(env.project), *args], check=True, capture_output=True
        )
    policy = env.project / ".claude" / "settings.local.json"
    policy.parent.mkdir()
    policy.write_text(json.dumps({"disabledMcpjsonServers": [name]}))
    config = linked / ".mcp.json"
    config.write_text(json.dumps({"mcpServers": {name: claude_entry(serve(env))}}))
    before = config.read_bytes(), policy.read_bytes()
    monkeypatch.chdir(linked)
    scope = "user" if name == OLD_COMMAND else "project"

    result = invoke("mcp", "install", "claude", "--scope", scope)

    message = failure(result)
    assert "claude (project): failed" in message and "disabled" in message
    assert name in message and "Review MCP approvals" in message
    assert (config.read_bytes(), policy.read_bytes()) == before
    assert not (env.home / ".claude.json").exists()
    assert not env.calls()


def test_claude_install_honors_the_project_toggle_for_a_user_alias(env: Env):
    config = env.home / ".claude.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    OLD_COMMAND: claude_entry(serve(env), {"CUSTOM": SECRET})
                },
                "projects": {str(env.project): {"disabledMcpServers": [OLD_COMMAND]}},
            }
        )
    )
    before = config.read_bytes()

    result = invoke("mcp", "install", "claude")

    message = failure(result)
    assert "claude (user): failed" in message
    assert f"{OLD_COMMAND} registration is disabled in claude" in message
    assert config.read_bytes() == before
    assert SECRET not in result.output
    assert not env.calls()


@pytest.mark.parametrize("stale", [False, True])
def test_ordinary_claude_install_preserves_the_legacy_entry(env: Env, stale: bool):
    config = env.home / ".claude.json"
    routing = ["--endpoint", f"https://private.example/{SECRET}"]
    argv = [OLD_COMMAND, *routing] if stale else [*serve(env), *routing]
    overrides = {"SAI_HOME": str(env.root / "other-profile"), "CUSTOM": SECRET}
    config.write_text(
        json.dumps({"mcpServers": {OLD_COMMAND: claude_entry(argv, overrides)}})
    )
    original = config.read_bytes()
    fails_add = FAKE_CLAUDE.replace(
        "args = sys.argv[1:]\n",
        "args = sys.argv[1:]\nif args[1] == 'add':\n    sys.exit(3)\n",
    )
    write_exe(env.bin / "claude", fails_add)

    result = invoke("mcp", "install", "--client", "claude")

    if stale:
        assert "Run `sai doctor` for repair instructions" in failure(result)
        summary = payload(result)["error"]["details"]
    else:
        assert result.exit_code == 0, result.output
        summary = payload(result)
    [row] = summary["results"]
    assert row["name"] == OLD_COMMAND and row["scope"] == "user"
    assert row["action"] == ("failed" if stale else "unchanged")
    assert not summary["reconnectNeeded"]
    assert SECRET not in result.output
    assert config.read_bytes() == original
    assert env.calls() == []

    if stale:
        repaired = invoke(
            "mcp", "install", "--client", "claude", "--repair-existing", OLD_COMMAND
        )
        assert repaired.exit_code == 0, repaired.output
        assert env.claude_json()["mcpServers"] == {
            OLD_COMMAND: claude_entry([*serve(env), *routing], overrides)
        }
        again = invoke("mcp", "install", "--client", "claude")
        assert actions(again) == {"claude": "unchanged"}
        assert env.calls() == []


@pytest.mark.parametrize("different", [True, False])
def test_claude_install_reports_an_explicit_command_blocked_by_a_legacy_alias(
    env: Env, different: bool
):
    config = env.home / ".claude.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    OLD_COMMAND: claude_entry(serve(env), {"CUSTOM": SECRET})
                },
            }
        )
    )
    before = config.read_bytes()
    command = ["uv", "run", "--directory", f"/src/{SECRET}", "sai", "mcp", "serve"]
    if not different:
        command = serve(env)

    result = invoke("mcp", "install", "claude", "--", *command)

    if different:
        error = failure(result)
        assert f"Kept the existing {OLD_COMMAND} registration" in error
        assert "skipped the requested server command" in error
        summary = payload(result)["error"]["details"]
    else:
        assert result.exit_code == 0, result.output
        summary = payload(result)
    [row] = summary["results"]
    assert (row["name"], row["scope"], row["action"]) == (
        OLD_COMMAND,
        "user",
        "failed" if different else "unchanged",
    )
    assert not summary["reconnectNeeded"]
    assert SECRET not in result.output
    assert config.read_bytes() == before
    assert not env.calls()


def test_claude_manual_commands_are_copyable(env: Env, monkeypatch: pytest.MonkeyPatch):
    profile = env.root / "a profile"
    monkeypatch.setenv("SAI_HOME", str(profile))
    config = env.home / ".claude.json"
    config.write_text(
        json.dumps(
            {"mcpServers": {"sai": {**claude_entry(serve(env)), "cwd": "/custom"}}}
        )
    )
    before = config.read_bytes()
    result = invoke("mcp", "install", "claude")
    assert "differs from the requested settings" in failure(result)
    [row] = payload(result)["error"]["details"]["results"]
    assert config.read_bytes() == before
    assert not env.calls()
    [command] = row["fix"].split("`")[1::2]
    shell = shutil.which("sh", path=os.defpath)
    assert shell is not None
    subprocess.run([shell, "-c", command], cwd=env.project, check=True)
    assert env.claude_json()["mcpServers"]["sai"] == claude_entry(
        serve(env), {"SAI_HOME": str(profile)}
    )


@pytest.mark.parametrize("profile", ["relative profile", "~/profile with spaces"])
def test_claude_printed_profile_matches_normal_install(
    env: Env, monkeypatch: pytest.MonkeyPatch, profile: str
):
    monkeypatch.setenv("SAI_HOME", profile)
    config = env.home / ".claude.json"
    config.write_text(
        json.dumps({"mcpServers": {"sai": claude_entry(["old-launcher"])}})
    )
    expected_home = os.path.abspath(os.path.expanduser(profile))
    result = invoke("mcp", "install", "claude")
    failure(result)
    [row] = payload(result)["error"]["details"]["results"]
    commands = row["fix"].split("`")[1::2]
    assert any(
        shlex.quote(f"--env=SAI_HOME={expected_home}") in cmd for cmd in commands
    )
    shell = shutil.which("sh", path=os.defpath)
    assert shell
    monkeypatch.delenv("SAI_HOME")
    subprocess.run([shell, "-c", " && ".join(commands)], cwd=env.home, check=True)
    assert env.claude_json()["mcpServers"]["sai"]["env"] == {"SAI_HOME": expected_home}
    monkeypatch.setenv("SAI_HOME", profile)
    assert actions(invoke("mcp", "install", "claude")) == {"claude": "unchanged"}


@pytest.mark.parametrize("missing_launcher", [False, True])
def test_claude_replacement_checks_absolute_launcher_before_remove(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    missing_launcher: bool,
):
    profile = env.root / "inline profile"
    monkeypatch.setenv("SAI_HOME", str(profile))
    config = env.home / ".claude.json"
    before = json.dumps({"mcpServers": {"sai": claude_entry(["old-launcher"])}})
    config.write_text(before)
    result = invoke("mcp", "install", "claude")
    failure(result)
    [row] = payload(result)["error"]["details"]["results"]
    [command] = row["fix"].split("`")[1::2]
    assert command.startswith(f"test -f {env.sai} && test -x {env.sai} && ")
    assert "claude mcp remove --scope user sai && claude mcp add" in command
    shell = shutil.which("sh", path=os.defpath)
    assert shell
    client_bin = env.root / "client bin"
    client_bin.mkdir()
    shutil.copy2(env.bin / "claude", client_bin / "claude")
    monkeypatch.delenv("SAI_HOME")
    monkeypatch.setenv("PATH", str(client_bin))
    if missing_launcher:
        env.sai.unlink()

    finished = subprocess.run([shell, "-c", command], cwd=env.project, check=False)

    if missing_launcher:
        assert finished.returncode != 0
        assert config.read_text() == before
        assert not env.calls()
    else:
        assert finished.returncode == 0
        assert env.claude_json()["mcpServers"]["sai"] == claude_entry(
            serve(env), {"SAI_HOME": str(profile)}
        )


@pytest.mark.parametrize("surface", ["install", "doctor"])
def test_claude_local_replacement_targets_git_root(
    env: Env, monkeypatch: pytest.MonkeyPatch, surface: str
):
    git = shutil.which("git", path=os.defpath)
    shell = shutil.which("sh", path=os.defpath)
    assert git and shell
    subprocess.run([git, "-C", str(env.project), "init", "--quiet"], check=True)
    nested = env.project / "nested checkout"
    nested.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(env.bin), os.defpath]))
    monkeypatch.chdir(nested)
    root_entry = claude_entry(["missing-root-wrapper"])
    nested_entry = claude_entry(["untouched-nested-wrapper"])
    original = {
        "projects": {
            str(env.project): {"mcpServers": {"sai": root_entry}},
            str(nested): {"mcpServers": {"sai": nested_entry}},
        }
    }
    (env.home / ".claude.json").write_text(json.dumps(original))

    if surface == "doctor":
        result = invoke("doctor", "--offline")
        check = mcp_checks(result)["claude"]
        assert "points to a missing command" in check["summary"]
        fix = check["fix"]
    else:
        result = invoke("mcp", "install", "claude", "--scope", "local")
        assert "differs from the requested settings" in failure(result)
        [row] = payload(result)["error"]["details"]["results"]
        fix = row["fix"]
    assert env.claude_json() == original
    commands = fix.split("`")[1::2]
    assert all(command.startswith(f"(cd {env.project} && ") for command in commands)
    subprocess.run([shell, "-c", " && ".join(commands)], cwd=env.root, check=True)
    projects = env.claude_json()["projects"]
    assert projects[str(env.project)]["mcpServers"]["sai"] == claude_entry(serve(env))
    assert projects[str(nested)]["mcpServers"]["sai"] == nested_entry


@pytest.mark.parametrize("background", [False, True])
def test_update_ignores_a_healthy_claude_profile_pin(env: Env, background: bool):
    config = env.home / ".claude.json"
    profile = env.root / "pinned profile"
    original = {
        "mcpServers": {"sai": claude_entry(serve(env), {"SAI_HOME": str(profile)})}
    }
    config.write_text(json.dumps(original))

    updated = updater.repair_after_update(
        updater.Outcome(updater.UPDATED, "updated"), env.sai, background
    )

    assert updated.mcp_results() == []
    assert "Claude Code" not in updater.mcp_notice(
        updated.mcp_results(), updated.mcp_notes
    )
    assert env.claude_json() == original
    assert not any(call[1] in ("add", "remove") for call in env.calls())


def test_update_commands_keep_the_profile_of_a_stale_claude_entry(env: Env):
    profile = env.root / "pinned profile"
    original = {
        "mcpServers": {
            "sai": claude_entry(["old-launcher"], {"SAI_HOME": str(profile)})
        }
    }
    (env.home / ".claude.json").write_text(json.dumps(original))
    updated = updater.repair_after_update(
        updater.Outcome(updater.UPDATED, "updated"), env.sai, True
    )
    notice = updater.mcp_notice(updated.mcp_results(), updated.mcp_notes)
    assert shlex.quote(f"--env=SAI_HOME={profile}") in notice
    assert env.claude_json() == original


def test_repair_existing_refuses_to_create_a_claude_registration(env: Env):
    args = ("mcp", "install", "--client", "claude", "--repair-existing", OLD_COMMAND)
    config = env.home / ".claude.json"
    config.write_text(
        json.dumps({"mcpServers": {OLD_COMMAND: claude_entry([OLD_COMMAND])}})
    )
    original = config.read_bytes()
    plan = invoke(*args, "--dry-run")
    assert plan.exit_code == 0 and actions(plan) == {"claude": "planned"}
    assert config.read_bytes() == original
    replacement = json.dumps({"mcpServers": {"sai": claude_entry(serve(env))}})
    config.write_text(replacement)

    result = invoke(*args)
    assert f"No existing {OLD_COMMAND} registration" in failure(result)
    assert config.read_text() == replacement
    assert not env.calls()


def test_doctor_reports_remote_claude_entries_without_a_launcher_repair(env: Env):
    config = env.home / ".claude.json"
    remote = {"type": "http", "url": f"https://private.example/{SECRET}"}
    original = json.dumps({"mcpServers": {"sai": remote}})
    config.write_text(original)

    result = invoke("doctor", "--offline")
    check = mcp_checks(result)["claude"]
    assert check["status"] == "fail"
    assert "claude mcp remove --scope user sai" in check["fix"]
    assert "--repair-existing" not in check["fix"]
    assert SECRET not in result.output
    assert config.read_text() == original


@pytest.mark.parametrize("unsupported", [False, True])
def test_explicit_claude_repair_preserves_arguments_or_requires_reset(env, unsupported):
    config = env.home / ".claude.json"
    routing = ["--endpoint", f"https://private.example/{SECRET}"]
    args = [*routing, "--bad-flag"] if unsupported else routing
    entry = claude_entry(["missing-wrapper", *args], {"CUSTOM": SECRET})
    original = json.dumps({"mcpServers": {OLD_COMMAND: entry}})
    config.write_text(original)

    report = invoke("doctor", "--offline")
    command = mcp_checks(report)["claude"]["fix"].split("`")[1]
    assert ("--reset-args" in command) is unsupported
    if unsupported:
        rejected = invoke(*command.removesuffix(" --reset-args").split()[1:])
        assert "Unsupported server arguments" in failure(rejected)
        assert config.read_text() == original

    result = invoke(*command.split()[1:])
    assert result.exit_code == 0, result.output
    assert env.claude_json()["mcpServers"] == {
        OLD_COMMAND: {
            **entry,
            "command": str(env.sai),
            "args": ["mcp", "serve", *([] if unsupported else routing)],
        }
    }
    assert SECRET not in report.output + result.output


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
    assert env.calls() == GETS + LEGACY_GETS
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
        *LEGACY_GETS,
        *CHECK,
    ]
    env.clear()
    again = invoke("mcp", "uninstall", "claude", "codex", "--dry-run")
    assert actions(again) == {"claude": "absent", "codex": "absent"}
    assert env.calls() == GETS + LEGACY_GETS


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
    ]
    cursor = json.loads(env.cursor_path().read_text())["mcpServers"]["sai"]
    assert cursor["env"] == {"SAI_HOME": str(sai_home)}
    assert tomlkit.parse(env.codex_path().read_text())["mcp_servers"]["sai"]["env"] == {
        "SAI_HOME": str(sai_home)
    }
    # A registration with a different SAI_HOME needs a manual update in Claude.
    monkeypatch.delenv("SAI_HOME")
    again = invoke("mcp", "install", "claude", "codex", "cursor")
    assert "differs from the requested settings" in failure(again)
    assert [row["action"] for row in payload(again)["error"]["details"]["results"]] == [
        "failed",
        "replaced",
        "replaced",
    ]
    assert env.claude_json()["mcpServers"]["sai"]["env"] == {"SAI_HOME": str(sai_home)}
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory symlink")
def test_list_hides_the_saved_directory_for_a_normalized_match(env: Env):
    private_dir = env.root / f"deployment-{SECRET}"
    private_dir.symlink_to(env.bin, target_is_directory=True)
    env.write_codex({"sai": claude_entry([str(private_dir / "sai"), "mcp", "serve"])})
    before = env.codex_path().read_bytes()

    result = invoke("mcp", "list")

    assert result.exit_code == 0
    assert SECRET not in result.output
    codex = next(row for row in payload(result)["clients"] if row["client"] == "codex")
    assert codex["current"] and codex["registeredCommand"] == serve(env)
    [registration] = codex["registrations"]
    assert registration["current"] and registration["command"] == serve(env)
    assert env.codex_path().read_bytes() == before


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
    env.write_codex(
        {
            "sai": {
                "type": "stdio",
                "command": str(env.sai),
                "args": ["mcp", "serve"],
                "env": {"TOKEN": SECRET},
            }
        }
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


@pytest.mark.parametrize(
    ("client", "args", "command"),
    [
        ("claude", ("install", "claude"), "claude mcp add"),
        ("codex", ("install", "codex"), "codex mcp get sai --json"),
        ("codex", ("list",), "codex mcp get sai --json"),
    ],
)
def test_client_failure_keeps_config_values_out_of_output(
    env: Env, client: str, args: tuple[str, ...], command: str
):
    diagnostic = f"Error: {OTHER} has invalid configuration with TOKEN={SECRET}"
    write_exe(
        env.bin / client,
        f"import sys\nprint({diagnostic!r})\n"
        f"print({diagnostic!r}, file=sys.stderr)\nsys.exit(7)\n",
    )

    result = invoke("mcp", *args)
    if args == ("list",):
        assert result.exit_code == 0, result.output
        row = next(row for row in payload(result)["clients"] if row["client"] == client)
        error = row["error"]
    else:
        error = failure(result)
    assert f"`{command}` exited 7" in error
    assert SECRET not in result.output and OTHER not in result.output


def test_codex_first_install_accepts_a_startup_warning_before_not_found(env: Env):
    warning = (
        "WARNING: proceeding, even though we could not create PATH aliases: "
        "Read-only file system (os error 30)"
    )
    write_exe(
        env.bin / "codex",
        FAKE_CODEX.replace(
            "    if entry is None:\n",
            f"    if entry is None:\n        print({warning!r}, file=sys.stderr)\n",
        ),
    )

    result = invoke("mcp", "install", "codex")

    assert result.exit_code == 0, result.output
    assert actions(result) == {"codex": "added"}
    entries = tomlkit.parse(env.codex_path().read_text())["mcp_servers"]
    assert set(entries) == {"sai"}
    assert entries["sai"]["command"] == str(env.sai)
    assert env.calls() == GETS + LEGACY_GETS + CHECK
    assert warning not in result.output


@pytest.mark.parametrize(
    ("exit_code", "stdout", "stderr"),
    [
        (7, "", f"Config I/O error: no MCP server could be read. TOKEN={SECRET}"),
        (1, "", "Error: No MCP server named 'sai' found."),
        (7, "", f"Error: No MCP server named '{OLD_COMMAND}' found."),
        (1, "Config I/O error", f"Error: No MCP server named '{OLD_COMMAND}' found."),
        (1, "", f"I/O error\nError: No MCP server named '{OLD_COMMAND}' found."),
        (1, "", f"WARNING: I/O diagnostic contains no MCP server text. TOKEN={SECRET}"),
        (0, "{}", ""),
    ],
)
def test_codex_install_refuses_to_write_after_an_uncertain_alias_lookup(
    env: Env, exit_code: int, stdout: str, stderr: str
):
    env.write_codex({OLD_COMMAND: claude_entry(serve(env))})
    original = env.codex_path().read_bytes()
    write_exe(
        env.bin / "codex",
        FAKE_CODEX.replace(
            "name = args[2]\n",
            f"name = args[2]\nif name == {OLD_COMMAND!r}:\n"
            f"    print({stdout!r})\n    print({stderr!r}, file=sys.stderr)\n"
            f"    sys.exit({exit_code})\n",
        ),
    )

    result = invoke("mcp", "install", "codex")

    diagnostic = (
        f"exited {exit_code}" if exit_code else "returned an invalid registration"
    )
    assert f"`codex mcp get {OLD_COMMAND} --json` {diagnostic}" in failure(result)
    assert SECRET not in result.output
    assert env.codex_path().read_bytes() == original
    assert env.calls() == GETS + LEGACY_GETS[:1]


@pytest.mark.parametrize("args", [("install", "codex"), ("list",)])
@pytest.mark.parametrize("exit_code", [7, 0])
def test_codex_lookup_hides_invalid_utf8(
    env: Env, args: tuple[str, ...], exit_code: int
):
    stream = "stderr" if exit_code else "stdout"
    private = bytes([255]) + SECRET.encode()
    write_exe(
        env.bin / "codex",
        f"import sys\nsys.{stream}.buffer.write({private!r})\nsys.exit({exit_code})\n",
    )

    result = invoke("mcp", *args)

    if args == ("list",):
        assert result.exit_code == 0, result.output
        error = next(
            row["error"]
            for row in payload(result)["clients"]
            if row["client"] == "codex"
        )
    else:
        error = failure(result)
    diagnostic = "exited 7" if exit_code else "printed invalid JSON"
    assert f"`codex mcp get sai --json` {diagnostic}" in error
    assert log.flush()
    logs = "".join(path.read_text() for path in log.directory().glob("*.jsonl"))
    for value in (SECRET, "0xff", "position 0", "UnicodeDecodeError"):
        assert value not in result.output + logs
    assert not env.codex_path().exists()


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
    env.write_codex(
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
    listed = invoke("mcp", "list")
    rows = {r["client"]: r for r in payload(listed)["clients"]}
    assert rows["claude"]["current"] is False
    assert rows["claude"]["staleEnv"] == ["SAI_ENDPOINT"]
    assert rows["codex"]["staleEnv"] == ["SAI_LCD_ENDPOINT"]
    assert endpoint not in listed.stdout + listed.stderr
    result = invoke("mcp", "install", "claude", "codex")
    assert "differs from the requested settings" in failure(result)
    assert endpoint not in result.output
    assert env.claude_json()["mcpServers"]["sai"]["env"] == {"SAI_ENDPOINT": endpoint}
    again = invoke("mcp", "install", "codex")
    assert actions(again) == {"codex": "unchanged"}


def test_disabled_codex_entry_is_not_current_and_is_replaced(env: Env):
    env.write_codex(
        {
            "sai": {
                "enabled": False,
                "command": str(env.sai),
                "args": ["mcp", "serve"],
            },
        }
    )
    row = payload(invoke("mcp", "list"))["clients"][1]
    assert (row["client"], row["current"], row["disabled"]) == ("codex", False, True)
    result = invoke("mcp", "install", "codex")
    assert actions(result) == {"codex": "replaced"}
    assert env.calls() == GETS + LEGACY_GETS + GETS + CHECK
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
    env.write_codex(
        {
            "sai": {
                "type": "stdio",
                "command": str(env.sai),
                "args": ["mcp", "serve"],
            }
        }
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
    entry = codex_transport(argv, extra)
    del entry["cwd"]
    env.write_codex({"sai": entry})


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


@pytest.mark.parametrize("scope", ["user", "project", "local"])
@pytest.mark.parametrize("output_mode", ["text", "json", "installer"])
def test_claude_install_reports_scoped_commands_without_credentials(
    env: Env, monkeypatch: pytest.MonkeyPatch, scope: str, output_mode: str
):
    profile = env.root / "selected profile"
    monkeypatch.setenv("SAI_HOME", str(profile))
    command = [f"/opt/launcher-{SECRET}", "--credential", SECRET]
    entry = claude_entry(command, {"PRIVATE_TOKEN": SECRET})
    data = {"mcpServers": {"sai": entry, OTHER: {"command": "untouched"}}}
    config = (
        env.project / ".mcp.json" if scope == "project" else env.home / ".claude.json"
    )
    if scope == "local":
        data = {"projects": {str(env.project): data}}
    config.write_text(json.dumps(data))
    before = config.read_bytes()
    args = ["mcp", "install", "claude", "--scope", scope, "--", *command]
    if output_mode == "installer":
        client = mcp_install.make_clients(env.home, os.environ, env.project)["claude"]
        target, _ = mcp_install.server_target(
            command, os.environ, env.home, str(env.root)
        )
        installed = mcp_install.install_one(client, scope, target, dry_run=False)
        row = installed.to_json()
        assert not installed.changed
        output = json.dumps(row)
    elif output_mode == "json":
        result = invoke(*args)
        assert "differs from the requested settings" in failure(result)
        details = payload(result)["error"]["details"]
        assert not details["reconnectNeeded"]
        [row] = details["results"]
        assert not row["changed"]
        output = result.output
    else:
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 1
        row = {"action": "failed", "fix": result.output}
        output = result.output
    assert row["action"] == "failed"
    assert f"claude mcp remove --scope {scope} sai" in row["fix"]
    assert (
        f"claude mcp add --scope {scope} sai {shlex.quote(f'--env=SAI_HOME={profile}')}"
        f" -- {shlex.quote(str(env.sai))} mcp serve"
    ) in row["fix"]
    assert "substitute your requested command" in row["fix"]
    assert SECRET not in output and OTHER not in output
    assert config.read_bytes() == before
    assert not env.calls()
