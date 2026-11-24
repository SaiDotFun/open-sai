"""Isolated registration fixtures for both CLI entry points."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tomllib
from pathlib import Path
from typing import Any

from compare import Entry, Normalize

CLIENT_CONFIGS = (
    ".claude.json",
    ".mcp.json",
    ".codex/config.toml",
    ".codex/project.json",
    ".cursor/mcp.json",
    "argv.jsonl",
)
SECRET = "private-fixture-value-must-stay-in-client-config"
CLIENT_SCRIPT = """
import json
import os
import sys
import tomllib
from pathlib import Path

home = Path(os.environ["HOME"])
argv = sys.argv[1:]
program = Path(sys.argv[0]).name
with (home / "argv.jsonl").open("a") as trace:
    scope = "project" if Path.cwd() == home else "user"
    trace.write(json.dumps([program, scope, *argv]) + "\\n")
if program == "codex":
    action, name = argv[1:3]
    assert argv == ["mcp", "get", name, "--json"]
    config = Path(os.environ["CODEX_HOME"]) / "config.toml"
    servers = tomllib.loads(config.read_text()).get("mcp_servers", {}) if config.exists() else {}
    entry = servers.get(name)
    project = Path(os.environ["CODEX_HOME"]) / "project.json"
    if Path.cwd() == home and name == "sai" and project.exists():
        if entry is None and (home / "fail-second-project-query").exists():
            observed = home / "first-project-query"
            if observed.exists():
                sys.exit(9)
            observed.touch()
        entry = json.loads(project.read_text())
    if entry is None:
        if (name == "sai" + "-mcp" and Path.cwd() != config.parent
                and (home / "drop-codex-after-discovery").exists()):
            Path(sys.argv[0]).unlink()
        if name == "sai" and (home / "fail-uninstall-verification").exists():
            sys.exit(9)
        sys.exit(f"Error: No MCP server named '{name}' found.")
    if (home / "fail-verification").exists() and entry["command"] == str(home / "bin/sai"):
        print("private-fixture-value-must-stay-in-client-config", file=sys.stderr)
        sys.exit(9)
    transport = dict(entry)
    reply = {"transport": {"type": "stdio", **transport}, "enabled": transport.get("enabled", True)}
    for key in ["enabled_tools", "disabled_tools"]:
        if key in transport:
            reply[key] = transport[key]
    print(json.dumps(reply))
elif program == "claude":
    assert argv[0] == "mcp" and argv[2] == "--scope"
    scope, name = argv[3:5]
    path = Path.cwd() / ".mcp.json" if scope == "project" else home / ".claude.json"
    data = json.loads(path.read_text()) if path.is_file() else {}
    holder = data["projects"][str(Path.cwd())] if scope == "local" else data
    entries = holder.setdefault("mcpServers", {})
    if argv[1] == "remove":
        del entries[name]
    else:
        assert argv[1] == "add"
        divider = argv.index("--")
        launcher, *args = argv[divider + 1 :]
        variables = dict(item.removeprefix("--env=").split("=", 1) for item in argv[5:divider])
        entries[name] = {"type": "stdio", "command": launcher, "args": args, "env": variables}
    path.write_text(json.dumps(data))
else:
    raise AssertionError("A registered launcher was invoked")
"""


def write(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)


def seed_clients(home: Path, env: dict[str, str], scenario: str) -> dict[str, bytes]:
    programs = home / "bin"
    env["PATH"] = os.pathsep.join([str(programs), env["PATH"]])
    env["CODEX_HOME"] = str(home / ".codex")
    (home / ".codex").mkdir()
    for name in ("claude", "codex", "sai"):
        write(programs / name, f"#!{sys.executable}\n{CLIENT_SCRIPT}", 0o755)
    if scenario == "empty":
        (home / ".cursor").mkdir()
        return {}
    if scenario == "uninstall-query-failure":
        (home / "fail-uninstall-verification").touch()
    stable = programs / "sai"
    saved = stable
    args = ["mcp", "serve"]
    home_setting = str(home / "sai")
    if scenario in {"retired", "partial", "shadowed"}:
        saved = programs / ("sai" + "-mcp")
        args = ["--endpoint", "https://fixture.invalid/api?token=private"]
        if scenario == "partial":
            (home / "fail-verification").touch()
    elif scenario == "missing":
        saved = home / "removed/sai"
    elif scenario == "cache":
        saved = home / ".cache/uv/archive-v0/id/bin/sai"
        saved.parent.mkdir(parents=True)
        shutil.copy2(stable, saved)
    elif scenario == "legacy":
        home_setting = str(home / "pinned-profile")
    name = "sai" + "-mcp" if scenario == "legacy" else "sai"
    entry = {"command": str(saved), "args": args, "env": {"SAI_HOME": home_setting}}
    extra = scenario != "current"
    if extra:
        entry["env"]["FIXTURE_CREDENTIAL"] = SECRET
    unrelated = {
        "url": "https://fixture.invalid/mcp",
        "headers": {"Authorization": SECRET},
    }
    document = {"mcpServers": {name: entry, "other-server": unrelated}}
    write(home / ".cursor/mcp.json", json.dumps(document), 0o640)
    if scenario == "uninstall-scopes":
        write(home / ".mcp.json", json.dumps(document), 0o640)
        document["projects"] = {str(home): {"mcpServers": {name: entry}}}
    write(home / ".claude.json", json.dumps(document), 0o640)
    if scenario == "shadowed":
        write(home / ".codex/project.json", json.dumps(entry))
    lines = [
        "# Preserve the Codex comment",
        'unrelated.dotted = "root value"',
        "",
        f"[mcp_servers.{name}]",
        f"command = {json.dumps(str(saved))} # Preserve the launcher comment",
        f"args = {json.dumps(args)}",
    ]
    if extra:
        lines += ['disabled_tools = ["trade_open"]']
    lines += [
        "",
        f"[mcp_servers.{name}.env]",
        f"SAI_HOME = {json.dumps(home_setting)}",
    ]
    if extra:
        lines += [f"FIXTURE_CREDENTIAL = {json.dumps(SECRET)}"]
    lines += [
        "",
        "# Other server",
        "[mcp_servers.other-server]",
        f"url = {json.dumps(unrelated['url'])}",
        f"headers = {{ Authorization = {json.dumps(SECRET)} }}",
    ]
    config = "\n".join(lines) + "\n"
    if scenario == "uninstall-crlf":
        config = (
            '# Keep this comment\r\nmodel = "before"\r\n\r\n'
            "[mcp_servers.sai]\r\n"
            f"command = {json.dumps(str(saved))}\r\n"
            'args = ["mcp", "serve"]\r\n\r\n'
            '# Other server\r\n[mcp_servers.other-server]\r\nurl = "https://fixture.invalid/mcp"\r\n\r\n'
            '[profiles.demo]\r\nmodel =    "after"\r\n'
        )
    write(home / ".codex/config.toml", config, 0o640)
    read_only = {
        "uninstall-codex-read-only": ".codex/config.toml",
        "uninstall-cursor-read-only": ".cursor/mcp.json",
    }.get(scenario)
    if read_only:
        (home / read_only).chmod(0o444)
    return {
        relative: (home / relative).read_bytes()
        for relative in CLIENT_CONFIGS
        if (home / relative).is_file()
    }


def client_state(
    home: Path, previous: dict[str, bytes], normalize: Normalize
) -> dict[str, Entry]:
    files = {}
    for relative in CLIENT_CONFIGS:
        path = home / relative
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if relative.endswith(".jsonl"):
            data: Any = [json.loads(line) for line in raw.splitlines()]
        elif relative.endswith(".toml"):
            source = raw.decode()
            data = {
                "settings": tomllib.loads(source),
                "comments": [
                    f"#{line.partition('#')[2]}"
                    for line in source.splitlines()
                    if "#" in line
                ],
            }
            if b"\r\n" in previous.get(relative, b""):
                data["source"] = source
        else:
            data = json.loads(raw)
            if relative == ".claude.json" and isinstance(data.get("projects"), dict):
                data["projects"] = {
                    normalize.text(project): entry
                    for project, entry in data["projects"].items()
                }
        mode = stat.S_IMODE(path.stat().st_mode)
        files[f"clients/{relative}"] = Entry(
            "file",
            mode,
            {"changed": previous.get(relative) != raw, "data": normalize.paths(data)},
        )
    return files
