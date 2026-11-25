"""Exercise native updates with local assets and real launcher ownership."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
import zipfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from reference_uv import resolve_uv
from sai_cli.core import update as python_update
from sai_cli.tests.loopback import http_server

fcntl = pytest.importorskip("fcntl")
pty = pytest.importorskip("pty")

REPO = "/SaiDotFun/open-sai"
NEW = "9999.0.0"
PLATFORM = {
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
}.get((platform.system(), platform.machine()))


def archive_name(version: str = NEW) -> str:
    return f"sai-{version}-{PLATFORM}.tar.gz"


pytestmark = pytest.mark.skipif(
    PLATFORM is None, reason="native archive target required"
)
MCP = """
import json
import os
import sys
from pathlib import Path

def run():
    args = sys.argv[1:]
    public = Path(os.environ["FIXTURE_STABLE_LAUNCHER"])
    location = Path(__file__).resolve()
    in_stable = location.parent.parent.name == ".sai-native" and (
        location.parent.name.startswith(f"{VERSION}-")
    )
    in_stable = in_stable or sys.prefix == os.environ.get("FIXTURE_STABLE_UV_PREFIX")
    if args == ["--version"]:
        if FAIL == "replace-launcher" and in_stable:
            public.with_name("competitor").replace(public)
        print(f"sai {VERSION}")
    elif args == ["config", "get", "update.auto"]:
        print("true")
    elif args == ["mcp", "serve"]:
        if FAIL == "trial" or (FAIL == "installed" and in_stable):
            return 9
        request = json.loads(sys.stdin.readline())
        assert request["method"] == "initialize"
        print(json.dumps({
            "jsonrpc": "2.0", "id": request["id"],
            "result": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "serverInfo": {"name": "sai", "version": VERSION}},
        }), flush=True)
        sys.stdin.read()
    elif FORWARD:
        candidate = os.environ["FIXTURE_NATIVE_BINARY"]
        os.execve(candidate, [candidate, *args], {
            **os.environ, "SAI_NATIVE_UV_PREFIX": sys.prefix
        })
    else:
        return 2
    return 0
"""


CACHE_SHIM = """
from urllib.request import urlopen

def main():
    home = Path(os.environ["SAI_HOME"])
    if home.stat().st_mode & 0o022:
        return 8
    cache = home / "bin" / "fixture-shim" / VERSION
    cached = cache / "verified"
    if not cached.exists():
        base = os.environ["SAI_NATIVE_RELEASE_BASE"]
        with urlopen(base + ARCHIVE_PATH, timeout=5) as response:
            if not response.read():
                return 7
        cache.mkdir(parents=True, exist_ok=True)
        cached.touch()
    return run()
"""


def executable(version: str, *, fail: str = "", forward: bool = False) -> str:
    return f"VERSION = {version!r}\nFAIL = {fail!r}\nFORWARD = {forward!r}\n{MCP}"


def archive(version: str = NEW, *, fail: str = "", bad_entry: str = "") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = f"#!{sys.executable}\n{executable(version, fail=fail)}\nraise SystemExit(run())\n".encode()
        entry = tarfile.TarInfo("../sai" if bad_entry == "traversal" else "sai")
        entry.mode = 0o755
        if bad_entry == "symlink":
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/bin/false"
            tar.addfile(entry)
        else:
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
            if bad_entry == "duplicate":
                tar.addfile(entry, io.BytesIO(data))
    return buffer.getvalue()


def wheel(
    tmp: Path,
    version: str,
    *,
    fail: str = "",
    forward: bool = False,
    cached: bool = False,
) -> Path:
    wheel_version = version.replace("-rc.", "rc")
    filename = f"sai_cli-{wheel_version}-py3-none-any.whl"
    name = f"sai_cli-{wheel_version}.dist-info"
    script = executable(version, fail=fail, forward=forward)
    entry = "run"
    if cached:
        entry = "main"
        source = os.environ.get("SAI_PARITY_C10_SHIM")
        if source:
            script = Path(source).read_text(encoding="utf-8")
        else:
            route = asset_key(version, archive_name(version))
            script += f"\nARCHIVE_PATH = {route!r}\n{CACHE_SHIM}"
    files = {
        "fixture_sai.py": script,
        f"{name}/METADATA": f"Metadata-Version: 2.1\nName: sai-cli\nVersion: {wheel_version}\n",
        f"{name}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{name}/entry_points.txt": f"[console_scripts]\nsai = fixture_sai:{entry}\n",
    }
    files[f"{name}/RECORD"] = "".join(
        f"{path},,\n" for path in [*files, f"{name}/RECORD"]
    )
    path = tmp / filename
    with zipfile.ZipFile(path, "w") as zip_file:
        for filename, content in files.items():
            zip_file.writestr(filename, content)
    return path


def asset_key(version: str, name: str) -> str:
    return f"{REPO}/releases/download/sai-cli/v{version}/{name}"


def assets(
    version: str, name: str, data: bytes, *, manifest: str = "valid"
) -> dict[str, bytes]:
    digest = hashlib.sha256(data).hexdigest()
    if manifest == "wrong":
        digest = "0" * 64
    entry = f"{digest} *{name}\n"
    if manifest == "duplicate":
        entry *= 2
    return {
        asset_key(version, name): data,
        asset_key(version, "SHA256SUMS"): entry.encode(),
    }


@dataclass
class ReleaseFixture:
    base: str
    reads: list[str]

    @property
    def args(self) -> tuple[str, ...]:
        return ("--api-base", self.base, "--web-base", self.base)


@dataclass(frozen=True)
class Redirect:
    location: str


@dataclass(frozen=True)
class Streamed:
    chunk: bytes
    count: int


@contextmanager
def releases(
    files: Mapping[str, bytes | Redirect | Streamed],
    *,
    tags: tuple[str, ...] = (NEW,),
    pause: tuple[str, threading.Event, threading.Event] | None = None,
) -> Generator[ReleaseFixture]:
    reads: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            reads.append(path)
            if pause is not None and pause[0] == path:
                pause[1].set()
                if not pause[2].wait(12):
                    self.send_error(503)
                    return
            if path == f"/repos{REPO}/releases":
                data = json.dumps(
                    [{"tag_name": f"sai-cli/v{tag}"} for tag in tags]
                ).encode()
            elif path in files:
                data = files[path]
            else:
                self.send_error(404)
                return
            if isinstance(data, Redirect):
                self.send_response(302)
                self.send_header("Location", data.location)
                self.end_headers()
                return
            if isinstance(data, Streamed):
                # HTTP/1.0 delimits this body by closing the connection.
                self.send_response(200)
                self.end_headers()
                try:
                    for _ in range(data.count):
                        self.wfile.write(data.chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with http_server(Handler) as server:
        yield ReleaseFixture(server, reads)


@dataclass
class NativeInstall:
    launcher: Path
    executable: Path
    home: Path
    env: dict[str, str]
    installed: str

    def invoke(
        self,
        *args: str,
        exe: Path | None = None,
        cwd: Path | None = None,
        umask: int = -1,
        **overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [exe or self.launcher, *args],
            env={**self.env, **overrides},
            cwd=cwd or self.home.parent,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            umask=umask,
        )

    def update(
        self, server: ReleaseFixture, *args: str, **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        return self.invoke("--json", "update", *server.args, *args, **overrides)

    def state(self) -> dict[str, object]:
        return json.loads((self.home / "update.json").read_text(encoding="utf-8"))


@pytest.fixture
def standalone(
    cli_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> NativeInstall:
    tmp_path = tmp_path.resolve()
    root = tmp_path / "bin"
    launcher = root / "sai"
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    env = {
        "PATH": os.pathsep.join([str(root), os.defpath]),
        "HOME": str(tmp_path),
        "SAI_HOME": str(home),
        "SAI_LOG": "false",
        "CI": "1",
        "FIXTURE_STABLE_LAUNCHER": str(launcher),
        "NO_PROXY": "127.0.0.1,localhost,::1",
    }
    binary = cli_pair[1][0]
    version = subprocess.check_output(
        [binary, "--version"], env=env, text=True, timeout=10
    ).strip()
    executable, installed = version.split(" ", 1)
    assert executable == "sai"
    old = root / ".sai-native" / f"{installed}-{'b' * 64}" / "sai"
    old.parent.mkdir(parents=True, mode=0o700)
    old.parent.parent.chmod(0o700)
    shutil.copy2(binary, old)
    selector = old.parent.parent / "current"
    selector.symlink_to(old.relative_to(selector.parent))
    launcher.symlink_to(selector.relative_to(root))
    return NativeInstall(launcher, old, home, env, installed)


def test_standalone_update_keeps_the_public_launcher(
    standalone: NativeInstall,
) -> None:
    public = standalone.launcher.lstat()
    initial = standalone.launcher.readlink()
    asset = assets(NEW, archive_name(), archive())
    ledger = standalone.home / "ledger-fixture"
    ledger.write_bytes(b"existing Sai state")
    with releases(asset) as server:
        checked = standalone.update(server, "--check")
        assert checked.returncode == 0, checked.stderr
        assert json.loads(checked.stdout)["install"]["kind"] == "standalone"
        assert server.reads == [f"/repos{REPO}/releases"]
        assert standalone.launcher.readlink() == initial
        result = standalone.update(server)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "updated"
        assert server.reads[2:] == list(reversed(asset))
    assert standalone.launcher.is_symlink()
    assert standalone.launcher.readlink() == initial
    assert os.path.samestat(standalone.launcher.lstat(), public)
    assert stat.S_IMODE(standalone.launcher.stat().st_mode) == 0o700
    assert standalone.invoke("--version").stdout.strip() == f"sai {NEW}"
    assert standalone.executable.is_file()
    assert ledger.read_bytes() == b"existing Sai state"


@pytest.mark.parametrize(
    ("manifest", "bad_entry"),
    [
        ("wrong", ""),
        ("duplicate", ""),
        ("valid", "symlink"),
        ("valid", "duplicate"),
        ("valid", "traversal"),
    ],
)
def test_invalid_assets_keep_the_working_launcher(
    standalone: NativeInstall, manifest: str, bad_entry: str
) -> None:
    initial = standalone.launcher.readlink()
    with releases(
        assets(NEW, archive_name(), archive(bad_entry=bad_entry), manifest=manifest)
    ) as server:
        result = standalone.update(server)
    assert result.returncode == 1
    assert "is unchanged" in json.loads(result.stdout)["error"]["message"]
    assert standalone.launcher.readlink() == initial
    assert {path.name for path in standalone.executable.parent.parent.iterdir()} == {
        standalone.executable.parent.name,
        "current",
    }


def test_update_refuses_cross_origin_asset_redirect(standalone: NativeInstall) -> None:
    published = assets(NEW, archive_name(), archive())
    path = asset_key(NEW, archive_name())
    with (
        releases(published) as other,
        releases({**published, path: Redirect(other.base + path)}) as server,
    ):
        result = standalone.update(server)
        assert not other.reads
    assert result.returncode == 1, result.stdout
    assert standalone.launcher.resolve() == standalone.executable


@pytest.mark.parametrize(
    ("name", "chunk"),
    [("SHA256SUMS", 1024), (archive_name(), 1 << 20)],
)
def test_update_bounds_streamed_release_assets(
    standalone: NativeInstall, name: str, chunk: int
) -> None:
    published = assets(NEW, archive_name(), archive())
    with releases(
        {**published, asset_key(NEW, name): Streamed(b"x" * chunk, 65)}
    ) as server:
        result = standalone.update(server)
    assert result.returncode == 1
    assert f"{name} is too large" in json.loads(result.stdout)["error"]["message"]
    assert standalone.launcher.resolve() == standalone.executable


def test_update_bounds_the_expanded_tar(standalone: NativeInstall) -> None:
    excess = io.BytesIO()
    zeroes = bytes(1 << 20)
    with gzip.GzipFile(fileobj=excess, mode="wb", compresslevel=1) as stream:
        for _ in range(98):
            stream.write(zeroes)
    payload = archive() + excess.getvalue()
    with releases(assets(NEW, archive_name(), payload)) as server:
        result = standalone.update(server)
    assert result.returncode == 1
    assert (
        "expanded native archive is too large"
        in json.loads(result.stdout)["error"]["message"]
    )
    assert standalone.launcher.resolve() == standalone.executable


@pytest.mark.parametrize("phase", ["trial", "installed"])
def test_failed_smoke_check_keeps_or_restores_standalone(
    standalone: NativeInstall, phase: str
) -> None:
    initial = standalone.launcher.readlink()
    with releases(assets(NEW, archive_name(), archive(fail=phase))) as server:
        result = standalone.update(server)
    assert result.returncode == 1
    assert "did not initialize" in json.loads(result.stdout)["error"]["message"]
    assert standalone.launcher.readlink() == initial
    assert (
        standalone.invoke("--version").stdout.strip() == f"sai {standalone.installed}"
    )
    assert standalone.state()["result"]["rolledBack"] is (phase == "installed")
    assert not (standalone.executable.parent.parent / "pending.json").exists()


def test_standalone_smoke_does_not_execute_a_replacement_launcher(
    standalone: NativeInstall, tmp_path: Path
) -> None:
    invoked = tmp_path / "competing-launcher-invoked"
    competitor = standalone.launcher.with_name("competitor")
    script = f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(invoked)!r}).touch()\n"
    competitor.write_text(script, encoding="utf-8")
    competitor.chmod(0o700)
    with releases(
        assets(NEW, archive_name(), archive(fail="replace-launcher"))
    ) as server:
        result = standalone.update(server)
    assert result.returncode == 1, result.stdout + result.stderr
    assert not invoked.exists()
    assert standalone.launcher.read_text(encoding="utf-8") == script
    assert (standalone.executable.parent.parent / "pending.json").exists()


def test_check_leaves_interrupted_install_for_next_mutating_update(
    standalone: NativeInstall,
) -> None:
    root = standalone.executable.parent.parent
    interrupted = f"{NEW}-{'a' * 64}"
    staged = root / interrupted / "sai"
    staged.parent.mkdir()
    shutil.copy2(standalone.executable, staged)
    selector = root / "current"
    selector.unlink()
    selector.symlink_to(staged.relative_to(root))
    record = root / "pending.json"
    owner = standalone.launcher.lstat()
    record.write_text(
        json.dumps(
            {
                "old": standalone.executable.parent.name,
                "new": interrupted,
                "launcher": [owner.st_dev, owner.st_ino],
            }
        ),
        encoding="utf-8",
    )
    with releases({}, tags=(standalone.installed,)) as server:
        checked = standalone.update(server, "--check")
        assert checked.returncode == 0, checked.stdout
        assert standalone.launcher.resolve() == staged
        assert record.exists()
        changed = standalone.update(server)
        assert changed.returncode == 0, changed.stdout
        assert json.loads(changed.stdout)["status"] == "current"
    assert standalone.launcher.resolve() == standalone.executable
    assert not record.exists()


@pytest.mark.parametrize("has_previous", [False, True], ids=["first", "upgrade"])
@pytest.mark.parametrize("record_identity", [True, False], ids=["recorded", "legacy"])
def test_interrupted_rollback_refuses_a_replacement_launcher(
    standalone: NativeInstall, has_previous: bool, record_identity: bool
) -> None:
    root = standalone.executable.parent.parent
    candidate = root / f"{NEW}-{'a' * 64}" / "sai"
    candidate.parent.mkdir()
    shutil.copy2(standalone.executable, candidate)
    selector = root / "current"
    selector.unlink()
    selector.symlink_to(candidate.relative_to(root))
    selected = selector.lstat()

    launcher = standalone.launcher
    original = launcher.lstat()
    link = launcher.readlink()
    record = root / "pending.json"
    journal: dict[str, str | list[int] | None] = {
        "old": standalone.executable.parent.name if has_previous else None,
        "new": candidate.parent.name,
    }
    if record_identity:
        journal["launcher"] = [original.st_dev, original.st_ino]
    pending = json.dumps(journal)
    record.write_text(pending, encoding="utf-8")
    launcher.rename(launcher.with_name("original-launcher"))
    launcher.symlink_to(link)
    replacement = launcher.lstat()
    assert not os.path.samestat(replacement, original)

    with releases({}) as server:
        result = standalone.update(server)
        assert not server.reads
    assert result.returncode == 1, result.stdout
    message = json.loads(result.stdout)["error"]["message"]
    if record_identity:
        assert "launcher changed during the interrupted install" in message
    else:
        assert "pending Sai install record has no launcher identity" in message
    assert os.path.samestat(launcher.lstat(), replacement)
    assert os.path.samestat(selector.lstat(), selected)
    assert launcher.resolve(strict=True) == candidate
    assert record.read_text(encoding="utf-8") == pending


@pytest.mark.parametrize("has_previous", [False, True], ids=["first", "upgrade"])
def test_ordinary_recovery_leaves_legacy_direct_launchers_in_place(
    standalone: NativeInstall, has_previous: bool
) -> None:
    root = standalone.executable.parent.parent
    candidate = root / f"{NEW}-{'a' * 64}" / "sai"
    candidate.parent.mkdir()
    shutil.copy2(standalone.executable, candidate)
    standalone.launcher.unlink()
    standalone.launcher.symlink_to(candidate.relative_to(standalone.launcher.parent))
    public = standalone.launcher.lstat()
    record = root / "pending.json"
    record.write_text(
        json.dumps(
            {
                "old": standalone.executable.parent.name if has_previous else None,
                "new": candidate.parent.name,
            }
        ),
        encoding="utf-8",
    )
    with releases({}, tags=(standalone.installed,)) as server:
        result = standalone.update(server)
        assert not server.reads
    assert result.returncode == 3, result.stdout
    assert (
        "no longer points to a managed Sai binary"
        in json.loads(result.stdout)["error"]["message"]
    )
    assert os.path.samestat(standalone.launcher.lstat(), public)
    assert standalone.launcher.resolve() == candidate
    assert record.exists()


@pytest.mark.parametrize("interruption", ["installer-staged", "rust-withdrew"])
@pytest.mark.parametrize("repopulated", [False, True], ids=["vacant", "new-owner"])
def test_update_recovers_a_staged_competing_launcher(
    standalone: NativeInstall, interruption: str, repopulated: bool
) -> None:
    def entry(path: Path) -> tuple[int, int, str]:
        metadata = path.lstat()
        return metadata.st_dev, metadata.st_ino, os.readlink(path)

    launcher = standalone.launcher
    root = standalone.executable.parent.parent
    new = f"{NEW}-{'a' * 64}"
    candidate = root / new / "sai"
    candidate.parent.mkdir()
    shutil.copy2(standalone.executable, candidate)
    stage = root / ".launcher-switch"
    stage.mkdir(mode=0o700)
    displaced = stage / "sai"
    competitor = launcher.with_name("competitor")
    competitor.write_text(
        "#!/bin/sh\nprintf 'competing launcher\\n'\n", encoding="utf-8"
    )
    competitor.chmod(0o700)

    expected = entry(launcher)
    launcher.unlink()
    launcher.symlink_to(competitor.name)
    launcher.rename(displaced)
    displaced_entry = entry(displaced)
    published = None
    if interruption == "rust-withdrew":
        launcher.symlink_to(candidate.relative_to(launcher.parent))
        published = entry(launcher)
        launcher.rename(stage / "public")
    assert not launcher.is_symlink()
    if repopulated:
        launcher.symlink_to(competitor)
        newer_entry = entry(launcher)

    record = root / "pending.json"
    pending = json.dumps(
        {
            "old": standalone.executable.parent.name,
            "new": new,
            "launcher_switch": {
                "staged": str(displaced.relative_to(root)),
                "expected": expected,
                "published": published,
            },
        }
    )
    record.write_text(pending, encoding="utf-8")

    with releases({}) as server:
        result = standalone.invoke(
            "--json", "update", *server.args, exe=standalone.executable
        )
        assert server.reads == []
    assert result.returncode == 1, result.stdout
    message = json.loads(result.stdout)["error"]["message"]
    assert "launcher changed during the switch" in message
    if repopulated:
        assert "displaced launcher is at" in message
        assert entry(launcher) == newer_entry
        assert entry(displaced) == displaced_entry
        assert record.read_text(encoding="utf-8") == pending
    else:
        assert entry(launcher) == displaced_entry
        assert standalone.invoke("--version").stdout == "competing launcher\n"
        assert not stage.exists()
        assert not record.exists()


def test_background_optout_keeps_pending_standalone_recovery(
    standalone: NativeInstall,
) -> None:
    root = standalone.executable.parent.parent
    interrupted = f"2.0.0-{'a' * 64}"
    staged = root / interrupted / "sai"
    staged.parent.mkdir()
    shutil.copy2(standalone.executable, staged)
    selector = root / "current"
    selector.unlink()
    selector.symlink_to(staged.relative_to(root))
    record = root / "pending.json"
    content = json.dumps({"old": standalone.executable.parent.name, "new": interrupted})
    record.write_text(content, encoding="utf-8")
    with releases({}) as server:
        result = standalone.update(server, "--background", SAI_UPDATE_AUTO="false")
        assert server.reads == [f"/repos{REPO}/releases"]
    assert result.returncode == 0 and result.stdout == ""
    assert standalone.launcher.resolve() == staged
    assert record.read_text(encoding="utf-8") == content
    assert standalone.state()["result"]["status"] == "available"


def test_only_explicit_targets_can_downgrade_or_select_prereleases(
    standalone: NativeInstall,
) -> None:
    previous, prerelease = "0.0.9", "0.2.0-rc.1"
    local_assets = {}
    for version in (previous, prerelease):
        local_assets.update(assets(version, archive_name(version), archive(version)))
    with releases(local_assets, tags=(previous, prerelease)) as server:
        current = standalone.update(server)
        assert current.returncode == 0, current.stdout
        assert json.loads(current.stdout)["status"] == "current"
        assert server.reads == [f"/repos{REPO}/releases"]
        result = standalone.update(server, "--to", prerelease)
        assert result.returncode == 0, result.stdout
        assert json.loads(result.stdout)["target"] == prerelease
        # The retained running binary still dispatches through its stable install.
        previous_result = standalone.invoke(
            "--json",
            "update",
            *server.args,
            "--to",
            previous,
            exe=standalone.executable,
        )
        assert previous_result.returncode == 0, previous_result.stdout
        assert json.loads(previous_result.stdout)["target"] == previous
        assert server.reads.count(f"/repos{REPO}/releases") == 1
    assert standalone.invoke("--version").stdout.strip() == f"sai {previous}"


@pytest.mark.parametrize("optout", ["env", "config", "during-download"])
def test_background_optout_rechecked_before_install(
    standalone: NativeInstall, optout: str
) -> None:
    config = standalone.home / "config.toml"
    if optout == "config":
        config.write_text("[update]\nauto = false\n", encoding="utf-8")
    entered, resume = threading.Event(), threading.Event()
    local_assets = assets(NEW, archive_name(), archive())
    pause = (
        (asset_key(NEW, archive_name()), entered, resume)
        if optout == "during-download"
        else None
    )
    with releases(local_assets, pause=pause) as server:
        args = (str(standalone.launcher), "update", "--background", *server.args)
        child_env = dict(standalone.env)
        if optout == "env":
            child_env["SAI_UPDATE_AUTO"] = "false"
        with subprocess.Popen(
            args,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                if pause:
                    assert entered.wait(10)
                    config.write_text("[update]\nauto = false\n", encoding="utf-8")
            finally:
                resume.set()
            assert process.communicate(timeout=15) == (b"", b"")
            assert process.returncode == 0
        if not pause:
            assert server.reads == [f"/repos{REPO}/releases"]
        assert standalone.state()["result"]["status"] == "available"
        assert standalone.launcher.resolve() == standalone.executable
        # An explicit update still installs while update.auto is false.
        result = standalone.update(server, SAI_UPDATE_AUTO="false")
        assert result.returncode == 0, result.stdout
        assert json.loads(result.stdout)["status"] == "updated"


def test_launcher_lock_spans_profiles_and_waiters_reread_the_install(
    standalone: NativeInstall, tmp_path: Path
) -> None:
    local_assets = assets(NEW, archive_name(), archive())
    entered, resume = threading.Event(), threading.Event()
    other = tmp_path / "other-profile"
    with releases(
        local_assets, pause=(asset_key(NEW, archive_name()), entered, resume)
    ) as server:
        args = [str(standalone.launcher), "--json", "update", *server.args]
        with subprocess.Popen(
            args, env=standalone.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as first:
            try:
                assert entered.wait(10)
                env = {**standalone.env, "SAI_HOME": str(other)}
                busy = standalone.update(server, "--background", SAI_HOME=str(other))
                assert busy.returncode == 0
                assert not other.exists()
                lock_path = standalone.launcher.parent / ".sai-update.lock"
                with lock_path.open("rb") as handle, pytest.raises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with subprocess.Popen(
                    args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                ) as second:
                    # Keep the first update locked until the second has opened the old binary.
                    if sys.platform == "linux":
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            try:
                                if (
                                    Path(f"/proc/{second.pid}/exe").resolve(strict=True)
                                    == standalone.executable
                                ):
                                    break
                            except FileNotFoundError:
                                pass
                            time.sleep(0.01)
                        else:
                            pytest.fail("Waiting updater did not start")
                    resume.set()
                    output, diagnostic = second.communicate(timeout=10)
                    assert second.returncode == 0, diagnostic
                    assert json.loads(output)["status"] == "current"
            finally:
                resume.set()
            output, diagnostic = first.communicate(timeout=10)
            assert first.returncode == 0, diagnostic
            assert json.loads(output)["status"] == "updated"
        assert server.reads.count(asset_key(NEW, archive_name())) == 1


def test_interactive_update_notice_and_schedule_are_reserved_once(
    standalone: NativeInstall,
) -> None:
    state = standalone.home / "update.json"
    state.write_text(
        json.dumps({"schema": 1, "result": {"status": "available", "to": NEW}}),
        encoding="utf-8",
    )
    env = {
        **standalone.env,
        "SAI_LOG": "true",
        "HTTPS_PROXY": "http://127.0.0.1:1",
    }
    del env["CI"]

    def interactive() -> str:
        master, slave = pty.openpty()
        try:
            result = subprocess.run(
                [standalone.launcher, "auth", "list"],
                env=env,
                stdin=slave,
                stderr=slave,
                stdout=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            assert result.returncode == 0
            os.set_blocking(master, False)
            try:
                return os.read(master, 4096).decode("utf-8")
            except BlockingIOError:
                return ""
        finally:
            os.close(slave)
            os.close(master)

    # The detached worker takes this same lock. Keep it offline and observe its
    # busy result before releasing it.
    lock = standalone.launcher.parent / ".sai-update.lock"
    with lock.open("wb") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert f"sai {NEW} is available" in interactive()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if any(
                '"status":"busy"' in path.read_text(encoding="utf-8")
                for path in (standalone.home / "logs").glob("update-*.jsonl")
            ):
                break
            time.sleep(0.025)
        else:
            pytest.fail("the detached updater never tried the launcher lock")
        initial = standalone.state()
        assert isinstance(initial["scheduledAt"], float)
        assert initial["notified"] == f"available:{NEW}"
        assert "startedAt" not in initial
        assert f"sai {NEW} is available" not in interactive()
        assert standalone.state() == initial


def test_shadowing_reports_both_paths_without_fetching(
    standalone: NativeInstall, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "another bin"
    elsewhere.mkdir()
    shadow = elsewhere / "sai"
    shadow.symlink_to(standalone.executable)
    with releases({}) as server:
        result = standalone.update(server, PATH=f"{elsewhere}:{standalone.env['PATH']}")
        assert not server.reads
    assert result.returncode == 3
    message = json.loads(result.stdout)["error"]["message"]
    assert str(shadow) in message and str(standalone.launcher) in message
    assert standalone.launcher.resolve() == standalone.executable


def uv_install(standalone: NativeInstall, tmp_path: Path) -> tuple[NativeInstall, Path]:
    uv = shutil.which(os.environ.get("UV", "uv")) or resolve_uv()
    if Path(uv).resolve().name == "mise":
        uv = resolve_uv()
    bins = tmp_path / "uv bin"
    bins.mkdir()
    (bins / "uv").symlink_to(uv)
    tools = tmp_path / "uv tools"
    cache = standalone.home / "bin"
    cache.mkdir(mode=0o700)
    candidate = cache / "sai-native"
    shutil.copy2(standalone.executable, candidate)
    env = {
        **standalone.env,
        "PATH": os.pathsep.join([str(bins), os.defpath]),
        "UV_TOOL_DIR": str(tools),
        "UV_TOOL_BIN_DIR": str(bins),
        "UV_CACHE_DIR": str(tmp_path / "uv cache"),
        "UV_NO_CONFIG": "1",
        "FIXTURE_NATIVE_BINARY": str(candidate),
        "FIXTURE_STABLE_UV_PREFIX": str(tools / "sai-cli"),
    }
    initial = wheel(tmp_path, standalone.installed, forward=True)
    result = subprocess.run(
        [uv, "tool", "install", "--offline", "--python", sys.executable, initial],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    prefix = tools / "sai-cli"
    (record_file,) = prefix.glob("lib/python*/site-packages/sai_cli-*.dist-info/RECORD")
    (tools / ".sai-cli-release.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "tag": f"sai-cli/v{standalone.installed}",
                "sha256": hashlib.sha256(initial.read_bytes()).hexdigest(),
                "installed": hashlib.sha256(record_file.read_bytes()).hexdigest(),
                "wheel": str(initial),
            }
        ),
        encoding="utf-8",
    )
    launcher = bins / "sai"
    return NativeInstall(
        launcher, candidate, standalone.home, env, standalone.installed
    ), initial


def cached_shim_release(
    install: NativeInstall, initial: Path, tmp_path: Path
) -> dict[str, bytes]:
    replacement = wheel(tmp_path, NEW, cached=True)
    wheel_assets = assets(NEW, replacement.name, replacement.read_bytes())
    native_assets = assets(NEW, archive_name(), archive())
    manifest = asset_key(NEW, "SHA256SUMS")
    native_assets[manifest] += wheel_assets[manifest]
    return {
        **assets(install.installed, initial.name, initial.read_bytes()),
        **wheel_assets,
        **native_assets,
    }


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
def test_background_uv_update_respects_the_shared_launcher_lock(
    standalone: NativeInstall, tmp_path: Path
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    replacement = wheel(tmp_path, NEW)
    local_assets = {
        **assets(install.installed, initial.name, initial.read_bytes()),
        **assets(NEW, replacement.name, replacement.read_bytes()),
    }
    lock = install.launcher.with_name(".sai-update.lock")
    lock.touch(mode=0o600)
    with lock.open("rb") as held, releases(local_assets) as server:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = install.update(server, "--background", "--to", NEW)
        assert not server.reads
    assert result.returncode == 0, result.stdout + result.stderr
    assert install.invoke("--version").stdout.strip() == f"sai {install.installed}"


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
def test_uv_trial_home_is_private_under_a_group_writable_umask(
    standalone: NativeInstall, tmp_path: Path
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    with releases(cached_shim_release(install, initial, tmp_path)) as server:
        result = install.invoke("--json", "update", *server.args, umask=0o002)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "updated"


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
@pytest.mark.parametrize("relative_home", [False, True])
def test_uv_upgrade_can_launch_offline_in_the_updating_profile(
    standalone: NativeInstall, tmp_path: Path, relative_home: bool
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    profile = install.home.name if relative_home else str(install.home)
    with releases(cached_shim_release(install, initial, tmp_path)) as server:
        updated = install.invoke(
            "--json", "update", *server.args, umask=0o077, SAI_HOME=profile
        )
        assert updated.returncode == 0, updated.stdout + updated.stderr
        assert json.loads(updated.stdout)["status"] == "updated"
    assert not (install.home / install.home.name / "bin").exists()
    offline = install.invoke(
        "--version", SAI_HOME=profile, SAI_NATIVE_RELEASE_BASE=server.base
    )
    assert offline.returncode == 0, offline.stderr
    assert offline.stdout.strip() == f"sai {NEW}"


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
@pytest.mark.parametrize("provenance", ["missing", "stale"])
def test_uv_custom_wheel_is_reported_and_left_to_its_installer(
    standalone: NativeInstall, tmp_path: Path, provenance: str
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    tools = Path(install.env["UV_TOOL_DIR"])
    record = tools / ".sai-cli-release.json"
    if provenance == "missing":
        record.unlink()
    else:
        saved = json.loads(record.read_text(encoding="utf-8"))
        saved["installed"] = "c" * 64
        record.write_text(json.dumps(saved), encoding="utf-8")
    link = install.launcher.readlink()
    replacement = wheel(tmp_path, NEW)
    local_assets = {
        **assets(install.installed, initial.name, initial.read_bytes()),
        **assets(NEW, replacement.name, replacement.read_bytes()),
    }
    with releases(local_assets) as server:
        prefix = tools / "sai-cli"
        reference = python_update.install_kind(prefix, server.base)
        assert reference.kind == "uv-tool-other"
        checked = install.update(server, "--check")
        assert checked.returncode == 0, checked.stdout
        assert json.loads(checked.stdout)["install"]["kind"] == reference.kind
        before = server.reads.copy()
        changed = install.update(server, "--to", NEW)
        assert changed.returncode == 3, changed.stdout
        assert server.reads == before
    assert install.launcher.readlink() == link
    assert install.invoke("--version").stdout.strip() == f"sai {install.installed}"


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
@pytest.mark.parametrize("authority", ["github.com", "github.com:443", "GITHUB.COM"])
def test_uv_release_url_uses_python_authority_matching(
    standalone: NativeInstall, tmp_path: Path, authority: str
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    prefix = Path(install.env["FIXTURE_STABLE_UV_PREFIX"])
    (origin,) = prefix.glob(
        "lib/python*/site-packages/sai_cli-*.dist-info/direct_url.json"
    )
    origin.write_text(
        json.dumps(
            {
                "url": f"https://{authority}{asset_key(install.installed, initial.name)}",
                "archive_info": {},
            }
        ),
        encoding="utf-8",
    )
    with releases({}) as server:
        result = install.invoke(
            "--json", "update", "--check", "--api-base", server.base
        )
    assert result.returncode == 0, result.stdout
    assert (
        json.loads(result.stdout)["install"]["kind"]
        == python_update.install_kind(prefix).kind
    )


def interrupted_uv_install(install: NativeInstall, initial: Path) -> Path:
    tools = Path(install.env["UV_TOOL_DIR"])
    digest = hashlib.sha256(initial.read_bytes()).hexdigest()
    cached = tools / ".sai-cli-native-wheels" / digest / initial.name
    cached.parent.mkdir(parents=True, mode=0o700)
    shutil.copyfile(initial, cached)
    pending = tools / ".sai-cli-native-pending.json"
    pending.write_text(
        json.dumps(
            {
                "version": install.installed,
                "digest": digest,
                "launcher": str(install.launcher),
                "python": str(Path(sys.executable).resolve()),
            }
        ),
        encoding="utf-8",
    )
    return pending


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
@pytest.mark.parametrize(
    ("legacy_record", "leftover"),
    [
        (False, "none"),
        (True, "none"),
        (False, "empty"),
        (False, "partial"),
        (False, "foreign"),
    ],
)
def test_uv_recovers_an_interrupted_in_place_bridge_install(
    standalone: NativeInstall, tmp_path: Path, legacy_record: bool, leftover: str
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    prefix = Path(install.env["FIXTURE_STABLE_UV_PREFIX"])
    pending = interrupted_uv_install(install, initial)
    record = json.loads(pending.read_text(encoding="utf-8"))
    (installed_record,) = prefix.glob(
        "lib/python*/site-packages/sai_cli-*.dist-info/RECORD"
    )
    if not legacy_record:
        record["original_record"] = hashlib.sha256(
            installed_record.read_bytes()
        ).hexdigest()
    pending.write_text(json.dumps(record), encoding="utf-8")
    if leftover == "foreign":
        foreign_dir = tmp_path / "foreign"
        foreign_dir.mkdir()
        foreign = wheel(foreign_dir, install.installed, fail="installed")
        replaced = subprocess.run(
            [
                install.launcher.with_name("uv"),
                "tool",
                "install",
                "--force",
                "--offline",
                "--python",
                sys.executable,
                foreign,
            ],
            env=install.env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert replaced.returncode == 0, replaced.stderr
    receipt = prefix / "uv-receipt.toml"
    receipt_bytes = receipt.read_bytes()
    link = install.launcher.readlink()
    if leftover == "none":
        removed = subprocess.run(
            [
                install.launcher.with_name("uv"),
                "pip",
                "uninstall",
                "--python",
                prefix / "bin/python",
                "sai-cli",
            ],
            env=install.env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert removed.returncode == 0, removed.stderr
    else:
        (prefix / "bin/sai").unlink()
        (installed_record.parent / "METADATA").unlink()
        if leftover == "empty":
            for entry in installed_record.parent.iterdir():
                entry.unlink()
        assert installed_record.parent.is_dir()
    assert receipt.read_bytes() == receipt_bytes
    assert install.launcher.readlink() == link
    assert not install.launcher.exists()
    assert installed_record.exists() is (leftover in {"partial", "foreign"})

    recovered = install.invoke("--json", "update", "--recover", exe=install.executable)
    if leftover == "foreign":
        assert recovered.returncode == 3, recovered.stdout + recovered.stderr
        assert pending.exists()
        assert not install.launcher.exists()
        assert receipt.read_bytes() == receipt_bytes
        return
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert json.loads(recovered.stdout)["status"] == "recovered"
    assert install.launcher.readlink() == link
    assert not pending.exists()
    assert install.invoke("--version").stdout.strip() == f"sai {install.installed}"


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
def test_uv_tool_upgrade_keeps_the_rolled_back_wheel(
    standalone: NativeInstall, tmp_path: Path
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    pending = interrupted_uv_install(install, initial)
    journal = json.loads(pending.read_text(encoding="utf-8"))
    candidate = wheel(tmp_path, NEW, fail="installed")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    cached = pending.parent / ".sai-cli-native-wheels" / digest / candidate.name
    cached.parent.mkdir(parents=True, mode=0o700)
    shutil.copyfile(candidate, cached)
    journal["next"] = {"version": NEW, "digest": digest}
    pending.write_text(json.dumps(journal), encoding="utf-8")
    uv = install.launcher.with_name("uv")
    changed = subprocess.run(
        [
            uv,
            "tool",
            "install",
            "--force",
            "--offline",
            "--python",
            sys.executable,
            cached,
        ],
        env=install.env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert changed.returncode == 0, changed.stderr
    assert install.invoke("--version").stdout.strip() == f"sai {NEW}"
    public_inode = install.launcher.lstat().st_ino

    recovered = install.invoke("--json", "update", "--recover", exe=install.executable)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert install.launcher.lstat().st_ino == public_inode
    assert not pending.exists()
    assert install.invoke("--version").stdout.strip() == f"sai {install.installed}"

    upgraded = subprocess.run(
        [uv, "tool", "upgrade", "--offline", "sai-cli"],
        env=install.env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert upgraded.returncode == 0, upgraded.stderr
    assert install.invoke("--version").stdout.strip() == f"sai {install.installed}"


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
@pytest.mark.parametrize("cooperative", [True, False])
def test_uv_recovery_preserves_a_competing_public_launcher(
    standalone: NativeInstall, tmp_path: Path, cooperative: bool
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    prefix = Path(install.env["FIXTURE_STABLE_UV_PREFIX"])
    pending = interrupted_uv_install(install, initial)
    install.launcher.unlink()
    shutil.rmtree(prefix)
    lock = install.launcher.with_name(".sai-update.lock")
    lock.touch(mode=0o600)
    wrapper = install.launcher.with_name("uv")
    real_uv = wrapper.resolve()
    wrapper.unlink()
    attempted = tmp_path / "publication-attempt"
    wrapper.write_text(
        f"""#!{sys.executable}
import fcntl
import os
import sys
from pathlib import Path

if sys.argv[1:3] == ["tool", "install"] and not Path({str(attempted)!r}).exists():
    with Path({str(lock)!r}).open("rb") as held:
        try:
            if {cooperative!r}:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            Path({str(attempted)!r}).write_text("locked")
        else:
            Path({str(install.launcher)!r}).symlink_to({str(standalone.executable)!r})
            Path({str(attempted)!r}).write_text("published")
os.execv({str(real_uv)!r}, [{str(real_uv)!r}, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    recovered = install.invoke("--json", "update", "--recover", exe=install.executable)
    assert attempted.read_text(encoding="utf-8") == (
        "locked" if cooperative else "published"
    )
    if cooperative:
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    else:
        assert recovered.returncode == 1, recovered.stdout + recovered.stderr
        assert install.launcher.readlink() == standalone.executable
        assert pending.exists()
        receipt = tomllib.loads(
            (prefix / "uv-receipt.toml").read_text(encoding="utf-8")
        )
        assert receipt["tool"]["entrypoints"][0]["install-path"] != str(
            install.launcher
        )
        install.launcher.unlink()
        retried = install.invoke(
            "--json", "update", "--recover", exe=install.executable
        )
        assert retried.returncode == 0, retried.stdout + retried.stderr
    assert install.launcher.readlink() == prefix / "bin/sai"
    assert not pending.exists()
    assert install.invoke("--version").stdout.strip() == f"sai {install.installed}"


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
@pytest.mark.parametrize("environment_missing", [False, True])
def test_uv_recovery_entry_point_does_not_need_a_shim_prefix(
    standalone: NativeInstall, tmp_path: Path, environment_missing: bool
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    assert "SAI_NATIVE_UV_PREFIX" not in install.env
    record = interrupted_uv_install(install, initial)
    link = install.launcher.readlink()
    install.launcher.unlink()
    if environment_missing:
        shutil.rmtree(Path(install.env["FIXTURE_STABLE_UV_PREFIX"]))

    with releases({}) as server:
        recovered = install.invoke(
            "--json",
            "update",
            "--recover",
            *server.args,
            exe=standalone.executable,
        )
        assert not server.reads
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        assert json.loads(recovered.stdout)["status"] == "recovered"
        assert install.launcher.readlink() == link
        assert install.invoke("--version").stdout.strip() == f"sai {install.installed}"
        assert not record.exists()
        repeated = install.invoke(
            "--json", "update", "--recover", *server.args, exe=standalone.executable
        )
        assert repeated.returncode == 3, repeated.stdout + repeated.stderr
        assert not server.reads


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
@pytest.mark.parametrize("different_cwd", [False, True])
def test_uv_recovery_uses_recorded_cwd_for_relative_bin_directory(
    standalone: NativeInstall, tmp_path: Path, different_cwd: bool
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    record = interrupted_uv_install(install, initial)
    value = json.loads(record.read_text(encoding="utf-8"))
    value["cwd"] = str(tmp_path)
    record.write_text(json.dumps(value), encoding="utf-8")
    install.launcher.unlink()
    resume = tmp_path
    if different_cwd:
        resume = tmp_path / "another cwd"
        resume.mkdir()
    with releases({}) as server:
        recovered = install.invoke(
            "--json",
            "update",
            *server.args,
            "--to",
            install.installed,
            exe=install.executable,
            cwd=resume,
            SAI_NATIVE_UV_PREFIX=install.env["FIXTURE_STABLE_UV_PREFIX"],
            UV_TOOL_BIN_DIR=install.launcher.parent.name,
        )
        assert not server.reads
    assert not (install.home / install.launcher.parent.name / "sai").is_symlink()
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert install.launcher.is_symlink()
    assert not record.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
@pytest.mark.parametrize("occupied", [False, True])
def test_uv_pending_recovery_distinguishes_partial_metadata_from_another_package(
    standalone: NativeInstall, tmp_path: Path, occupied: bool
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    prefix = Path(install.env["FIXTURE_STABLE_UV_PREFIX"])
    record = interrupted_uv_install(install, initial)
    if occupied:
        custom = tmp_path / "custom wheel"
        custom.mkdir()
        replacement = wheel(custom, install.installed, fail="installed", forward=True)
        result = subprocess.run(
            [
                install.launcher.parent / "uv",
                "tool",
                "install",
                "--force",
                "--offline",
                "--python",
                sys.executable,
                replacement,
            ],
            env=install.env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    else:
        (prefix / "uv-receipt.toml").unlink()
        (origin,) = prefix.glob(
            "lib/python*/site-packages/sai_cli-*.dist-info/direct_url.json"
        )
        origin.unlink()
    with releases({}) as server:
        result = install.invoke(
            "--json",
            "update",
            *server.args,
            "--to",
            install.installed,
            exe=install.executable,
            SAI_NATIVE_UV_PREFIX=str(prefix),
        )
        assert not server.reads
    assert result.returncode == (3 if occupied else 0), result.stdout + result.stderr
    assert record.exists() is occupied
    expected = "uv-tool-other" if occupied else "uv-tool-release"
    assert python_update.install_kind(prefix).kind == expected


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
def test_background_optout_keeps_pending_uv_recovery(
    standalone: NativeInstall, tmp_path: Path
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    record = interrupted_uv_install(install, initial)
    content = record.read_bytes()
    install.launcher.unlink()
    with releases({}) as server:
        result = install.invoke(
            "update",
            "--background",
            *server.args,
            exe=install.executable,
            SAI_NATIVE_UV_PREFIX=install.env["FIXTURE_STABLE_UV_PREFIX"],
            SAI_UPDATE_AUTO="false",
        )
        assert server.reads == [f"/repos{REPO}/releases"]
    assert result.returncode == 0 and result.stdout == ""
    assert not install.launcher.is_symlink()
    assert record.read_bytes() == content
    assert install.state()["result"]["status"] == "available"


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
def test_background_uv_optout_while_validating_recovery_destination(
    standalone: NativeInstall, tmp_path: Path
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    record = interrupted_uv_install(install, initial)
    content = record.read_bytes()
    install.launcher.unlink()
    wrapper = install.launcher.parent / "uv"
    real_uv = wrapper.resolve()
    wrapper.unlink()
    entered = tmp_path / "uv-destination-entered"
    resume = tmp_path / "uv-destination-resume"
    wrapper.write_text(
        f"""#!{sys.executable}
import os
import sys
import time
from pathlib import Path

if sys.argv[1:] == ["tool", "dir", "--bin"]:
    Path({str(entered)!r}).touch()
    deadline = time.monotonic() + 10
    while not Path({str(resume)!r}).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
os.execv({str(real_uv)!r}, [{str(real_uv)!r}, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    with (
        releases({}) as server,
        subprocess.Popen(
            [install.executable, "update", "--background", *server.args],
            env={
                **install.env,
                "SAI_NATIVE_UV_PREFIX": install.env["FIXTURE_STABLE_UV_PREFIX"],
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as child,
    ):
        try:
            deadline = time.monotonic() + 5
            while not entered.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert entered.exists()
            (install.home / "config.toml").write_text(
                "[update]\nauto = false\n", encoding="utf-8"
            )
        finally:
            resume.touch()
        assert child.communicate(timeout=15) == (b"", b"")
        assert child.returncode == 0
    assert not install.launcher.is_symlink()
    assert record.read_bytes() == content
    assert install.state()["result"]["status"] == "available"


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
def test_background_uv_optout_during_download_discards_staged_wheels(
    standalone: NativeInstall, tmp_path: Path
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    replacement = wheel(tmp_path, NEW)
    local_assets = {
        **assets(install.installed, initial.name, initial.read_bytes()),
        **assets(NEW, replacement.name, replacement.read_bytes()),
    }
    entered, resume = threading.Event(), threading.Event()
    pause = (asset_key(NEW, replacement.name), entered, resume)
    with (
        releases(local_assets, pause=pause) as server,
        subprocess.Popen(
            [install.launcher, "update", "--background", *server.args],
            env=install.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as child,
    ):
        try:
            assert entered.wait(10)
            (install.home / "config.toml").write_text(
                "[update]\nauto = false\n", encoding="utf-8"
            )
        finally:
            resume.set()
        assert child.communicate(timeout=15) == (b"", b"")
        assert child.returncode == 0
    assert install.state()["result"]["status"] == "available"
    assert install.invoke("--version").stdout.strip() == f"sai {install.installed}"
    assert not (Path(install.env["UV_TOOL_DIR"]) / ".sai-cli-native-wheels").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
@pytest.mark.parametrize(
    "interrupted",
    [
        "launcher",
        "receipt",
        "interpreter",
        "environment",
        "dangling-with-path-fallback",
        "foreign-launcher",
    ],
)
def test_uv_recovers_an_interrupted_install(
    standalone: NativeInstall, tmp_path: Path, interrupted: str
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    prefix = Path(install.env["FIXTURE_STABLE_UV_PREFIX"])
    record = interrupted_uv_install(install, initial)
    link = install.launcher.readlink()
    if interrupted in {"launcher", "environment", "foreign-launcher"}:
        install.launcher.unlink()
    if interrupted == "receipt":
        (prefix / "uv-receipt.toml").unlink()
    if interrupted == "interpreter":
        (prefix / "bin/python").unlink()
    if interrupted in {"environment", "dangling-with-path-fallback"}:
        shutil.rmtree(prefix)
    if interrupted == "foreign-launcher":
        install.launcher.symlink_to(standalone.executable)

    search_path = install.env["PATH"]
    if interrupted == "dangling-with-path-fallback":
        search_path += f":{standalone.launcher.parent}"
    with releases({}) as server:
        result = install.invoke(
            "--json",
            "update",
            *server.args,
            "--to",
            install.installed,
            exe=install.executable,
            SAI_NATIVE_UV_PREFIX=str(prefix),
            PATH=search_path,
        )
        assert not server.reads
    if interrupted == "foreign-launcher":
        assert result.returncode == 3, result.stdout
        assert install.launcher.resolve() == standalone.executable
        assert record.exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["status"] == "current"
        assert install.launcher.readlink() == link
        assert (prefix / "uv-receipt.toml").is_file()
        assert install.invoke("--version").stdout.strip() == f"sai {install.installed}"
        assert not record.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="parity uv resolver requires ELF")
@pytest.mark.parametrize("fail", ["", "installed"])
def test_uv_owned_launcher_is_reinstalled_or_restored_by_uv(
    standalone: NativeInstall, tmp_path: Path, fail: str
) -> None:
    install, initial = uv_install(standalone, tmp_path)
    uv = str(install.launcher.parent / "uv")
    launcher, env = install.launcher, install.env
    tools = Path(env["UV_TOOL_DIR"])
    replacement = wheel(tmp_path, NEW, fail=fail)
    link = launcher.readlink()
    local_assets = {
        **assets(standalone.installed, initial.name, initial.read_bytes()),
        **assets(NEW, replacement.name, replacement.read_bytes()),
    }
    with releases(local_assets) as server:
        report = install.update(server, "--check")
        assert report.returncode == 0, report.stdout
        assert json.loads(report.stdout)["install"]["kind"] == "uv-tool-release"
        before = server.reads.copy()
        refused = install.update(
            server, "--to", NEW, UV_TOOL_DIR=str(tmp_path / "wrong tools")
        )
        assert refused.returncode == 3, refused.stdout
        assert server.reads == before
        assert launcher.readlink() == link
        if not fail:
            pinned = subprocess.run(
                [
                    uv,
                    "tool",
                    "run",
                    "--offline",
                    "--isolated",
                    "--python",
                    sys.executable,
                    "--from",
                    initial,
                    "sai",
                    "--json",
                    "update",
                    *server.args,
                    "--to",
                    NEW,
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert pinned.returncode == 3, pinned.stdout + pinned.stderr
            assert server.reads == before
        changed = install.update(server)
        assert server.reads.count(asset_key(standalone.installed, initial.name)) == 1, (
            changed.stdout
        )
        assert server.reads.count(asset_key(NEW, replacement.name)) == 1
    assert launcher.is_symlink() and launcher.readlink() == link
    assert (tools / "sai-cli" / "uv-receipt.toml").is_file()
    assert not (tools / ".sai-cli-native-pending.json").exists()
    assert python_update.install_kind(tools / "sai-cli").kind == "uv-tool-release"
    if fail:
        assert changed.returncode == 1
        assert "Rolled back" in json.loads(changed.stdout)["error"]["message"]
        assert install.state()["result"]["rolledBack"] is True
        assert (
            install.invoke("--version").stdout.strip() == f"sai {standalone.installed}"
        )
    else:
        assert changed.returncode == 0, changed.stdout
        assert json.loads(changed.stdout)["status"] == "updated"
        assert install.invoke("--version").stdout.strip() == f"sai {NEW}"
