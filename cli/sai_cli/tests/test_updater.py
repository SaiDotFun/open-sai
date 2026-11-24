"""`sai update` installs: real `uv` against a loopback release built from a
throwaway package, plus the locks, the notices, and the daily background run.

Every uv tool directory, the uv cache, and SAI_HOME are temporary, so the
real installed sai and ~/.sai are never touched.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
import tomlkit
from click.testing import CliRunner
from fake_release import build_wheel, downloads, release_routes
from sai_cli.core import log, update, updater
from sai_cli.sai_cli import EXIT_UNSUPPORTED, auto_update_applies, main
from test_mcp_install import (
    FAKE_CLAUDE,
    FAKE_CODEX,
    OLD_COMMAND,
    OTHER,
    SECRET,
    write_exe,
    write_project_mcp_denials,
)
from test_update import API_PATH, Stub, listing, make_prefix

REAL_UV = os.environ.get("UV") or shutil.which("uv")
needs_uv = pytest.mark.skipif(REAL_UV is None, reason="uv is not on PATH")


class Tool:
    """A temporary uv tool home holding one `sai-cli` install."""

    def __init__(self, root: Path, base: str) -> None:
        self.base = base
        self.prefix = root / "tools" / "sai-cli"
        self.sai = root / "bin" / "sai"

    def uv_install(self, spec: str) -> None:
        assert REAL_UV is not None
        subprocess.run(
            [REAL_UV, "tool", "install", spec],
            check=True,
            capture_output=True,
            timeout=120,
        )

    def install_release(self, version: str) -> None:
        name = update.wheel_name(version)
        self.uv_install(f"{self.base}{downloads(version)}/{name}")

    def version(self) -> str:
        done = subprocess.run(
            [str(self.sai), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip().removeprefix("sai, version ")

    def server_source(self) -> str:
        found = list(
            self.prefix.glob("lib/*/site-packages/sai_cli/mcp_server/server.py")
        )
        return found[0].read_text()

    def options(self, **changes) -> updater.Options:
        options = updater.Options(
            api_base=self.base, web_base=self.base, prefix=self.prefix
        )
        return replace(options, **changes)

    def update(self, **changes) -> updater.Outcome:
        return updater.update_now(self.options(**changes))

    def kind(self) -> str:
        return update.install_kind(self.prefix, self.base).kind


@pytest.fixture
def sai_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "sai-home"
    monkeypatch.setenv("SAI_HOME", str(home))
    return home


@pytest.fixture
def uv_dirs(tmp_path, monkeypatch, sai_home) -> Path:
    """Point uv at temporary tool, bin, and cache directories."""
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    monkeypatch.setenv("UV_NO_CONFIG", "1")
    monkeypatch.delenv("UV", raising=False)
    return tmp_path


def user_files(home: Path) -> dict[str, bytes]:
    """SAI_HOME files that belong to the user, not to the updater."""
    own = {"update.json", "update.lock", "update-cache.lock", "cache"}
    return {
        str(path.relative_to(home)): path.read_bytes()
        for path in sorted(home.rglob("*"))
        if path.is_file() and path.relative_to(home).parts[0] not in own
    }


BROKEN_IMPORT = 'raise ImportError("this release cannot import")\n'
# Imports in a trial environment but not once it is the installed tool, so
# only the check after the install can catch it.
BROKEN_WHEN_INSTALLED = (
    "import os, sys\n"
    'if os.sep + "tools" + os.sep in sys.prefix + os.sep:\n'
    '    raise ImportError("this release cannot import once installed")\n'
)


# Real uv


@pytest.mark.slow
@needs_uv
@pytest.mark.parametrize(
    "mode",
    [
        "foreground",
        "json",
        "background",
        "failed",
        "deleted-cwd",
        "deleted-cwd-relative",
        "legacy-project",
    ],
)
def test_update_repairs_mcp_after_verification(uv_dirs, monkeypatch, mode):
    home = uv_dirs / "home"
    home.mkdir()
    codex_home = uv_dirs / "codex-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("FAKE_LOG", str(uv_dirs / "client-calls.jsonl"))
    project = uv_dirs / "project"
    project.mkdir()
    legacy = mode == "legacy-project"
    background = mode == "background"
    name = OLD_COMMAND if legacy else "sai"
    scope = "project" if legacy else "user"
    config = project / ".mcp.json" if legacy else home / ".claude.json"
    entry = {
        "command": OLD_COMMAND,
        "args": ["--endpoint", "https://private.example/" + SECRET],
        "env": {"CUSTOM": SECRET},
    }
    other = {"url": "https://private.example/mcp", "headers": {"X-Token": SECRET}}
    original = json.dumps({"mcpServers": {name: entry, OTHER: other}})
    config.write_text(original)
    cursor = project / ".cursor" / "mcp.json"
    server_body = ""
    if mode.startswith("deleted-cwd"):
        # An installed release's import runs in the updater's neutral directory.
        # Remove the CLI's actual cwd during post-install verification.
        server_body = (
            "import sys\n"
            "from pathlib import Path\n"
            f"project = Path({str(project)!r})\n"
            "if 'tools' in Path(sys.prefix).parts and project.is_dir():\n"
            "    project.rmdir()\n"
        )
    else:
        (home / ".cursor").mkdir()
        cursor.parent.mkdir()
        cursor.write_text(original)
    if legacy:
        policies = write_project_mcp_denials(project)
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        codex_config.write_text(
            tomlkit.dumps(
                {
                    "mcp_servers": {
                        name: {**entry, "enabled": False, "enabled_tools": []}
                    }
                }
            )
        )
    wheels = {
        "0.1.0": build_wheel("0.1.0"),
        "0.2.0": build_wheel("0.2.0", server_body),
    }
    bad_hash = ("0.2.0",) if mode == "failed" else ()
    with Stub(release_routes(wheels, bad_hash=bad_hash)) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        write_exe(tool.sai.with_name("claude"), FAKE_CLAUDE)
        if legacy:
            write_exe(tool.sai.with_name("codex"), FAKE_CODEX)
        # Supply the installed tool prefix to the real CLI. Keep the test
        # runner's Python and dependencies while uv replaces the tool.
        monkeypatch.setenv(
            "PATH", str(tool.sai.parent) + os.pathsep + os.environ["PATH"]
        )
        args = ["update", "--api-base", stub.url, "--web-base", stub.url]
        if mode == "json":
            args.insert(0, "--json")
        if background:
            args.append("--background")
        monkeypatch.chdir(project)
        if mode == "deleted-cwd-relative":
            monkeypatch.setenv("SAI_HOME", "../sai-home")
            monkeypatch.setenv("SAI_LOG", "false")
        with monkeypatch.context() as runtime:
            runtime.setattr(sys, "prefix", str(tool.prefix))
            result = CliRunner().invoke(main, args, catch_exceptions=False)
        monkeypatch.chdir(uv_dirs)
        if mode == "deleted-cwd-relative":
            monkeypatch.setenv("SAI_HOME", str(uv_dirs / "sai-home"))
        if mode == "failed":
            assert result.exit_code == 1
            assert config.read_text() == original
            assert not (uv_dirs / "client-calls.jsonl").exists()
            return
        assert result.exit_code == 0, result.output
        assert tool.version() == "0.2.0"
        expected = {
            name: {
                **entry,
                "command": str(tool.sai),
                "args": ["mcp", "serve", *entry["args"]],
            },
            OTHER: other,
        }
        assert config.read_text() == original
        if background:
            assert cursor.read_text() == original
        elif mode.startswith("deleted-cwd"):
            assert not project.exists()
        else:
            assert json.loads(cursor.read_text())["mcpServers"] == expected
        if legacy:
            assert all(
                path.read_bytes() == content for path, content in policies.items()
            )
            assert tomlkit.parse(codex_config.read_text()).unwrap()["mcp_servers"] == {
                name: {**expected[name], "enabled": False, "enabled_tools": []},
            }
        state = updater.read_state()
        assert state["result"]["status"] == updater.UPDATED
        if mode == "deleted-cwd-relative":
            assert "The working directory is gone." in result.stdout
            assert state["result"]["mcpNotes"] == ["cwd-missing"]
            return
        assert log.flush()
        repair = [
            event
            for path in log.directory().glob("update-*.jsonl")
            for line in path.read_text().splitlines()
            if (event := json.loads(line))["event"] == "update.mcp_repair"
        ]
        assert len(repair) == 1
        assert repair[0]["background"] is background
        claude = repair[0]["clients"][0]
        assert claude["client"] == "claude"
        assert claude["status"] == "fail" and claude["changes"] == []
        assert claude["manualRepairs"] == [{"scope": scope, "name": name}]
        command = (
            f"sai mcp install --client claude --scope {scope} --repair-existing {name}"
        )
        assert command in claude["fix"]
        assert state["result"]["mcp"] == repair[0]["clients"]
        if legacy:
            clients = {client["client"]: client for client in repair[0]["clients"]}
            assert set(clients) == {"claude", "codex", "cursor"}
            assert all(client["legacyName"] == name for client in clients.values())
            assert f"kept the existing {name} name" in result.stdout
        message = "Claude Code MCP registrations need manual repair."
        if background:
            assert result.output == ""
            notices: list[str] = []
            # This check just ran. The next terminal command announces it once.
            updater.save_state({"scheduledAt": time.time()})
            updater.tick(notices.append, prefix=tool.prefix)
            updater.tick(notices.append, prefix=tool.prefix)
            assert len(notices) == 1 and message in notices[0]
            assert "sai doctor" in notices[0]
            assert command in notices[0]
            assert "project registrations" in notices[0]
            assert state["result"]["mcpNotes"] == ["background"]
        elif mode == "deleted-cwd":
            assert "The working directory is gone." in result.stdout
            assert command in result.stdout
            assert state["result"]["mcpNotes"] == ["cwd-missing"]
        elif mode == "json":
            assert json.loads(result.stdout)["mcp"] == state["result"]["mcp"]
        else:
            assert message in result.stdout
            assert command in result.stdout
        assert "Repaired Sai MCP registrations for claude" not in result.stdout
        if not background:
            assert updater.notice(state, "0.2.0") is None
        output = result.output + json.dumps(repair) + json.dumps(updater.read_state())
        for value in (OTHER, SECRET, "private.example"):
            assert value not in output


@pytest.mark.slow
@needs_uv
def test_upgrade_keeps_the_sai_path_and_the_users_files(uv_dirs, sai_home, monkeypatch):
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": build_wheel("0.2.0")}
    (sai_home / "trader").mkdir(parents=True)
    (sai_home / "trader" / "sessions.json").write_bytes(b'{"keep": "me"}\n')
    (sai_home / "config.toml").write_bytes(b"[update]\nauto = true\n")
    with Stub(release_routes(wheels)) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        assert tool.version() == "0.1.0"
        assert tool.kind() == update.UV_TOOL_RELEASE
        link = os.readlink(tool.sai) if tool.sai.is_symlink() else None
        before = user_files(sai_home)
        # The person's directory holds a package named like ours. uv and the
        # checks run elsewhere, with an isolated Python, and never import it.
        poisoned = uv_dirs / "checkout"
        (poisoned / "sai_cli").mkdir(parents=True)
        (poisoned / "sai_cli" / "__init__.py").write_text("raise SystemExit(9)\n")
        monkeypatch.chdir(poisoned)

        outcome = tool.update()

        assert outcome.status == updater.UPDATED, outcome.message
        assert (outcome.installed, outcome.target) == ("0.2.0", "0.2.0")
        assert outcome.exit_code == 0
        assert tool.version() == "0.2.0"
        assert (os.readlink(tool.sai) if tool.sai.is_symlink() else None) == link
        assert user_files(sai_home) == before
        # The install now comes from the verified wheel in the cache. The
        # release record beside the tool says so, with the release's hash.
        kept = update.wheel_cache() / update.wheel_name("0.2.0")
        digest = hashlib.sha256(wheels["0.2.0"]).hexdigest()
        assert hashlib.sha256(kept.read_bytes()).hexdigest() == digest
        record = update.read_release_record(tool.prefix)
        assert record is not None
        assert (record.tag, record.sha256) == ("sai-cli/v0.2.0", digest)
        assert record.wheel == str(kept)
        assert update.release_record_path(tool.prefix).parent == uv_dirs / "tools"
        assert tool.kind() == update.UV_TOOL_RELEASE
        state = updater.read_state()
        assert state["latest"] == "sai-cli/v0.2.0"
        assert state["result"]["status"] == updater.UPDATED
        assert (state["result"]["from"], state["result"]["to"]) == ("0.1.0", "0.2.0")
        # The person watched this update, so no later notice repeats it.
        assert updater.notice(state, "0.2.0") is None

        again = tool.update()
        assert again.status == updater.CURRENT
        assert tool.version() == "0.2.0"


@pytest.mark.slow
@needs_uv
def test_another_sai_home_sees_the_same_release_install(uv_dirs, monkeypatch):
    """The record and the install lock belong to the tool, not the profile."""
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": build_wheel("0.2.0")}
    with Stub(release_routes(wheels)) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        assert tool.update().status == updater.UPDATED
        lock = updater.tool_lock_path(tool.prefix)

        monkeypatch.setenv("SAI_HOME", str(uv_dirs / "other-home"))
        assert tool.kind() == update.UV_TOOL_RELEASE
        assert updater.tool_lock_path(tool.prefix) == lock
        with updater.locked(lock) as held:
            assert held
            busy = tool.update(background=True)
        assert busy.status == updater.BUSY
        assert tool.update().status == updater.CURRENT


@pytest.mark.slow
@needs_uv
def test_bad_hash_keeps_the_old_version(uv_dirs):
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": build_wheel("0.2.0")}
    with Stub(release_routes(wheels, bad_hash=["0.2.0"])) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        outcome = tool.update()
    assert outcome.status == updater.FAILED
    assert outcome.exit_code == 1
    assert "does not match its SHA256SUMS entry" in outcome.message
    assert "sai 0.1.0 is unchanged" in outcome.message
    assert not outcome.rolled_back
    assert tool.version() == "0.1.0"
    assert list(update.wheel_cache().iterdir()) == []
    assert updater.read_state()["result"]["status"] == updater.FAILED


@pytest.mark.slow
@needs_uv
def test_broken_wheel_keeps_the_old_version(uv_dirs):
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": b"this is not a wheel"}
    with Stub(release_routes(wheels)) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        outcome = tool.update()
    assert outcome.status == updater.FAILED
    assert "failed its trial install: uv pip failed" in outcome.message
    assert tool.version() == "0.1.0"
    assert list(update.wheel_cache().glob("*.whl")) == []


@pytest.mark.slow
@needs_uv
def test_a_release_that_cannot_import_never_replaces_the_install(uv_dirs):
    """The new wheel runs in a throwaway environment first. The installed
    version is withdrawn, so no rollback could be had; nothing is replaced."""
    wheels = {
        "0.1.0": build_wheel("0.1.0", "ORIGINAL = True\n"),
        "0.2.0": build_wheel("0.2.0", BROKEN_IMPORT),
    }
    with Stub(release_routes(wheels, latest=["0.2.0"])) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        for name in (update.wheel_name("0.1.0"), "SHA256SUMS"):
            stub.routes.pop(f"{downloads('0.1.0')}/{name}")
        environment = tool.prefix.stat().st_ino
        outcome = tool.update()
    assert outcome.status == updater.FAILED
    assert not outcome.rolled_back
    assert "failed its trial install" in outcome.message
    assert "the package does not import" in outcome.message
    assert "sai 0.1.0 is unchanged" in outcome.message
    assert tool.version() == "0.1.0"
    assert "ORIGINAL" in tool.server_source()
    assert tool.prefix.stat().st_ino == environment
    assert list(update.wheel_cache().glob("*.whl")) == []


@pytest.mark.slow
@needs_uv
def test_a_release_that_breaks_only_once_installed_rolls_back(uv_dirs):
    wheels = {
        "0.1.0": build_wheel("0.1.0"),
        "0.2.0": build_wheel("0.2.0"),
        "0.3.0": build_wheel("0.3.0", BROKEN_WHEN_INSTALLED),
    }
    with Stub(release_routes(wheels, latest=["0.3.0"])) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")

        # From a release URL install: the copy of the environment is swapped
        # back, so the install is exactly what it was.
        first = tool.update()
        assert first.status == updater.FAILED
        assert first.rolled_back, first.message
        assert "the package does not import" in first.message
        assert "Rolled back to sai 0.1.0" in first.message
        assert tool.version() == "0.1.0"
        assert tool.kind() == update.UV_TOOL_RELEASE

        # From an install sai update made, with the release gone from the
        # server.
        assert tool.update(to="0.2.0").status == updater.UPDATED
        stub.routes.pop(f"{downloads('0.2.0')}/{update.wheel_name('0.2.0')}")
        second = tool.update()
        assert second.status == updater.FAILED
        assert second.rolled_back, second.message
        assert tool.version() == "0.2.0"
        assert tool.kind() == update.UV_TOOL_RELEASE
        kept = sorted(path.name for path in update.wheel_cache().glob("*.whl"))
        # A failed update prunes nothing: 0.1.0 stays from the update before.
        assert kept == [update.wheel_name("0.1.0"), update.wheel_name("0.2.0")]
    result = updater.read_state()["result"]
    assert (result["status"], result["to"], result["rolledBack"]) == (
        updater.FAILED,
        "0.3.0",
        True,
    )


def build_dependency(name: str, version: str = "1.0.0") -> bytes:
    """A wheel of an empty package NAME, for sai-cli to depend on."""
    info = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": "",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
        f"{info}/RECORD": "",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return buffer.getvalue()


@pytest.mark.slow
@needs_uv
def test_rollback_needs_no_package_source_for_the_old_dependencies(
    uv_dirs, monkeypatch
):
    """The installed version depends on a package that is gone by the time
    the update fails. Reinstalling the old wheel would have to resolve it
    again; swapping the environment copy back does not."""
    links = uv_dirs / "links"
    links.mkdir()
    dependency = links / "saidep-1.0.0-py3-none-any.whl"
    dependency.write_bytes(build_dependency("saidep"))
    monkeypatch.setenv("UV_FIND_LINKS", str(links))
    monkeypatch.setenv("UV_NO_INDEX", "1")
    wheels = {
        "0.1.0": build_wheel("0.1.0", "import saidep\n", requires=["saidep"]),
        "0.2.0": build_wheel("0.2.0", BROKEN_WHEN_INSTALLED),
    }
    log = uv_dirs / "uv.log"
    shims = uv_dirs / "shims"
    shims.mkdir()
    shim = shims / "uv"
    shim.write_text(UV_SHIM.format(python=sys.executable, log=str(log), uv=REAL_UV))
    shim.chmod(0o755)
    with Stub(release_routes(wheels, latest=["0.2.0"])) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        assert "import saidep" in tool.server_source()
        # The dependency is withdrawn, and uv's cache of it is gone too.
        dependency.unlink()
        shutil.rmtree(uv_dirs / "uv-cache")
        monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
        outcome = tool.update()
    assert outcome.status == updater.FAILED
    assert outcome.rolled_back, outcome.message
    assert "Rolled back to sai 0.1.0" in outcome.message
    assert tool.version() == "0.1.0"
    python = tool.prefix / "bin" / "python"
    done = subprocess.run(
        [str(python), "-I", "-c", "import sai_cli.mcp_server.server, saidep"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    # uv replaced the install once and was not asked to put it back.
    installs = [line for line in log.read_text().splitlines() if "tool install" in line]
    assert len(installs) == 1


@pytest.mark.slow
@needs_uv
def test_the_spare_wheel_restores_when_the_copy_cannot_be_swapped_back(
    uv_dirs, monkeypatch
):
    wheels = {
        "0.1.0": build_wheel("0.1.0"),
        "0.2.0": build_wheel("0.2.0", BROKEN_WHEN_INSTALLED),
    }
    monkeypatch.setattr(
        updater, "swap_back", lambda prefix, snapshot, launcher: "disk error"
    )
    with Stub(release_routes(wheels, latest=["0.2.0"])) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        outcome = tool.update()
    assert outcome.status == updater.FAILED
    assert outcome.rolled_back, outcome.message
    assert tool.version() == "0.1.0"
    # Reinstalled from the wheel that was verified before the update began.
    record = update.read_release_record(tool.prefix)
    assert record is not None
    assert record.sha256 == hashlib.sha256(wheels["0.1.0"]).hexdigest()
    assert tool.kind() == update.UV_TOOL_RELEASE


@pytest.mark.slow
@needs_uv
def test_with_no_previous_wheel_the_environment_copy_restores_the_install(uv_dirs):
    """The installed release is withdrawn, so no rollback wheel can be had.
    The new release passes its trial but breaks once installed. The copy of
    the old environment is swapped back and the old version works again."""
    wheels = {
        "0.1.0": build_wheel("0.1.0", "ORIGINAL = True\n"),
        "0.2.0": build_wheel("0.2.0", BROKEN_WHEN_INSTALLED),
    }
    with Stub(release_routes(wheels, latest=["0.2.0"])) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        for name in (update.wheel_name("0.1.0"), "SHA256SUMS"):
            stub.routes.pop(f"{downloads('0.1.0')}/{name}")
        link = os.path.realpath(tool.sai)
        outcome = tool.update()
    assert outcome.status == updater.FAILED
    assert outcome.rolled_back, outcome.message
    assert "the package does not import" in outcome.message
    assert "Rolled back to sai 0.1.0" in outcome.message
    assert tool.version() == "0.1.0"
    assert "ORIGINAL" in tool.server_source()
    assert os.path.realpath(tool.sai) == link
    # The old install is whole: it still counts as the same release install.
    assert tool.kind() == update.UV_TOOL_RELEASE
    assert sorted(path.name for path in (uv_dirs / "tools").iterdir()) == [
        ".gitignore",
        ".lock",
        ".sai-cli-update.lock",
        "sai-cli",
    ]
    assert list(update.wheel_cache().glob("*.whl")) == []


@pytest.mark.slow
@needs_uv
def test_another_install_owning_the_shared_launcher_stops_the_update(
    uv_dirs, monkeypatch
):
    """Two uv tool directories share one executable directory. B installed
    last and owns `sai`; A's receipt still names it. Updating A must not
    take B's launcher."""
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": build_wheel("0.2.0")}
    with Stub(release_routes(wheels)) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        monkeypatch.setenv("UV_TOOL_DIR", str(uv_dirs / "tools-b"))
        name = update.wheel_name("0.1.0")
        assert REAL_UV is not None
        subprocess.run(
            [
                REAL_UV,
                "tool",
                "install",
                "--force",
                f"{stub.url}{downloads('0.1.0')}/{name}",
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        owner = os.path.realpath(tool.sai)
        assert str(uv_dirs / "tools-b") in owner
        monkeypatch.setenv("UV_TOOL_DIR", str(uv_dirs / "tools"))
        outcome = tool.update()
    assert outcome.status == updater.UNSUPPORTED
    assert outcome.exit_code == 3
    assert "runs another install" in outcome.message
    assert os.path.realpath(tool.sai) == owner
    assert tool.version() == "0.1.0"
    assert update.installed_version(tool.prefix) == "0.1.0"


@pytest.mark.slow
@needs_uv
def test_the_wheel_cache_stays_inside_sai_home(uv_dirs, sai_home):
    """A symlinked cache is refused, and pruning removes only this updater's
    own wheels."""
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": build_wheel("0.2.0")}
    shared = uv_dirs / "shared-wheels"
    shared.mkdir()
    (shared / "sai_cli-9.9.9-py3-none-any.whl").write_bytes(b"someone else's")
    sai_home.mkdir()
    (sai_home / "cache").symlink_to(shared)
    with Stub(release_routes(wheels)) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        refused = tool.update()
        assert refused.status == updater.FAILED
        assert "is a symlink" in refused.message
        assert tool.version() == "0.1.0"
        assert sorted(path.name for path in shared.iterdir()) == [
            "sai_cli-9.9.9-py3-none-any.whl"
        ]

        (sai_home / "cache").unlink()
        cache = update.wheel_cache()
        cache.mkdir(parents=True)
        for name in (
            "sai_cli-0.0.1-py3-none-any.whl",
            "sai_cli-extra-1.0.whl",
            "other.whl",
        ):
            (cache / name).write_bytes(b"x")
        (cache / "sai_cli-0.0.2-py3-none-any.whl").symlink_to(
            shared / "sai_cli-9.9.9-py3-none-any.whl"
        )
        assert tool.update().status == updater.UPDATED
        assert sorted(path.name for path in cache.iterdir()) == [
            "other.whl",
            "sai_cli-0.0.2-py3-none-any.whl",
            "sai_cli-0.1.0-py3-none-any.whl",
            "sai_cli-0.2.0-py3-none-any.whl",
            "sai_cli-extra-1.0.whl",
        ]
        assert (
            shared / "sai_cli-9.9.9-py3-none-any.whl"
        ).read_bytes() == b"someone else's"


@pytest.mark.slow
@needs_uv
def test_rollback_does_not_trust_a_changed_wheel_in_the_cache(uv_dirs):
    """A kept wheel whose bytes no longer match the release record is not
    installed; the release is downloaded and verified again."""
    wheels = {
        "0.1.0": build_wheel("0.1.0"),
        "0.2.0": build_wheel("0.2.0", "GENUINE = True\n"),
        "0.3.0": build_wheel("0.3.0", BROKEN_WHEN_INSTALLED),
    }
    with Stub(release_routes(wheels, latest=["0.2.0"])) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        assert tool.update().status == updater.UPDATED
        kept = update.wheel_cache() / update.wheel_name("0.2.0")
        kept.write_bytes(build_wheel("0.2.0", "TAMPERED = True\n"))

        stub.routes[API_PATH] = listing("sai-cli/v0.3.0")
        outcome = tool.update()

        assert outcome.status == updater.FAILED
        assert outcome.rolled_back, outcome.message
        assert tool.version() == "0.2.0"
        assert "GENUINE" in tool.server_source()
        digest = hashlib.sha256(wheels["0.2.0"]).hexdigest()
        assert hashlib.sha256(kept.read_bytes()).hexdigest() == digest
        assert tool.kind() == update.UV_TOOL_RELEASE


@pytest.mark.slow
@needs_uv
@pytest.mark.parametrize("variable", ["UV_TOOL_DIR", "UV_TOOL_BIN_DIR"])
def test_uv_pointed_elsewhere_changes_nothing(uv_dirs, monkeypatch, variable):
    """uv installs wherever its settings say. If that is not this install,
    the update stops before touching either place."""
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": build_wheel("0.2.0")}
    elsewhere = uv_dirs / "elsewhere"
    with Stub(release_routes(wheels)) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        monkeypatch.setenv(variable, str(elsewhere))
        outcome = tool.update()
    assert outcome.status == updater.UNSUPPORTED
    assert outcome.exit_code == EXIT_UNSUPPORTED == 3
    assert "stopped before changing anything" in outcome.message
    assert str(elsewhere) in outcome.message
    assert tool.version() == "0.1.0"
    assert not elsewhere.exists()


@pytest.mark.slow
@needs_uv
def test_never_downgrades_unless_told_to(uv_dirs):
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": build_wheel("0.2.0")}
    with Stub(release_routes(wheels, latest=["0.1.0"])) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.2.0")
        kept = tool.update()
        assert kept.status == updater.CURRENT
        assert "newer than the latest release 0.1.0" in kept.message
        assert tool.version() == "0.2.0"

        missing = tool.update(to="0.9.0")
        assert missing.status == updater.FAILED
        assert "the release has no file" in missing.message
        assert tool.version() == "0.2.0"

        back = tool.update(to="0.1.0")
        assert back.status == updater.UPDATED, back.message
        assert tool.version() == "0.1.0"


WORKER = """
import json, sys
from pathlib import Path
from sai_cli.core import updater
base, prefix = sys.argv[1:3]
options = updater.Options(api_base=base, web_base=base, prefix=Path(prefix))
print(json.dumps(updater.update_now(options).to_json()))
"""

UV_SHIM = """#!{python}
import os, sys
with open({log!r}, "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
os.execv({uv!r}, [{uv!r}, *sys.argv[1:]])
"""


@pytest.mark.slow
@needs_uv
def test_two_workers_install_once(uv_dirs, monkeypatch):
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": build_wheel("0.2.0")}
    log = uv_dirs / "uv.log"
    shims = uv_dirs / "shims"
    shims.mkdir()
    shim = shims / "uv"
    shim.write_text(UV_SHIM.format(python=sys.executable, log=str(log), uv=REAL_UV))
    shim.chmod(0o755)
    with Stub(release_routes(wheels)) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
        command = [sys.executable, "-c", WORKER, stub.url, str(tool.prefix)]
        workers = [
            subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        outcomes = [
            json.loads(worker.communicate(timeout=180)[0]) for worker in workers
        ]
    assert sorted(outcome["status"] for outcome in outcomes) == [
        updater.CURRENT,
        updater.UPDATED,
    ]
    installs = [line for line in log.read_text().splitlines() if "tool install" in line]
    assert len(installs) == 1
    assert tool.version() == "0.2.0"


# Scripted runner: the commands an update runs, without uv

COMMIT_A, COMMIT_B = "a" * 40, "b" * 40
GIT_URL = "https://github.com/SaiDotFun/open-sai"


def git_origin(commit: str, tag: str) -> dict:
    return {
        "url": GIT_URL,
        "vcs_info": {"vcs": "git", "commit_id": commit, "requested_revision": tag},
    }


GIT = git_origin(COMMIT_A, "sai-cli/v0.1.0")


def official_prefix(root: Path, direct: dict | None = None) -> Path:
    """An official uv tool install under ROOT/tools whose `sai` in ROOT/bin
    links into the environment, as uv lays it out and records it."""
    prefix = make_prefix(root / "tools" / "sai-cli", direct or GIT, receipt=True)
    inside = prefix / "bin" / "sai"
    inside.parent.mkdir()
    inside.write_text("#!/bin/sh\n")
    launcher = root / "bin" / "sai"
    launcher.parent.mkdir(exist_ok=True)
    launcher.symlink_to(inside)
    with (prefix / "uv-receipt.toml").open("a") as receipt:
        receipt.write(
            f'entrypoints = [{{ name = "sai", install-path = "{launcher}" }}]\n'
        )
    return prefix


def set_install(prefix: Path, version: str, direct: dict) -> None:
    """Rewrite the install at PREFIX as another update would leave it."""
    dist = next(prefix.glob("lib/*/site-packages/sai_cli-*.dist-info"))
    (dist / "METADATA").write_text(f"Name: sai-cli\nVersion: {version}\n")
    (dist / "direct_url.json").write_text(json.dumps(direct))


class Script:
    """A runner that records commands and plays uv, the trial environment,
    and the installed sai.

    COMMITS maps a Git commit to the version it installs. FAIL_TRIAL fails
    the trial `uv pip install`; FAIL_INSTALL fails `uv tool install`. A
    version in BROKEN fails `sai --version` everywhere; one in
    BROKEN_INSTALLED only as the installed tool. INTERRUPT raises Ctrl-C
    from the first installed `sai --version` after an install.
    """

    def __init__(
        self,
        root: Path,
        version: str,
        fail_trial: bool = False,
        fail_install: bool = False,
        commits: dict[str, str] | None = None,
        broken: Sequence[str] = (),
        broken_installed: Sequence[str] = (),
        interrupt: bool = False,
    ) -> None:
        self.root = root
        self.launcher = str(root / "bin" / "sai")
        self.prefix = root / "tools" / "sai-cli"
        self.start = version
        self.staged = ""
        self.fail_trial = fail_trial
        self.fail_install = fail_install
        self.commits = commits or {COMMIT_A: "0.1.0"}
        self.broken = broken
        self.broken_installed = broken_installed
        self.interrupt = interrupt
        self.commands: list[list[str]] = []

    @property
    def version(self) -> str:
        """The installed version, read from the environment on disk, so a
        swapped-back copy of the environment reports the old version."""
        return update.installed_version(self.prefix) or self.start

    def named(self, spec: str) -> str:
        """The version a requirement installs."""
        reference = spec.rsplit("@", 1)[-1]
        if reference in self.commits:
            return self.commits[reference]
        return re.findall(r"\d+\.\d+\.\d+", spec)[-1]

    def __call__(self, command: Sequence[str], timeout: float) -> tuple[int, str]:
        self.commands.append(list(command))
        if command[1:3] == ["tool", "dir"]:
            name = "bin" if command[-1] == "--bin" else "tools"
            return 0, f"{self.root / name}\n"
        if command[1:2] == ["venv"]:
            return 0, ""
        if command[1:3] == ["pip", "install"]:
            if self.fail_trial:
                return 2, "error: no such revision"
            self.staged = self.named(command[-1])
            return 0, "Installed 1 package"
        if command[1:3] == ["tool", "install"]:
            if self.fail_install:
                return 2, "error: disk full"
            dist = next(self.prefix.glob("lib/*/site-packages/sai_cli-*.dist-info"))
            version = self.named(command[-1])
            (dist / "METADATA").write_text(f"Name: sai-cli\nVersion: {version}\n")
            return 0, "Installed 1 executable: sai"
        if command[-1] == "--version":
            installed = command[0] == self.launcher
            version = self.version if installed else self.staged
            if installed and self.interrupt:
                self.interrupt = False
                raise KeyboardInterrupt
            if version in self.broken or (
                installed and version in self.broken_installed
            ):
                return 1, "Traceback: broken"
            return 0, f"sai, version {version}\n"
        return 0, ""

    @property
    def installs(self) -> list[str]:
        return [c[-1] for c in self.commands if c[1:3] == ["tool", "install"]]

    @property
    def trials(self) -> list[str]:
        return [c[-1] for c in self.commands if c[1:3] == ["pip", "install"]]


def scripted(tmp_path: Path, stub: Stub, script: Script, **changes) -> updater.Options:
    prefix = tmp_path / "tools" / "sai-cli"
    if not prefix.exists():
        official_prefix(tmp_path)
    return updater.Options(
        api_base=stub.url,
        web_base=stub.url,
        prefix=prefix,
        runner=script,
        uv="/fake/uv",
        **changes,
    )


def test_source_builds_the_exact_release_tag_from_git(tmp_path, sai_home):
    script = Script(tmp_path, "0.1.0")
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        outcome = updater.update_now(scripted(tmp_path, stub, script, source=True))
    assert outcome.status == updater.UPDATED, outcome.message
    # refs/tags/…: a branch named like the tag cannot shadow it.
    spec = f"git+{GIT_URL}@refs/tags/sai-cli/v0.2.0"
    assert script.installs == [spec]
    kinds = [" ".join(c[1:3]) for c in script.commands if c[0] == "/fake/uv"]
    # uv's destination is checked first, the release runs in a trial
    # environment next, and only then is the install replaced.
    assert kinds == [
        "tool dir",
        "tool dir",
        "venv --python",
        "pip install",
        "tool install",
    ]
    assert script.trials == [spec]
    # The check after the install runs the registered launcher, the one on
    # PATH, and imports with an isolated interpreter.
    assert [str(tmp_path / "bin" / "sai"), "--version"] in script.commands
    last = script.commands[-1]
    assert last[1:] == ["-I", "-c", "import sai_cli.sai_cli, sai_cli.mcp_server.server"]
    assert last[0] == str(tmp_path / "tools" / "sai-cli" / "bin" / "python")
    assert stub.requests == [("GET", API_PATH)]
    assert not update.wheel_cache().exists() or not list(update.wheel_cache().iterdir())


def test_a_release_that_fails_its_trial_is_never_installed(tmp_path, sai_home):
    for script in (
        Script(tmp_path, "0.1.0", fail_trial=True),
        Script(tmp_path, "0.1.0", broken=["0.2.0"]),
    ):
        with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
            outcome = updater.update_now(scripted(tmp_path, stub, script, source=True))
        assert outcome.status == updater.FAILED
        assert "failed its trial install" in outcome.message
        assert "sai 0.1.0 is unchanged" in outcome.message
        assert script.installs == []
        assert len(script.trials) == 1


def test_a_failed_install_keeps_the_old_version(tmp_path, sai_home):
    script = Script(tmp_path, "0.1.0", fail_install=True)
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        outcome = updater.update_now(scripted(tmp_path, stub, script, source=True))
    assert outcome.status == updater.FAILED
    assert "disk full" in outcome.message
    assert "sai 0.1.0 is unchanged" in outcome.message
    # One install attempt, then a check that the old version still runs.
    assert len(script.installs) == 1


def test_without_anything_to_roll_back_to_the_install_is_not_touched(
    tmp_path, sai_home, monkeypatch
):
    """No verified wheel and no copy of the environment: stop with exit 1."""
    monkeypatch.setattr(updater, "snapshot_environment", lambda prefix: None)
    script = Script(tmp_path, "0.1.0")
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        outcome = updater.update_now(scripted(tmp_path, stub, script, source=True))
    assert outcome.status == updater.FAILED
    assert outcome.exit_code == 1
    assert "nothing to roll back to could be prepared" in outcome.message
    assert "sai 0.1.0 is unchanged" in outcome.message
    assert len(script.trials) == 1
    assert script.installs == []


def test_two_tools_sharing_one_sai_home_do_not_prune_each_other(tmp_path, sai_home):
    """Tool A is mid-update and holds the cache lock, with its rollback
    wheel in the shared cache. Tool B's update, under its own tool lock,
    must wait and must not delete that wheel."""
    cache = updater.private_cache()
    rollback = cache / update.wheel_name("0.0.9")
    rollback.write_bytes(b"tool A's rollback wheel")
    wheels = {"0.2.0": build_wheel("0.2.0")}
    script = Script(tmp_path, "0.1.0")
    with Stub(release_routes(wheels)) as stub:
        options = scripted(tmp_path, stub, script, background=True)
        with updater.locked(updater.cache_lock_path()) as held:
            assert held
            busy = updater.update_now(options)
            assert busy.status == updater.BUSY
            assert script.trials == script.installs == []
            assert rollback.read_bytes() == b"tool A's rollback wheel"
        # A failed update never prunes; it removes only what it downloaded.
        failing = Script(tmp_path, "0.1.0", fail_trial=True)
        failed = updater.update_now(replace(options, runner=failing))
    assert failed.status == updater.FAILED
    assert sorted(path.name for path in cache.iterdir()) == [rollback.name]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes and ownership")
def test_the_wheel_cache_must_be_private_and_ours(tmp_path, sai_home, monkeypatch):
    """A cache other users could write to would let them swap a verified
    wheel. Ours is tightened; someone else's is refused."""
    wheels = sai_home / "cache" / "wheels"
    wheels.mkdir(parents=True)
    for directory in (sai_home, sai_home / "cache", wheels):
        directory.chmod(0o777)
    assert updater.private_cache() == wheels
    for directory in (sai_home, sai_home / "cache", wheels):
        assert directory.stat().st_mode & 0o777 == 0o700, directory

    other = os.getuid() + 1
    monkeypatch.setattr(updater.os, "getuid", lambda: other)
    with pytest.raises(updater.UpdateFailed, match="belongs to another user"):
        updater.private_cache()
    script = Script(tmp_path, "0.1.0")
    with Stub(release_routes({"0.2.0": build_wheel("0.2.0")})) as stub:
        outcome = updater.update_now(scripted(tmp_path, stub, script))
    assert outcome.status == updater.FAILED
    assert "belongs to another user" in outcome.message
    assert script.trials == script.installs == []


def test_a_wheel_changed_after_verification_is_not_given_to_uv(tmp_path, sai_home):
    """The kept wheel is hashed again right before uv opens it."""
    wheels = {"0.2.0": build_wheel("0.2.0")}
    script = Script(tmp_path, "0.1.0")
    kept = update.wheel_cache() / update.wheel_name("0.2.0")
    real = script.__call__

    def swapping(command: Sequence[str], timeout: float) -> tuple[int, str]:
        result = real(command, timeout)
        if command[1:3] == ["pip", "install"]:
            kept.write_bytes(b"swapped after the trial")
        return result

    with Stub(release_routes(wheels)) as stub:
        options = replace(scripted(tmp_path, stub, script), runner=swapping)
        outcome = updater.update_now(options)
    assert outcome.status == updater.FAILED
    assert "changed after it was verified" in outcome.message
    assert "sai 0.1.0 is unchanged" in outcome.message
    assert script.installs == []


def test_a_relative_sai_home_gives_uv_an_absolute_wheel(tmp_path, monkeypatch):
    """uv runs in another directory, so every path it gets is absolute."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SAI_HOME", "relative-home")
    wheels = {"0.2.0": build_wheel("0.2.0")}
    script = Script(tmp_path, "0.1.0")
    with Stub(release_routes(wheels)) as stub:
        outcome = updater.update_now(scripted(tmp_path, stub, script))
    cache = tmp_path / "relative-home" / "cache" / "wheels"
    wheel = str(cache / update.wheel_name("0.2.0"))
    assert script.trials == [wheel]
    # The record needs a real install; the scripted one has no RECORD file.
    assert "could not record the release" in outcome.message
    assert script.installs[0] == wheel
    assert cache.stat().st_mode & 0o777 == 0o700
    for command in script.commands:
        assert all(os.path.isabs(part) for part in command[:1])


def test_uv_installing_elsewhere_stops_with_exit_3(tmp_path, sai_home):
    """The scripted uv reports another tool directory than the install's."""
    official_prefix(tmp_path)
    script = Script(tmp_path / "another-uv-home", "0.1.0")
    script.launcher = str(tmp_path / "bin" / "sai")
    script.prefix = tmp_path / "tools" / "sai-cli"
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        outcome = updater.update_now(scripted(tmp_path, stub, script, source=True))
    assert outcome.status == updater.UNSUPPORTED
    assert outcome.exit_code == 3
    assert "uv installs tools into" in outcome.message
    assert script.installs == []


def test_ctrl_c_during_verification_still_leaves_a_working_install(tmp_path, sai_home):
    """uv already replaced the old version with one that does not run when
    Ctrl-C arrives; the old version is put back before the interrupt goes
    on."""
    script = Script(tmp_path, "0.1.0", broken_installed=["0.2.0"], interrupt=True)
    with (
        Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub,
        pytest.raises(KeyboardInterrupt),
    ):
        updater.update_now(scripted(tmp_path, stub, script, source=True))
    # A Git install goes back to the copy of its environment, offline.
    assert script.installs == [f"git+{GIT_URL}@refs/tags/sai-cli/v0.2.0"]
    assert script.version == "0.1.0"
    result = updater.read_state()["result"]
    assert (result["status"], result["rolledBack"]) == (updater.FAILED, True)
    assert "interrupted" in result["error"]
    leftovers = [p.name for p in (tmp_path / "tools").iterdir() if "rollback" in p.name]
    assert leftovers == []


def test_a_waiting_update_reads_the_install_after_it_gets_the_lock(
    tmp_path, sai_home, monkeypatch
):
    """Update B waits while update A moves the install to another version
    and origin. When B's own target fails, B must put back what A left, not
    what was installed when B started."""
    prefix = official_prefix(tmp_path)
    commits = {COMMIT_A: "0.1.0", COMMIT_B: "0.2.0"}
    script = Script(tmp_path, "0.1.0", commits=commits, broken_installed=["0.3.0"])
    monkeypatch.setattr(updater, "LOCK_WAIT", 30.0)
    outcomes: list[updater.Outcome] = []
    with Stub({API_PATH: listing("sai-cli/v0.3.0")}) as stub:
        options = scripted(tmp_path, stub, script, source=True)
        with updater.locked(updater.tool_lock_path(prefix)) as held:
            assert held
            waiter = threading.Thread(
                target=lambda: outcomes.append(updater.update_now(options))
            )
            waiter.start()
            time.sleep(0.5)
            # Update A finishes: version 0.2.0 from commit B.
            set_install(prefix, "0.2.0", git_origin(COMMIT_B, "sai-cli/v0.2.0"))
        waiter.join(60)
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status == updater.FAILED
    assert outcome.rolled_back, outcome.message
    assert "Rolled back to sai 0.2.0" in outcome.message
    assert script.installs == [f"git+{GIT_URL}@refs/tags/sai-cli/v0.3.0"]
    # The copy was taken after the lock, so it holds what update A left.
    assert script.version == "0.2.0"
    origin = json.loads(
        next(
            prefix.glob("lib/*/site-packages/sai_cli-*.dist-info/direct_url.json")
        ).read_text()
    )
    assert origin["vcs_info"]["commit_id"] == COMMIT_B


def test_no_release_and_bad_versions_install_nothing(tmp_path, sai_home):
    script = Script(tmp_path, "0.1.0")
    with Stub({API_PATH: listing("sai-rs/v1.0.0")}) as stub:
        none = updater.update_now(scripted(tmp_path, stub, script))
        bad = updater.update_now(scripted(tmp_path, stub, script, to="latest"))
    assert none.status == updater.NO_RELEASE
    assert none.exit_code == 0
    assert bad.status == updater.FAILED
    assert "is not a version like 0.2.0" in bad.message
    assert script.commands == []
    assert updater.read_state()["latest"] is None


def test_a_failed_check_is_reported_and_installs_nothing(tmp_path, sai_home):
    script = Script(tmp_path, "0.1.0")
    with Stub({API_PATH: (200, {}, b"garbage")}) as stub:
        outcome = updater.update_now(scripted(tmp_path, stub, script))
    assert outcome.status == updater.FAILED
    assert "update check failed" in outcome.message
    assert script.commands == []
    # A failed lookup is not an update attempt; doctor has nothing to warn of.
    assert "result" not in updater.read_state()


def test_a_download_cannot_outlast_its_wall_clock_limit(tmp_path, sai_home):
    """A server that keeps sending a byte at a time, headers included, is
    cut off at the deadline, so the tool lock is not held for long."""
    wheels = {"0.2.0": build_wheel("0.2.0")}
    wheel_path = f"{downloads('0.2.0')}/{update.wheel_name('0.2.0')}"
    script = Script(tmp_path, "0.1.0")
    with Stub(
        release_routes(wheels), drip=0.02, drip_paths=frozenset({wheel_path})
    ) as stub:
        options = scripted(tmp_path, stub, script, download_timeout=0.5)
        started = time.monotonic()
        outcome = updater.update_now(options)
        elapsed = time.monotonic() - started
    assert outcome.status == updater.FAILED
    assert "timed out downloading" in outcome.message
    assert "sai 0.1.0 is unchanged" in outcome.message
    assert script.installs == []
    # The drip would take over ten seconds; generous for a loaded machine.
    assert elapsed < 8


@pytest.mark.parametrize(
    ("name", "direct", "receipt", "text"),
    [
        (
            "checkout",
            {"url": "file:///src", "dir_info": {"editable": True}},
            False,
            "uv sync",
        ),
        (
            "fork",
            {**GIT, "url": "https://github.com/someone/open-sai"},
            True,
            "install.py",
        ),
        ("pip", None, False, "install.py"),
    ],
)
def test_unofficial_installs_are_not_updated(
    tmp_path, sai_home, name, direct, receipt, text
):
    prefix = make_prefix(tmp_path / name, direct, receipt=receipt)
    script = Script(tmp_path, "0.1.0")
    outcome = updater.update_now(updater.Options(prefix=prefix, runner=script))
    assert outcome.status == updater.UNSUPPORTED
    assert outcome.exit_code == EXIT_UNSUPPORTED == 3
    assert text in outcome.message
    assert script.commands == []
    assert not sai_home.exists()


def test_sai_update_from_a_checkout_exits_3_with_a_note(sai_home):
    result = CliRunner().invoke(main, ["update"])
    assert result.exit_code == EXIT_UNSUPPORTED
    assert result.stdout == ""
    assert "only updates an official uv tool install" in result.stderr
    data = CliRunner().invoke(main, ["--json", "update"])
    assert data.exit_code == EXIT_UNSUPPORTED
    error = json.loads(data.stdout)["error"]
    assert error["code"] == "INTERNAL_ERROR" and error["retryable"] is False
    assert error["hint"] == update.INSTALL_COMMAND


def test_check_takes_no_install_options(sai_home):
    result = CliRunner().invoke(main, ["update", "--check", "--to", "0.2.0"])
    assert result.exit_code == 2
    assert "--check takes no other update option" in result.stderr


# Background run


def test_the_opt_out_is_read_right_before_installing(tmp_path, sai_home):
    """update.auto is asked after the release is known, so turning it off
    while the background run looks up the release still holds."""
    script = Script(tmp_path, "0.1.0")
    asked: list[int] = []
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:

        def allowed() -> bool:
            asked.append(len(stub.requests))
            return False

        options = scripted(tmp_path, stub, script, allowed=allowed, background=True)
        outcome = updater.update_now(options)
    assert outcome.status == updater.AVAILABLE
    assert asked == [1]
    assert script.commands == []
    state = updater.read_state()
    result = state["result"]
    assert (result["status"], result["to"]) == (updater.AVAILABLE, "0.2.0")
    assert updater.notice(state, "0.1.0") is not None
    assert state["startedAt"] > 0


def test_an_opt_out_during_the_download_still_stops_the_install(tmp_path, sai_home):
    """update.auto is asked again right before uv replaces the install, after
    the download and the trial. Turned off in that window, nothing is
    replaced."""
    wheels = {"0.2.0": build_wheel("0.2.0")}
    script = Script(tmp_path, "0.1.0")
    asked: list[int] = []
    with Stub(release_routes(wheels)) as stub:

        def allowed() -> bool:
            asked.append(len(stub.requests))
            return len(asked) == 1

        options = scripted(tmp_path, stub, script, allowed=allowed, background=True)
        outcome = updater.update_now(options)
    assert outcome.status == updater.AVAILABLE
    # Once when the release was known, once after both files were fetched.
    assert asked[0] == 1
    assert asked[1] >= 3
    assert len(script.trials) == 1
    assert script.installs == []
    result = updater.read_state()["result"]
    assert (result["status"], result["to"]) == (updater.AVAILABLE, "0.2.0")
    assert list(update.wheel_cache().glob("*.whl")) == []


def test_background_run_skips_when_another_update_holds_the_lock(tmp_path, sai_home):
    script = Script(tmp_path, "0.1.0")
    prefix = official_prefix(tmp_path)
    with (
        Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub,
        updater.locked(updater.tool_lock_path(prefix)) as held,
    ):
        assert held
        options = scripted(tmp_path, stub, script, background=True)
        outcome = updater.update_now(options)
    assert outcome.status == updater.BUSY
    assert outcome.exit_code == 0
    assert stub.requests == []
    assert script.commands == []
    # It never began work, so the next tick may retry it soon.
    assert "startedAt" not in updater.read_state()


def test_background_run_does_not_retry_a_version_that_failed(tmp_path, sai_home):
    script = Script(tmp_path, "0.1.0", fail_trial=True)
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        options = scripted(tmp_path, stub, script, source=True, background=True)
        first = updater.update_now(options)
        attempts = len(script.trials)
        second = updater.update_now(options)
        assert len(script.trials) == attempts == 1
        manual = updater.update_now(replace(options, background=False))
    assert first.status == second.status == updater.FAILED
    assert "already failed" in second.message
    assert "no such revision" in manual.message
    assert len(script.trials) == attempts + 1


def test_lock_waits_for_the_holder(sai_home):
    released = threading.Event()

    def hold() -> None:
        with updater.locked() as held:
            assert held
            released.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        time.sleep(0.1)
        with updater.locked() as busy:
            assert not busy
        threading.Timer(0.2, released.set).start()
        with updater.locked(wait=5) as held:
            assert held
    finally:
        released.set()
        holder.join()


def test_expected_hash_reads_sha256sums():
    digest = "ab" * 32
    sums = (
        f"{'cd' * 32}  sai_cli-0.2.0.tar.gz\n{digest} *sai_cli-0.2.0-py3-none-any.whl\n"
    )
    assert updater.expected_hash(sums, "sai_cli-0.2.0-py3-none-any.whl") == digest
    assert updater.expected_hash(sums, "other.whl") is None
    assert updater.expected_hash("short  other.whl\n", "other.whl") is None


# Notices and the daily tick


def state_with(status: str, to: str = "0.2.0", **extra) -> dict:
    return {"schema": 1, "result": {"status": status, "to": to}, **extra}


def test_notice_names_each_state_once():
    key, text = updater.notice(state_with(updater.UPDATED), "0.2.0") or ("", "")
    assert key == "updated:0.2.0"
    assert "updated itself to 0.2.0" in text
    assert updater.notice(state_with(updater.UPDATED, notified=key), "0.2.0") is None

    available = updater.notice(state_with(updater.AVAILABLE), "0.1.0")
    assert available is not None and "sai 0.2.0 is available" in available[1]
    failed = updater.notice(state_with(updater.FAILED), "0.1.0")
    assert failed is not None and "could not update itself to 0.2.0" in failed[1]
    # A newer result is a new state even after an older one was announced.
    assert updater.notice(state_with(updater.UPDATED, "0.3.0", notified=key), "0.3.0")


def test_notice_is_silent_when_there_is_nothing_to_say():
    assert updater.notice({}, "0.1.0") is None
    assert updater.notice({"result": "junk"}, "0.1.0") is None
    assert updater.notice(state_with(updater.CURRENT), "0.2.0") is None
    assert updater.notice(state_with(updater.UPDATED, to="junk"), "0.2.0") is None
    # Already at or past the version: the offer and the failure are stale.
    assert updater.notice(state_with(updater.AVAILABLE), "0.2.0") is None
    assert updater.notice(state_with(updater.FAILED), "0.3.0") is None


def test_due_daily_and_sooner_when_the_run_never_began():
    now = 1_000_000.0
    day, retry = updater.CHECK_INTERVAL, updater.RETRY_INTERVAL
    assert updater.due({}, now)
    assert updater.due({"scheduledAt": "junk"}, now)
    assert updater.due({"scheduledAt": now - day}, now)
    # Started and began work: wait the full day.
    began = {"scheduledAt": now - retry - 1, "startedAt": now - retry}
    assert not updater.due(began, now)
    # Started, but the worker never began (it did not start, or an update
    # held the lock): retry after a short while, not immediately.
    assert not updater.due({"scheduledAt": now - 60}, now)
    assert updater.due({"scheduledAt": now - retry}, now)
    stale = {"scheduledAt": now - retry, "startedAt": now - day}
    assert updater.due(stale, now)


def test_tick_prints_a_notice_once_and_starts_one_background_run(tmp_path, sai_home):
    prefix = official_prefix(tmp_path)
    updater.write_state({"result": {"status": updater.AVAILABLE, "to": "0.2.0"}})
    printed: list[str] = []
    started: list[list[str]] = []

    def start(command: Sequence[str]) -> None:
        started.append(list(command))

    for _ in range(3):
        updater.tick(printed.append, start, prefix)
    assert printed == ["sai 0.2.0 is available. Run `sai update` to install it."]
    # -I: the worker never imports from the person's directory.
    assert started == [
        [sys.executable, "-I", "-m", "sai_cli", "update", "--background"]
    ]
    state = updater.read_state()
    assert state["notified"] == "available:0.2.0"

    # A day later the check is due again.
    updater.write_state({"scheduledAt": time.time() - updater.CHECK_INTERVAL - 1})
    updater.tick(printed.append, start, prefix)
    assert len(started) == 2
    assert len(printed) == 1


def test_two_terminals_print_one_notice_and_start_one_run(tmp_path, sai_home):
    """The notice is read, printed, and marked under the state lock, and the
    daily run is reserved under it."""
    prefix = official_prefix(tmp_path)
    updater.write_state({"result": {"status": updater.UPDATED, "to": "0.1.0"}})
    printed: list[str] = []
    started: list[int] = []
    barrier = threading.Barrier(8)

    def terminal() -> None:
        barrier.wait(10)
        for _ in range(5):
            updater.tick(printed.append, lambda command: started.append(1), prefix)

    threads = [threading.Thread(target=terminal) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(printed) == 1
    assert len(started) == 1


def test_tick_does_nothing_while_another_command_holds_the_state_lock(
    tmp_path, sai_home
):
    prefix = official_prefix(tmp_path)
    updater.write_state({"result": {"status": updater.UPDATED, "to": "0.1.0"}})
    printed: list[str] = []
    started: list[int] = []
    with updater.locked() as held:
        assert held
        updater.tick(printed.append, lambda command: started.append(1), prefix)
    assert (printed, started) == ([], [])
    updater.tick(printed.append, lambda command: started.append(1), prefix)
    assert (len(printed), len(started)) == (1, 1)


def test_tick_leaves_unofficial_installs_alone(tmp_path, sai_home):
    """Eligibility comes first: no notice, no run, and no state written."""
    editable = {"url": "file:///src", "dir_info": {"editable": True}}
    prefix = make_prefix(tmp_path / "checkout" / ".venv", editable)
    sai_home.mkdir()
    state = {"schema": 1, "result": {"status": updater.UPDATED, "to": "0.1.0"}}
    update.cache_path().write_text(json.dumps(state))
    before = update.cache_path().read_bytes()
    printed: list[str] = []
    started: list[int] = []
    updater.tick(printed.append, lambda command: started.append(1), prefix)
    assert (printed, started) == ([], [])
    assert update.cache_path().read_bytes() == before
    assert sorted(path.name for path in sai_home.iterdir()) == ["update.json"]


def test_a_run_that_cannot_start_is_retried_soon(tmp_path, sai_home):
    prefix = official_prefix(tmp_path)
    attempts: list[int] = []

    def explode(command: Sequence[str]) -> None:
        attempts.append(1)
        raise OSError("cannot start")

    updater.tick(lambda text: None, explode, prefix)
    updater.tick(lambda text: None, explode, prefix)
    assert len(attempts) == 1
    scheduled = time.time() - updater.RETRY_INTERVAL - 1
    updater.write_state({"scheduledAt": scheduled})
    updater.tick(lambda text: None, explode, prefix)
    assert len(attempts) == 2


def test_spawn_starts_a_detached_process_in_a_neutral_directory(tmp_path, monkeypatch):
    """The worker does not inherit the person's directory, and a relative
    SAI_HOME still names the same place for it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SAI_HOME", "relative-home")
    marker = tmp_path / "ran"
    code = (
        "import json, os, pathlib; "
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps("
        '[os.getsid(0), os.getcwd(), os.environ["SAI_HOME"]]))'
    )
    updater.spawn([sys.executable, "-I", "-c", code])
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.1)
    session, cwd, home = json.loads(marker.read_text())
    assert session != os.getsid(0)
    assert cwd == updater.NEUTRAL_DIR != str(tmp_path)
    assert home == str(tmp_path / "relative-home")


@pytest.mark.parametrize(
    ("subcommand", "json_output", "environ", "terminal", "expected"),
    [
        ("market", False, {}, True, True),
        (None, False, {}, True, True),
        ("market", True, {}, True, False),
        ("market", False, {"CI": "true"}, True, False),
        ("market", False, {}, False, False),
        ("mcp", False, {}, True, False),
        ("update", False, {}, True, False),
        ("doctor", False, {}, True, False),
        # `sai config set update.auto false` must not start an update first.
        ("config", False, {}, True, False),
        ("logs", False, {}, True, False),
        ("skill", False, {}, True, False),
    ],
)
def test_auto_update_applies_only_to_interactive_commands(
    subcommand, json_output, environ, terminal, expected
):
    assert auto_update_applies(subcommand, json_output, environ, terminal) is expected


def test_commands_under_the_test_runner_never_tick(sai_home, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    result = CliRunner().invoke(main, ["config", "list"])
    assert result.exit_code == 0, result.output
    assert not update.cache_path().exists()
    assert not updater.state_lock_path().exists()
