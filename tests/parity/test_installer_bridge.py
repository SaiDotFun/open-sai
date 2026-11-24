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


DENY_SYSCALLS = """
import ctypes, errno, os, sys
syscalls, binary, *args = sys.argv[1:]
lib = ctypes.CDLL('libseccomp.so.2')
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_rule_add_array.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
lib.seccomp_load.argtypes = [ctypes.c_void_p]
lib.seccomp_release.argtypes = [ctypes.c_void_p]
ctx = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
assert ctx
for name in syscalls.split(","):
    syscall = lib.seccomp_syscall_resolve_name(name.encode())
    assert syscall >= 0
    assert lib.seccomp_rule_add_array(ctx, 0x00050000 | errno.ENOSYS, syscall, 0, None) == 0
assert lib.seccomp_load(ctx) == 0
lib.seccomp_release(ctx)
os.execv(binary, [binary, *args])
"""


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
        assert not os.environ.get("SAI_C10B_REQUIRED"), (
            f"the rehearsal requires SAI_C10B_{name}"
        )
        pytest.skip(f"set SAI_C10B_{name} to the locally built release artifact")
    return Path(value).resolve(strict=True)


def test_bridge_build_refuses_the_published_python_version(install: Install) -> None:
    result = install.run(
        sys.executable,
        ROOT / "scripts" / "build-bridge.py",
        "--version",
        PRE_BRIDGE,
        "--out-dir",
        install.home / "dist",
    )
    assert result.returncode == 2
    assert "requires a distinct stable version newer than 0.1.2" in result.stderr
    assert not (install.home / "dist").exists()


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
        not os.environ.get("SAI_C10B_REQUIRED")
        and not (ROOT / "sai/src/mcp.rs").is_file()
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
def test_uv_owned_launcher_rejects_an_explicit_native_prerelease(
    install: Install, replay: Replay
) -> None:
    prepare_uv(install, artifact("BRIDGE"), replay)
    requests = len(replay.requests)
    result = install.install(replay, "--native", "--dry-run", "--version", "0.2.0-rc.1")
    assert result.returncode == 1
    assert "'0.2.0-rc.1' is not a version like 0.2.0." in result.stderr
    assert "Running" not in result.stdout
    assert len(replay.requests) == requests


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
    if event == 'sai.launcher.rename' and os.fsdecode(values[1]) == destination:
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
@pytest.mark.parametrize("phase", ["reinstall", "rollback"])
@pytest.mark.parametrize("crash", [False, True])
def test_standalone_preserves_an_owner_change_during_switch(
    install: Install, replay: Replay, phase: str, crash: bool
) -> None:
    if phase == "reinstall":
        native_artifact(replay)
        ok(install.install(replay, "--native"))
    else:
        native_artifact(replay, "MUTANT")
    external = install.home / "external-sai"
    external.write_text("another installer's binary")
    replacement = install.home / "new-owner"
    replacement.symlink_to(external)
    expected = replacement.lstat()
    probe = """
import os, runpy, signal, sys
script, launcher, competitor, phase, crash, *args = sys.argv[1:]
changed = False
def race(event, values):
    global changed
    if changed:
        if crash == 'True' and event == 'sai.launcher.renamed':
            os.kill(os.getpid(), signal.SIGKILL)
        return
    if phase == 'reinstall':
        switch = event in ('os.rename', 'sai.launcher.rename') and os.fsdecode(values[1]) == launcher
    else:
        switch = event in ('os.remove', 'sai.launcher.rename') and os.fsdecode(values[0]) == launcher
    if switch:
        changed = True
        os.replace(competitor, launcher)
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
        replacement,
        phase,
        str(crash),
        "--native",
        "--api-base",
        replay.base,
        "--web-base",
        replay.base,
    )
    if crash:
        assert result.returncode == -9, (result.stdout, result.stderr)
        result = install.install(replay, "--native")
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "launcher changed during the switch" in result.stderr
    assert os.path.samestat(install.launcher.lstat(), expected)
    assert install.launcher.readlink() == external


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
@pytest.mark.parametrize(
    "recovery",
    [
        "public",
        "retained",
        pytest.param(
            "without-links",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="uses Linux seccomp"
            ),
        ),
    ],
)
def test_native_updater_returns_a_competing_launcher_after_killed_reinstall(
    install: Install, replay: Replay, recovery: str
) -> None:
    native_artifact(replay, "RECOVERY_RUST")
    ok(install.install(replay, "--native"))
    external = install.home / "competing-sai"
    external.write_text("competing installer's binary")
    competitor = install.home / "competing-launcher"
    competitor.symlink_to(external)
    expected = competitor.lstat()
    probe = """
import os, runpy, signal, sys
script, launcher, competitor, *args = sys.argv[1:]
def kill_after_exchange(event, values):
    if event == 'sai.launcher.rename' and values[1] == launcher:
        os.replace(competitor, launcher)
    if event == 'sai.launcher.renamed' and values[1] == launcher:
        os.kill(os.getpid(), signal.SIGKILL)
sys.addaudithook(kill_after_exchange)
sys.argv = [script, *args]
runpy.run_path(script, run_name='__main__')
"""
    result = install.run(
        sys.executable,
        "-c",
        probe,
        INSTALLER,
        install.launcher,
        competitor,
        "--native",
        "--api-base",
        replay.base,
        "--web-base",
        replay.base,
    )
    assert result.returncode == -9, (result.stdout, result.stderr)
    recovery_command: list[str | Path] = [install.launcher]
    if recovery == "retained":
        # Rehearse a second interruption after recovery withdrew its own entry.
        recovery_command = [install.launcher.resolve(strict=True)]
        install.launcher.rename(pending(install).parent / ".launcher-switch" / "public")
    elif recovery == "without-links":
        recovery_command = [
            sys.executable,
            "-c",
            DENY_SYSCALLS,
            "link,linkat",
            install.launcher,
        ]
    result = install.run(
        *recovery_command,
        "--json",
        "update",
        "--api-base",
        replay.base,
        "--web-base",
        replay.base,
    )
    assert "launcher changed during the switch" in (result.stdout + result.stderr)
    assert os.path.samestat(install.launcher.lstat(), expected)
    assert install.launcher.readlink() == external
    assert not pending(install).exists()


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
def test_standalone_keeps_the_newest_competing_launcher_public(
    install: Install, replay: Replay
) -> None:
    native_artifact(replay)
    ok(install.install(replay, "--native"))
    older = install.home / "older-competitor"
    newer = install.home / "newer-competitor"
    older.write_text("older launcher")
    newer.write_text("newer launcher")
    older_entry, newer_entry = older.stat(), newer.stat()
    probe = """
import os, runpy, sys
script, launcher, older, newer, *args = sys.argv[1:]
competitors = iter([older, newer])
def race(event, values):
    if event == 'sai.launcher.rename' and launcher in values:
        replacement = next(competitors, None)
        if replacement:
            os.replace(replacement, launcher)
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
        older,
        newer,
        "--native",
        "--api-base",
        replay.base,
        "--web-base",
        replay.base,
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    staged = pending(install).parent / ".launcher-switch" / "sai"
    assert str(staged) in result.stderr
    assert os.path.samestat(install.launcher.stat(), newer_entry)
    assert os.path.samestat(staged.stat(), older_entry)
    assert pending(install).is_file()


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


@pytest.mark.skipif(sys.platform != "linux", reason="uses Linux seccomp")
def test_standalone_rolls_back_without_rename_noreplace(
    install: Install, replay: Replay
) -> None:
    native_artifact(replay, "MUTANT")
    # Simulate a kernel without renameat2 without replacing any installer code.
    result = install.run(
        sys.executable,
        "-c",
        DENY_SYSCALLS,
        "renameat2",
        sys.executable,
        INSTALLER,
        "--native",
        "--api-base",
        replay.base,
        "--web-base",
        replay.base,
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "installed MCP startup mutation" in result.stderr
    assert "rolled back" in result.stderr
    assert not install.launcher.is_symlink()
    assert not pending(install).exists()


def interrupt_install(install: Install, replay: Replay, boundary: str) -> None:
    probe = """
import os, runpy, signal, sys
script, launcher, record, boundary, *args = sys.argv[1:]
def interrupt(event, values):
    if (
        (boundary == 'record' and event == 'os.rename' and os.fsdecode(values[1]) == record)
        or (boundary == 'link' and event == 'sai.launcher.rename' and os.fsdecode(values[1]) == launcher)
        or (boundary == 'smoke' and event == 'subprocess.Popen'
            and [os.fsdecode(value) for value in values[1]] == [launcher, '--version'])
        or (boundary == 'commit' and event == 'os.remove' and os.fsdecode(values[0]) == record)
    ):
        os.kill(os.getpid(), signal.SIGKILL)
sys.addaudithook(interrupt)
sys.argv = [script, *args]
runpy.run_path(script, run_name='__main__')
"""
    result = install.run(
        sys.executable,
        "-c",
        probe,
        INSTALLER,
        install.launcher,
        pending(install),
        boundary,
        "--native",
        "--api-base",
        replay.base,
        "--web-base",
        replay.base,
    )
    pre_mcp_xfail(result)
    assert result.returncode == -9, (result.stdout, result.stderr)


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
@pytest.mark.parametrize("boundary", ["record", "link", "smoke", "commit"])
def test_standalone_recovers_an_interrupted_first_switch(
    install: Install, replay: Replay, boundary: str
) -> None:
    native_artifact(replay)
    interrupt_install(install, replay, boundary)
    assert pending(install).exists() == (boundary != "record")
    assert install.launcher.is_symlink() == (boundary in ("smoke", "commit"))
    replay.redirects[f"/repos{REPO}/releases"] = "/unavailable"
    result = install.install(replay, "--native")
    assert result.returncode == 1 and "HTTP 302" in result.stderr
    assert not install.launcher.is_symlink()
    assert not pending(install).exists()


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
def test_native_updater_recovers_an_interrupted_first_install(
    install: Install, replay: Replay
) -> None:
    native_artifact(replay, "RECOVERY_RUST")
    interrupt_install(install, replay, "smoke")
    assert json.loads(pending(install).read_text())["old"] is None
    result = install.run(
        install.launcher, "update", "--api-base", replay.base, "--web-base", replay.base
    )
    assert "first Sai install was rolled back" in result.stderr
    assert not install.launcher.is_symlink()
    assert not pending(install).exists()


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
@pytest.mark.parametrize("profile_matches", [True, False])
def test_standalone_reinstall_repairs_cursor(
    install: Install, replay: Replay, profile_matches: bool
) -> None:
    native_artifact(replay)
    result = install.install(replay, "--native")
    pre_mcp_xfail(result)
    ok(result)
    cursor = install.home / ".cursor" / "mcp.json"
    cursor.parent.mkdir(mode=0o700)
    other = {"url": f"{replay.base}/service"}
    profile = install.profile if profile_matches else install.home / "different-profile"
    servers = {
        "sai": {"command": OLD_MCP_COMMAND, "env": {"SAI_HOME": str(profile)}},
        "other": other,
    }
    cursor.write_text(json.dumps({"mcpServers": servers}))
    result = install.install(replay, "--native")
    ok(result)
    assert ("different SAI_HOME setting" in result.stderr) is not profile_matches
    repaired = json.loads(cursor.read_text())["mcpServers"]
    assert repaired["sai"] == {
        "command": str(install.launcher),
        "args": ["mcp", "serve"],
        "env": {"SAI_HOME": str(profile)},
    }
    assert repaired["other"] == other
    assert not install.profile.exists()


@pytest.mark.skipif(WINDOWS, reason="standalone targets are Linux and macOS")
@pytest.mark.parametrize(
    ("scope", "filename"), [("user", ".claude/.claude.json"), ("project", ".mcp.json")]
)
def test_standalone_reinstall_prints_claude_repair_scopes(
    install: Install, replay: Replay, scope: str, filename: str
) -> None:
    version = native_artifact(replay)
    ok(install.install(replay, "--native"))
    (install.launcher.parent / "claude").symlink_to("/bin/false")
    original = json.dumps(
        {
            "mcpServers": {
                "sai": {"type": "stdio", "command": OLD_MCP_COMMAND, "args": []}
            }
        }
    )
    config = install.home / filename
    config.parent.mkdir(exist_ok=True)
    config.write_text(original)
    result = install.install(replay, "--native")
    ok(result)
    assert (
        f"sai mcp install --client claude --scope {scope} --repair-existing sai"
        in result.stderr
    )
    assert config.read_text() == original
    assert not pending(install).exists()
    install.pin(version)


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
        archive_path = (
            f"{REPO}/releases/download/sai-cli/v{version}/{asset_name(version)}"
        )
        del replay.assets[archive_path]
        requests = len(replay.requests)
        install.pin(version)
        assert len(replay.requests) == requests
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


@pytest.mark.skipif(WINDOWS, reason="native uv recovery runs on POSIX")
def test_native_updater_recovers_an_interrupted_bridge(
    install: Install, replay: Replay
) -> None:
    python_wheel, shim = artifact("BRIDGE"), artifact("SHIM")
    binary = artifact("RECOVERY_RUST")
    prepare_uv(install, python_wheel, replay)
    profile, contents = save_profile(install, replay)
    previous = install.launcher.resolve(strict=True)
    version = add_shim(replay, shim, binary)
    kill_after_uv = """
import os, signal, sys
from sai_cli.sai_cli import main
launcher, *args = sys.argv[1:]
def interrupt(event, values):
    if (event == 'subprocess.Popen'
        and [os.fsdecode(value) for value in values[1]] == [launcher, '--version']):
        os.kill(os.getpid(), signal.SIGKILL)
sys.addaudithook(interrupt)
sys.argv = [launcher, *args]
main()
"""
    result = install.run(
        install.tools / "sai-cli" / "bin" / "python",
        "-I",
        "-c",
        kill_after_uv,
        install.launcher,
        "update",
        "--api-base",
        replay.base,
        "--web-base",
        replay.base,
    )
    pre_mcp_xfail(result)
    assert result.returncode == -9, (result.stdout, result.stderr)
    record = install.tools / ".sai-cli-native-pending.json"
    assert json.loads(record.read_text())["next"]["version"] == version
    archive_path = f"{REPO}/releases/download/sai-cli/v{version}/{asset_name(version)}"
    native_archive = replay.assets.pop(archive_path)
    install.pin(version)
    result = install.run(
        install.launcher,
        "--json",
        "update",
        "--recover",
        "--api-base",
        replay.base,
        "--web-base",
        replay.base,
    )
    assert json.loads(ok(result))["status"] == "recovered"
    install.pin(wheel_version(python_wheel))
    assert install.launcher.resolve(strict=True) == previous
    assert profile.read_bytes() == contents
    assert not record.exists()

    # Retry after the old wheel becomes unavailable from the release server.
    replay.assets[archive_path] = native_archive
    old_release = f"{REPO}/releases/download/sai-cli/v{wheel_version(python_wheel)}/"
    del replay.assets[old_release + "SHA256SUMS"]
    del replay.assets[old_release + python_wheel.name]
    requests = len(replay.requests)
    result = install.update(replay)
    assert json.loads(ok(result))["status"] == "updated"
    assert not any(path.startswith(old_release) for path in replay.requests[requests:])
    install.pin(version)
    assert profile.read_bytes() == contents


@pytest.mark.skipif(WINDOWS, reason="native uv recovery runs on POSIX")
def test_bridge_recovers_when_uv_removed_the_environment_and_launcher(
    install: Install, replay: Replay
) -> None:
    python_wheel = artifact("BRIDGE")
    prepare_uv(install, python_wheel, replay)
    profile, contents = save_profile(install, replay)
    candidate = add_shim(replay, artifact("SHIM"), artifact("RECOVERY_RUST"))
    original_env = install.env.copy()
    uv = shutil.which("uv", path=original_env["PATH"])
    assert uv is not None
    interceptor = install.home / "interrupt"
    interceptor.mkdir()
    wrapper = interceptor / "uv"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, subprocess, sys\n"
        f"uv = {uv!r}\n"
        "if sys.argv[1:4] == ['tool', 'install', '--force']:\n"
        "    subprocess.run([uv, 'tool', 'uninstall', 'sai-cli'], check=True)\n"
        "    os.kill(os.getppid(), signal.SIGKILL)\n"
        "else:\n"
        "    os.execv(uv, [uv, *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o700)
    install.env["PATH"] = str(interceptor) + os.pathsep + original_env["PATH"]
    result = install.update(replay)
    install.env = original_env
    assert result.returncode == -9, (result.stdout, result.stderr)
    assert not (install.tools / "sai-cli").exists()
    assert not os.path.lexists(install.launcher)

    record = install.tools / ".sai-cli-native-pending.json"
    recovery = Path(json.loads(record.read_text())["recovery"])
    assert recovery.parent == install.tools
    assert str(recovery) in result.stderr and "update --recover" in result.stderr
    del replay.assets[
        f"{REPO}/releases/download/sai-cli/v{candidate}/{asset_name(candidate)}"
    ]
    result = install.run(
        recovery,
        "--json",
        "update",
        "--recover",
        "--api-base",
        replay.base,
        "--web-base",
        replay.base,
    )
    assert json.loads(ok(result))["status"] == "recovered"
    install.pin(wheel_version(python_wheel))
    assert profile.read_bytes() == contents
    assert not record.exists()


@pytest.mark.skipif(WINDOWS, reason="native uv recovery runs on POSIX")
def test_bridge_requires_an_offline_rollback_install(
    install: Install, replay: Replay
) -> None:
    python_wheel, shim = artifact("BRIDGE"), artifact("SHIM")
    prepare_uv(install, python_wheel, replay)
    add_shim(replay, shim, artifact("RUST"))
    install.env["UV_CACHE_DIR"] = str(install.home / "empty-uv-cache")
    result = install.update(replay)
    pre_mcp_xfail(result)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert (
        "rollback wheel failed its offline trial install"
        in json.loads(result.stdout)["error"]["message"]
    )
    install.pin(wheel_version(python_wheel))
    assert not (install.tools / ".sai-cli-native-pending.json").exists()


@pytest.mark.skipif(WINDOWS, reason="native uv recovery runs on POSIX")
def test_bridge_honors_opt_out_during_the_offline_rollback_trial(
    install: Install, replay: Replay
) -> None:
    python_wheel = artifact("BRIDGE")
    prepare_uv(install, python_wheel, replay)
    add_shim(replay, artifact("SHIM"), artifact("RUST"))
    profile, _ = save_profile(install, replay)
    opt_out = """
import sys
from pathlib import Path
from sai_cli.sai_cli import main
profile, launcher, *args = sys.argv[1:]
def during_trial(event, values):
    if event == 'subprocess.Popen' and list(values[1])[1:4] == ['pip', 'install', '--offline']:
        with Path(profile).open('a') as config:
            config.write('\\n[update]\\nauto = false\\n')
sys.addaudithook(during_trial)
sys.argv = [launcher, *args]
main()
"""
    result = install.run(
        install.tools / "sai-cli" / "bin" / "python",
        "-I",
        "-c",
        opt_out,
        profile,
        install.launcher,
        "update",
        "--background",
        "--api-base",
        replay.base,
        "--web-base",
        replay.base,
    )
    ok(result)
    assert "auto = false" in profile.read_text()
    state = json.loads((install.profile / "update.json").read_text())
    assert state["result"]["status"] == "available"
    install.pin(wheel_version(python_wheel))
    assert not (install.tools / ".sai-cli-native-pending.json").exists()


@pytest.mark.skipif(WINDOWS, reason="native uv recovery runs on POSIX")
def test_bridge_explains_how_to_convert_an_official_uv_git_install(
    install: Install, replay: Replay
) -> None:
    python_wheel = artifact("BRIDGE")
    prepare_uv(install, python_wheel, replay)
    version = wheel_version(python_wheel)
    profile, contents = save_profile(install, replay)
    prefix = install.tools / "sai-cli"
    (direct_url,) = prefix.glob(
        "lib/python*/site-packages/sai_cli-*.dist-info/direct_url.json"
    )
    # Replay PEP 610 metadata from an official Git install without fetching GitHub.
    direct_url.write_text(
        json.dumps(
            {
                "url": "https://github.com/SaiDotFun/open-sai",
                "vcs_info": {"vcs": "git", "commit_id": PRE_BRIDGE_COMMIT},
            }
        )
    )
    candidate = add_shim(replay, artifact("SHIM"), artifact("RUST"))
    result = install.update(replay)
    assert result.returncode == 1
    instruction = json.loads(result.stdout)["error"]["message"]
    assert "uv tool uninstall sai-cli" in instruction
    assert "scripts/install.py --version VERSION" in instruction
    assert not any(path.endswith(asset_name(candidate)) for path in replay.requests)
    install.pin(version)

    ok(install.run("uv", "tool", "uninstall", "sai-cli"))
    ok(install.install(replay, "--version", version))
    install.pin(version)
    assert profile.read_bytes() == contents


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
    install.pin(wheel_version(python_wheel))
    assert install.launcher.read_bytes() == launcher
    assert profile.read_bytes() == contents
