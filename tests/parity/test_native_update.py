"""Exercise native updates with local assets and real launcher ownership."""

from __future__ import annotations

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
import zipfile
from collections.abc import Generator
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
    if args == ["--version"]:
        print(f"sai {VERSION}")
    elif args == ["config", "get", "update.auto"]:
        print("true")
    elif args == ["mcp", "serve"]:
        in_stable = sys.argv[0] == os.environ.get("FIXTURE_STABLE_LAUNCHER")
        in_stable = in_stable or sys.prefix == os.environ.get("FIXTURE_STABLE_UV_PREFIX")
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


def wheel(tmp: Path, version: str, *, fail: str = "", forward: bool = False) -> Path:
    wheel_version = version.replace("-rc.", "rc")
    filename = f"sai_cli-{wheel_version}-py3-none-any.whl"
    name = f"sai_cli-{wheel_version}.dist-info"
    files = {
        "fixture_sai.py": executable(version, fail=fail, forward=forward),
        f"{name}/METADATA": f"Metadata-Version: 2.1\nName: sai-cli\nVersion: {wheel_version}\n",
        f"{name}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{name}/entry_points.txt": "[console_scripts]\nsai = fixture_sai:run\n",
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


@contextmanager
def releases(
    files: dict[str, bytes],
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
        self, *args: str, exe: Path | None = None, **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [exe or self.launcher, *args],
            env={**self.env, **overrides},
            cwd=self.home.parent,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
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
    launcher.symlink_to(old.relative_to(root))
    return NativeInstall(launcher, old, home, env, installed)


def test_standalone_update_replaces_only_the_launcher(
    standalone: NativeInstall,
) -> None:
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
    assert standalone.launcher.readlink() != initial
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
    assert sorted(
        path.name for path in standalone.executable.parent.parent.iterdir()
    ) == [standalone.executable.parent.name]


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


def test_check_leaves_interrupted_install_for_next_mutating_update(
    standalone: NativeInstall,
) -> None:
    root = standalone.executable.parent.parent
    interrupted = f"{NEW}-{'a' * 64}"
    staged = root / interrupted / "sai"
    staged.parent.mkdir()
    shutil.copy2(standalone.executable, staged)
    standalone.launcher.unlink()
    standalone.launcher.symlink_to(staged.relative_to(standalone.launcher.parent))
    record = root / "pending.json"
    record.write_text(
        json.dumps({"old": standalone.executable.parent.name, "new": interrupted}),
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
    cache.mkdir()
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
@pytest.mark.parametrize(
    "interrupted",
    [
        "launcher",
        "receipt",
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
