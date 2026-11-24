"""Regression tests for Rust doctor and MCP registration review findings."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp_clients import seed_clients, write


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
    return subprocess.run(
        [candidate, "--json", *args],
        cwd=home,
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
