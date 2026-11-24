"""Regression tests for Rust doctor and MCP registration review findings."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import pytest
from mcp_clients import seed_clients, write
from sai_cli.tests.loopback import http_server


@pytest.fixture
def candidate(cli_pair: tuple[tuple[str, ...], tuple[str, ...]]) -> str:
    return cli_pair[1][0]


@pytest.fixture
def fixture_env(tmp_path: Path) -> dict[str, str]:
    sai_home = tmp_path / "sai"
    sai_home.mkdir(mode=0o700)
    return {
        "HOME": str(tmp_path),
        "SAI_HOME": str(sai_home),
        "SAI_LOG": "false",
        "PATH": os.defpath,
        "NO_PROXY": "127.0.0.1,localhost,::1",
    }


def invoke(
    candidate: str, home: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    project = home / "project"
    project.mkdir(exist_ok=True)
    return subprocess.run(
        [candidate, "--json", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def checks(result: subprocess.CompletedProcess[str]) -> dict[str, dict[str, Any]]:
    body = json.loads(result.stdout)
    report = body["error"]["details"] if "error" in body else body
    return {check["id"]: check for check in report["checks"]}


def test_claude_install_keeps_entry_until_replaced(
    candidate: str, tmp_path: Path, fixture_env: dict[str, str]
) -> None:
    seed_clients(tmp_path, fixture_env, "current")
    path = tmp_path / ".claude.json"
    original = json.loads(path.read_text())
    original["mcpServers"]["sai"]["args"] += ["--endpoint", "https://fixture.invalid"]
    before = json.dumps(original).encode()
    path.write_bytes(before)
    write(
        tmp_path / "bin/claude",
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[2] == 'add':\n"
        "    sys.exit(7)\n"
        "path = Path.home() / '.claude.json'\n"
        "doc = json.loads(path.read_text())\n"
        "del doc['mcpServers']['sai']\n"
        "path.write_text(json.dumps(doc))\n",
        0o755,
    )

    result = invoke(candidate, tmp_path, fixture_env, "mcp", "install", "claude")
    if result.returncode == 0:
        server = json.loads(path.read_text())["mcpServers"]
        assert server["sai"]["args"] == ["mcp", "serve"]
        assert server["other-server"] == original["mcpServers"]["other-server"]
        assert json.loads(result.stdout)["results"][0]["action"] == "replaced"
    else:
        assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.parametrize("credentials", [False, True])
def test_doctor_rejects_unsafe_transport_before_connecting(
    candidate: str, tmp_path: Path, fixture_env: dict[str, str], credentials: bool
) -> None:
    seen: list[str] = []

    class Proxy(BaseHTTPRequestHandler):
        def respond(self) -> None:
            seen.append(self.command)
            self.send_response(403)
            self.end_headers()

        do_GET = respond
        do_CONNECT = respond

        def log_message(self, *_args: object) -> None:
            pass

    # The reserved address can only be reached through the local fixture proxy.
    # The Keeper endpoint is refused independently so it cannot contact the proxy.
    with http_server(Proxy) as proxy:
        fixture_env |= {
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "NO_PROXY": "",
            "SAI_ENDPOINT": "http://192.0.2.1:9",
            "SAI_LCD_ENDPOINT": (
                "https://fixture-user:fixture-password@192.0.2.1:9"
                if credentials
                else "http://192.0.2.1:9"
            ),
            "SAI_WS_ENDPOINT": (
                "wss://fixture-user:fixture-password@192.0.2.1:9"
                if credentials
                else "ws://192.0.2.1:9"
            ),
        }
        result = invoke(candidate, tmp_path, fixture_env, "doctor", "--no-fix")
    report = checks(result)
    assert seen == []
    assert report["lcd"]["status"] == report["websocket"]["status"] == "fail"
    lcd_error = (
        "Credentials are not allowed in the LCD URL"
        if credentials
        else "LCD endpoint must use HTTPS, except on loopback"
    )
    assert lcd_error in report["lcd"]["summary"]
    assert report["websocket"]["summary"].endswith("StreamError")
    assert "fixture-password" not in result.stdout + result.stderr


def test_doctor_restores_codex_config_when_verification_fails(
    candidate: str, tmp_path: Path, fixture_env: dict[str, str]
) -> None:
    seed_clients(tmp_path, fixture_env, "partial")
    codex = tmp_path / ".codex/config.toml"
    cursor = tmp_path / ".cursor/mcp.json"
    before = codex.read_bytes()
    result = invoke(candidate, tmp_path, fixture_env, "doctor", "--offline")
    report = checks(result)
    assert report["mcp.codex"]["status"] == "fail"
    assert report["mcp.cursor"]["status"] == "fixed"
    assert codex.read_bytes() == before
    assert stat.S_IMODE(codex.stat().st_mode) == 0o640
    assert json.loads(cursor.read_text())["mcpServers"]["sai"]["command"] == str(
        tmp_path / "bin/sai"
    )


@pytest.mark.parametrize("registered", [True, False])
def test_doctor_prefers_the_running_install_over_path_shadow(
    candidate: str, tmp_path: Path, fixture_env: dict[str, str], registered: bool
) -> None:
    stable = tmp_path / ".local/bin/sai"
    stable.parent.mkdir(parents=True)
    installed = stable.with_name("sai-release")
    shutil.copy2(candidate, installed)
    if registered:
        stable.symlink_to(installed.name)
    shadow = tmp_path / "shadow/sai"
    write(shadow, "#!/bin/sh\nexit 77\n", 0o755)
    fixture_env["PATH"] = f"{shadow.parent}:{stable.parent}:{os.defpath}"
    config = tmp_path / ".cursor/mcp.json"
    write(
        config,
        json.dumps(
            {
                "mcpServers": {
                    "sai": {
                        "command": str(tmp_path / "removed/sai"),
                        "args": ["mcp", "serve"],
                        "env": {"SAI_HOME": fixture_env["SAI_HOME"]},
                    }
                }
            }
        ),
    )
    before = config.read_bytes()
    result = invoke(str(installed), tmp_path, fixture_env, "doctor", "--offline")
    cursor = checks(result)["mcp.cursor"]
    if registered:
        assert cursor["status"] == "fixed"
        assert json.loads(config.read_text())["mcpServers"]["sai"]["command"] == str(
            stable
        )
    else:
        assert cursor["status"] == "fail"
        assert cursor["fix"] == "Install a stable Sai launcher on PATH"
        assert config.read_bytes() == before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX named pipes")
@pytest.mark.parametrize("source", ["settings", "cursor", "ca"])
def test_doctor_refuses_named_pipes(
    candidate: str, tmp_path: Path, fixture_env: dict[str, str], source: str
) -> None:
    args = ["doctor", "--no-fix"]
    if source == "settings":
        path = tmp_path / "sai/config.toml"
        check = "config"
    elif source == "cursor":
        path = tmp_path / ".cursor/mcp.json"
        check = "mcp.cursor"
    else:
        path = tmp_path / "ca.pem"
        check = "lcd"
        fixture_env |= {
            "SSL_CERT_FILE": str(path),
            "SAI_ENDPOINT": "https://127.0.0.1:1/query",
            "SAI_WS_ENDPOINT": "wss://127.0.0.1:1/query",
            "SAI_LCD_ENDPOINT": "https://127.0.0.1:1",
        }
    path.parent.mkdir(exist_ok=True)
    os.mkfifo(path, 0o600)
    if source != "ca":
        args.append("--offline")
    result = invoke(candidate, tmp_path, fixture_env, *args)
    assert checks(result)[check]["status"] == "fail"
    assert stat.S_ISFIFO(path.stat().st_mode)


@pytest.mark.skipif(sys.platform != "linux", reason="needs a private procfs mount")
def test_doctor_checks_foreign_profile_before_creating_children(
    candidate: str, tmp_path: Path, fixture_env: dict[str, str]
) -> None:
    unshare = shutil.which("unshare")
    if unshare is None:
        pytest.skip("unshare is unavailable")
    mount = tmp_path / "sai"
    fixture_env["SAI_LOG"] = "true"
    script = """
from pathlib import Path
import os
import subprocess
import sys

root, binary = sys.argv[1:]
root = Path(root)
if root.stat().st_uid == os.getuid():
    sys.exit(77)
run = subprocess.run([binary, '--json', 'doctor', '--offline'], capture_output=True)
assert not (root / 'trader').exists() and not (root / 'logs').exists()
sys.stdout.buffer.write(run.stdout)
sys.stderr.buffer.write(run.stderr)
sys.exit(run.returncode)
"""
    result = subprocess.run(
        [
            unshare,
            "--user",
            "--map-root-user",
            "--fork",
            "--pid",
            f"--mount-proc={mount}",
            sys.executable,
            "-c",
            script,
            str(mount),
            candidate,
        ],
        cwd=tmp_path,
        env=fixture_env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode == 77 or result.stderr.startswith("unshare:"):
        pytest.skip("a private mount with a foreign profile owner is unavailable")
    home = checks(result)["home"]
    assert home["status"] == "fail" and "belongs to another user" in home["summary"]


def test_doctor_respects_read_only_cursor_config(
    candidate: str, tmp_path: Path, fixture_env: dict[str, str]
) -> None:
    seed_clients(tmp_path, fixture_env, "retired")
    path = tmp_path / ".cursor/mcp.json"
    path.chmod(0o444)
    before = path.read_bytes()
    result = invoke(candidate, tmp_path, fixture_env, "doctor", "--offline")
    cursor = checks(result)["mcp.cursor"]
    assert cursor["status"] == "fail"
    assert "read-only" in cursor["summary"]
    assert path.read_bytes() == before and stat.S_IMODE(path.stat().st_mode) == 0o444
