"""Replay the public installer and Python updater against local release assets."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "install.py"
REPO = "/SaiDotFun/open-sai"
PRE_BRIDGE = "0.1.2"
PRE_BRIDGE_COMMIT = "8557a7e13dceab0accc369191182c8e01423c91b"
OLD_MCP_COMMAND = "sai" + "-mcp"
WINDOWS = os.name == "nt"


def wheel_version(wheel: Path) -> str:
    return wheel.name.split("-")[1]


def asset_name(version: str) -> str:
    system = {"Linux": "unknown-linux-gnu", "Darwin": "apple-darwin"}[platform.system()]
    cpu = "aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"
    return f"sai-{version}-{cpu}-{system}.tar.gz"


def archive(binary: Path | None = None, *, member: str = "sai") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w:gz", format=tarfile.USTAR_FORMAT
    ) as output:
        if binary is None:
            entry = tarfile.TarInfo(member)
            entry.size = 1
            output.addfile(entry, io.BytesIO(b"x"))
        else:
            output.add(binary, arcname=member)
    return buffer.getvalue()


class Replay:
    def __init__(self) -> None:
        self.latest = "0.2.0"
        self.requests: list[str] = []
        self.assets: dict[str, bytes] = {}
        self.redirects: dict[str, str] = {}
        replay = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = unquote(self.path.split("?", 1)[0])
                replay.requests.append(path)
                if location := replay.redirects.get(path):
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == f"/repos{REPO}/releases":
                    body = json.dumps(
                        [
                            {"tag_name": "other/v99.0.0"},
                            {"tag_name": "sai-cli/v99.0.0-rc.1", "prerelease": True},
                            {"tag_name": f"sai-cli/v{replay.latest}"},
                            {"tag_name": "sai-cli/v0.0.1"},
                        ]
                    ).encode()
                else:
                    body = replay.assets.get(path)
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def add(self, version: str, files: dict[str, bytes]) -> None:
        self.latest = version
        folder = f"{REPO}/releases/download/sai-cli/v{version}"
        manifest = "".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n"
            for name, data in files.items()
        )
        self.assets.update({f"{folder}/{name}": data for name, data in files.items()})
        self.assets[f"{folder}/SHA256SUMS"] = manifest.encode()


@pytest.fixture
def replay() -> Iterator[Replay]:
    release = Replay()
    worker = Thread(target=release.server.serve_forever, daemon=True)
    worker.start()
    try:
        yield release
    finally:
        release.server.shutdown()
        release.server.server_close()
        worker.join()


@dataclass
class Install:
    home: Path
    profile: Path
    launcher: Path
    tools: Path
    env: dict[str, str] = field(repr=False)

    def run(
        self, *args: str | Path, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=self.home,
            env=self.env if env is None else env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    def install(
        self, replay: Replay, *args: str, script: Path = INSTALLER
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            sys.executable,
            script,
            "--api-base",
            replay.base,
            "--web-base",
            replay.base,
            *args,
        )

    def update(self, replay: Replay) -> subprocess.CompletedProcess[str]:
        return self.run(
            self.launcher,
            "--json",
            "update",
            "--api-base",
            replay.base,
            "--web-base",
            replay.base,
        )

    def pin(self, version: str) -> None:
        output = ok(self.run(self.launcher, "--version"))
        assert output.strip() in (f"sai {version}", f"sai, version {version}")


@pytest.fixture
def install(tmp_path: Path, replay: Replay) -> Install:
    home = tmp_path / "user home"
    home.mkdir(mode=0o700)
    profile = home / ".sai"
    bin_dir = home / ".local" / "bin"
    bin_dir.parent.mkdir(mode=0o700)
    tools = home / "uv-tools"
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "SAI_HOME": str(profile),
        "SAI_LOG": "false",
        "CI": "1",
        "UV_PYTHON": sys.executable,
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_TOOL_DIR": str(tools),
        "UV_TOOL_BIN_DIR": str(bin_dir),
        "SAI_NATIVE_RELEASE_BASE": replay.base,
        "HTTP_PROXY": "http://127.0.0.1:1",
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "ALL_PROXY": "http://127.0.0.1:1",
        "NO_PROXY": "127.0.0.1",
        "UV_OFFLINE": "1",
    }
    for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        env.pop(key, None)
    return Install(
        home, profile, bin_dir / ("sai.exe" if WINDOWS else "sai"), tools, env
    )


def ok(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, (result.stdout, result.stderr)
    return result.stdout


def artifact(name: str) -> Path:
    value = os.environ.get(f"SAI_C10B_{name}")
    if value is None:
        pytest.skip(f"set SAI_C10B_{name} to the locally built release artifact")
    return Path(value).resolve(strict=True)


def save_profile(install: Install, replay: Replay) -> tuple[Path, bytes]:
    ok(
        install.run(
            install.launcher, "config", "set", "keeper.endpoint", f"{replay.base}/query"
        )
    )
    path = install.profile / "config.toml"
    return path, path.read_bytes()


def prepare_uv(
    install: Install, wheel: Path, replay: Replay, script: Path = INSTALLER
) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    # Seed the dependency cache before restricting all release traffic to loopback.
    prime_env = {
        **os.environ,
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_PYTHON": sys.executable,
    }
    default_cache = Path(ok(install.run(uv, "cache", "dir", env=prime_env)).strip())
    install.env["UV_CACHE_DIR"] = str(default_cache)
    ok(
        install.run(
            uv,
            "pip",
            "install",
            "--target",
            install.home / "dependencies",
            wheel,
            env=prime_env,
        )
    )
    version = wheel_version(wheel)
    replay.add(version, {wheel.name: wheel.read_bytes()})
    ok(install.install(replay, script=script))
    install.pin(version)


def pending(install: Install) -> Path:
    return install.launcher.parent / ".sai-native" / "pending.json"


def native_artifact(replay: Replay, name: str = "RUST") -> str:
    binary = artifact(name)
    version = (
        ok(
            subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        )
        .strip()
        .split()[1]
    )
    replay.add(version, {asset_name(version): archive(binary)})
    return version


def pre_mcp_xfail(result: subprocess.CompletedProcess[str]) -> None:
    if (
        not (ROOT / "sai/src/mcp.rs").is_file()
        and "No such command 'mcp'" in result.stderr
    ):
        pytest.xfail("Native MCP startup requires PR #120")


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
def test_standalone_installs_release(install: Install, replay: Replay) -> None:
    version = native_artifact(replay)
    result = install.install(replay, "--native")
    pre_mcp_xfail(result)
    ok(result)
    install.pin(version)
    assert ok(install.run("sai", "--version")).strip() == f"sai {version}"
    link = install.launcher.readlink()
    assert link.parts[0] == ".sai-native"
    assert link.parts[1].startswith(f"{version}-")
    assert link.name == "sai"
    assert (install.launcher.stat().st_mode & 0o777) == 0o700
    assert not pending(install).exists()
    assert not install.profile.exists()
    assert replay.requests[-1].endswith(asset_name(version))


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
def test_native_dry_run_selects_an_explicit_prerelease(
    install: Install, replay: Replay
) -> None:
    version = "0.2.0-rc.1"
    command = install.install(replay, "--native", "--dry-run", "--version", version)
    assert asset_name(version) in ok(command)
    assert not replay.requests
    assert not install.launcher.parent.exists()
    assert not install.profile.exists()


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
def test_standalone_preserves_a_concurrent_launcher(
    install: Install, replay: Replay
) -> None:
    native_artifact(replay)
    # Publish a competing launcher at the syscall boundary, after all reads.
    probe = """
import os, runpy, sys
from pathlib import Path
script, destination, *args = sys.argv[1:]
def race(event, values):
    if event in ('os.rename', 'os.symlink') and os.fsdecode(values[1]) == destination:
        try:
            with Path(destination).open('x') as file:
                file.write('another installer owns this launcher')
        except FileExistsError:
            pass
sys.addaudithook(race)
sys.argv = [script, *args]
runpy.run_path(script, run_name='__main__')
"""
    result = install.run(
        sys.executable,
        "-c",
        probe,
        INSTALLER,
        install.launcher,
        "--native",
        "--api-base",
        replay.base,
        "--web-base",
        replay.base,
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert install.launcher.read_text() == "another installer owns this launcher"
    assert not pending(install).exists()


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
def test_standalone_refuses_an_indirect_managed_target(
    install: Install, replay: Replay
) -> None:
    root = install.launcher.parent / ".sai-native"
    binary = root / ("0.1.2-" + "a" * 64) / "sai"
    binary.parent.mkdir(mode=0o700, parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    alias = install.home / "external-alias"
    alias.symlink_to(binary)
    install.launcher.symlink_to(alias)
    result = install.install(replay, "--native", "--dry-run")
    assert result.returncode == 1 and "through its installer" in result.stderr
    assert install.launcher.readlink() == alias


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
@pytest.mark.parametrize(
    "fault", ["wrong target", "digest", "duplicate manifest", "traversal"]
)
def test_native_rejects_invalid_assets(
    install: Install, replay: Replay, fault: str
) -> None:
    version = replay.latest
    name = asset_name(version)
    data = archive(member="../sai")
    if fault == "wrong target":
        replay.add(version, {f"sai-{version}-elsewhere.tar.gz": data})
        expected = "HTTP 404"
    else:
        replay.add(version, {name: data})
        folder = f"{REPO}/releases/download/sai-cli/v{version}"
        if fault == "digest":
            replay.assets[f"{folder}/{name}"] += b"changed"
            expected = "does not match its SHA256SUMS"
        elif fault == "duplicate manifest":
            manifest = f"{folder}/SHA256SUMS"
            replay.assets[manifest] *= 2
            expected = "must contain one checksum"
        else:
            expected = "one regular file named sai"
    result = install.install(replay, "--native")
    assert result.returncode == 1
    assert expected in result.stderr
    assert not install.launcher.is_symlink()
    assert not install.profile.exists()


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
@pytest.mark.parametrize("fault", ["owner", "path", "permissions", "lock"])
def test_native_refuses_unsafe_destination(
    install: Install, replay: Replay, fault: str
) -> None:
    import fcntl

    install.launcher.parent.mkdir(mode=0o700)
    with (install.launcher.parent / ".sai-update.lock").open("w") as lock:
        os.fchmod(lock.fileno(), 0o600)
        if fault == "lock":
            fcntl.flock(lock, fcntl.LOCK_EX)
            expected = "another Sai install or update"
        elif fault == "permissions":
            install.launcher.parent.chmod(0o777)
            expected = "safe permissions"
        else:
            path = install.launcher
            if fault == "path":
                shadow = install.home / "shadow"
                shadow.mkdir()
                install.env["PATH"] = str(shadow) + os.pathsep + install.env["PATH"]
                path = shadow / "sai"
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o700)
            expected = "which shadows" if fault == "path" else "through its installer"
        result = install.install(replay, "--native")
        assert result.returncode == 1
        assert expected in result.stderr
    assert not pending(install).exists()
    assert not any("/releases/download/" in path for path in replay.requests)


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
@pytest.mark.parametrize("fresh", [True, False])
def test_native_rolls_back_failed_installed_mcp(
    install: Install, replay: Replay, fresh: bool
) -> None:
    artifact("MUTANT")
    old = None
    if not fresh:
        version = native_artifact(replay)
        ok(install.install(replay, "--native"))
        old = install.launcher.readlink()
        config, content = save_profile(install, replay)
    else:
        install.profile.mkdir(mode=0o700)
        config = install.profile / "config.toml"
        content = b"[update]\nauto = false\n"
        config.write_bytes(content)
    version = native_artifact(replay, "MUTANT")
    result = install.install(replay, "--native", "--version", version)
    assert result.returncode == 1
    assert "rolled back" in result.stderr and "mcp serve" in result.stderr
    if old is None:
        assert not install.launcher.is_symlink()
    else:
        assert install.launcher.readlink() == old
    assert not pending(install).exists()
    assert config.read_bytes() == content


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
def test_standalone_recovers_an_interrupted_first_switch(
    install: Install, replay: Replay
) -> None:
    native_artifact(replay)
    result = install.install(replay, "--native")
    pre_mcp_xfail(result)
    ok(result)
    directory = install.launcher.readlink().parts[1]
    pending(install).write_text(json.dumps({"old": None, "new": directory}))
    replay.redirects[f"/repos{REPO}/releases"] = "/unavailable"
    result = install.install(replay, "--native")
    assert result.returncode == 1 and "HTTP 302" in result.stderr
    assert not install.launcher.is_symlink()
    assert not pending(install).exists()


def add_shim(replay: Replay, wheel: Path, binary: Path | None = None) -> str:
    version = wheel_version(wheel)
    files = {wheel.name: wheel.read_bytes()}
    if binary is not None:
        files[asset_name(version)] = archive(binary)
    replay.add(version, files)
    return version


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.skipif(WINDOWS, reason="Windows has a separate compatibility rehearsal")
def test_python_bridge_preserves_uv_and_rolls_back(
    install: Install, replay: Replay, failure: bool
) -> None:
    wheel = artifact("SHIM")
    python_wheel = artifact("BRIDGE")
    binary = artifact("MUTANT" if failure else "RUST")
    prepare_uv(install, python_wheel, replay)
    previous = install.launcher.resolve(strict=True)
    profile, contents = save_profile(install, replay)
    python = install.tools / "sai-cli" / "bin" / "python"
    ok(
        install.run(
            python,
            "-I",
            "-c",
            "import sys; from sai_cli.core.storage import pending; pending(*sys.argv[1:])",
            "20261124T120000000Z_0123456789ABCDEF",
            f"{replay.base}/query",
            "0x" + "1" * 40,
            "CLOSE_POSITION",
            "7",
        )
    )
    ledger = install.profile / "trader" / "intents.sqlite3"
    before = ledger.read_bytes()
    inode = ledger.stat().st_ino
    cursor = install.home / ".cursor" / "mcp.json"
    cursor.parent.mkdir()
    other = {"url": f"{replay.base}/service"}
    original = {
        "mcpServers": {
            "sai": {
                "command": OLD_MCP_COMMAND,
                "env": {"SAI_HOME": str(install.profile)},
            },
            "other": other,
        }
    }
    cursor.write_text(json.dumps(original))
    version = add_shim(replay, wheel, binary)
    # This is the public installer path for an existing uv-owned launcher.
    result = install.install(replay, "--native")
    pre_mcp_xfail(result)
    assert "is owned by uv" in result.stdout
    if failure:
        assert result.returncode == 1
        assert "Rolled back to sai" in result.stderr and "mcp serve" in result.stderr
        install.pin(wheel_version(python_wheel))
        assert json.loads(cursor.read_text()) == original
    else:
        ok(result)
        install.pin(version)
        assert len(list(install.tools.glob(".sai-cli-rollback-*"))) == 1
        repaired = json.loads(cursor.read_text())["mcpServers"]
        assert repaired["sai"] == {
            "command": str(install.launcher),
            "args": ["mcp", "serve"],
            "env": {"SAI_HOME": str(install.profile)},
        }
        assert repaired["other"] == other
    assert install.launcher.resolve(strict=True) == previous
    assert profile.read_bytes() == contents
    assert ledger.read_bytes() == before and ledger.stat().st_ino == inode
    assert not (install.launcher.parent / ".sai-native").exists()


def pre_bridge_files() -> tuple[Path, Path]:
    baseline = artifact("PRE_BRIDGE")
    assert (baseline / "commit").read_text().strip() == PRE_BRIDGE_COMMIT
    return baseline / f"sai_cli-{PRE_BRIDGE}-py3-none-any.whl", baseline / "install.py"


def test_python_bridge_rejects_cross_origin_manifest_redirect(
    install: Install, replay: Replay
) -> None:
    python_wheel = artifact("BRIDGE")
    wheel = artifact("SHIM")
    prepare_uv(install, python_wheel, replay)
    version = add_shim(replay, wheel)
    manifest = f"{REPO}/releases/download/sai-cli/v{version}/SHA256SUMS"
    with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as other:
        replay.redirects[manifest] = f"http://127.0.0.1:{other.server_port}/SHA256SUMS"
        result = install.update(replay)
    assert result.returncode == 1
    assert (
        "redirected outside its download hosts"
        in json.loads(result.stdout)["error"]["message"]
    )
    install.pin(wheel_version(python_wheel))


@pytest.mark.skipif(WINDOWS, reason="Windows has a separate compatibility rehearsal")
def test_pre_bridge_updater_can_skip_to_the_native_shim(
    install: Install, replay: Replay
) -> None:
    baseline, script = pre_bridge_files()
    prepare_uv(install, baseline, replay, script)
    profile, contents = save_profile(install, replay)
    version = add_shim(replay, artifact("SHIM"), artifact("RUST"))
    result = json.loads(ok(install.update(replay)))
    assert result["status"] == "updated"
    install.pin(version)
    assert profile.read_bytes() == contents


@pytest.mark.skipif(not WINDOWS, reason="requires a Windows uv launcher")
@pytest.mark.parametrize("pre_bridge", [False, True])
def test_windows_keeps_a_runnable_python_launcher(
    install: Install, replay: Replay, pre_bridge: bool
) -> None:
    wheel = artifact("SHIM")
    if pre_bridge:
        python_wheel, script = pre_bridge_files()
    else:
        python_wheel = artifact("BRIDGE")
        script = INSTALLER
    prepare_uv(install, python_wheel, replay, script)
    profile, contents = save_profile(install, replay)
    launcher = install.launcher.read_bytes()
    version = add_shim(replay, wheel)
    result = install.update(replay)
    assert result.returncode == (1 if pre_bridge else 3), (result.stdout, result.stderr)
    message = json.loads(result.stdout)["error"]["message"]
    expected = (
        "Native Sai supports Linux and macOS"
        if pre_bridge
        else f"sai-cli=={PRE_BRIDGE}"
    )
    assert expected in message
    assert not any(f"/sai-{version}-" in path for path in replay.requests)
    install.pin(PRE_BRIDGE)
    assert install.launcher.read_bytes() == launcher
    assert profile.read_bytes() == contents
