"""C10a distribution parity through real uv tool installs and loopback assets."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import runpy
import shutil
import signal
import socket
import stat
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "0.1.1"
# Published full Python release, which predates the handoff to native Sai.
BASELINE_COMMIT = "9ee5a356fd41a22697483e467652d367035909bd"
MCP_PENDING = "Native MCP startup requires C8 (PR #120)"


@pytest.fixture
def distribution(tmp_path: Path) -> SimpleNamespace:
    archive_env = os.environ.get("SAI_NATIVE_ARCHIVE")
    wheel_env = os.environ.get("SAI_NATIVE_WHEEL")
    if not archive_env and not wheel_env:
        pytest.skip(
            "set SAI_NATIVE_ARCHIVE and SAI_NATIVE_WHEEL to the built artifacts"
        )
    assert archive_env and wheel_env, "both native distribution artifacts are required"
    archive, wheel = (
        Path(archive_env).resolve(strict=True),
        Path(wheel_env).resolve(strict=True),
    )
    match = re.fullmatch(r"sai_cli-(\d+\.\d+\.\d+)-py3-none-any\.whl", wheel.name)
    assert match, "expected a stable sai-cli shim wheel"
    version = match[1]
    assert archive.name.startswith(f"sai-{version}-")
    uv = os.environ.get("UV") or shutil.which("uv")
    assert uv, "a real uv executable is required"
    rehearsal = SimpleNamespace(
        **runpy.run_path(str(ROOT / "scripts" / "release-rehearsal.py"))
    )
    tool = rehearsal.Install(tmp_path, clients=(), uv=Path(uv).resolve(strict=True))
    tool.env.update(UV_PYTHON=sys.executable, UV_PYTHON_DOWNLOADS="never")
    replay = rehearsal.Replay(wheel, version)
    return SimpleNamespace(
        archive=archive,
        wheel=wheel,
        version=version,
        tool=tool,
        replay=replay,
        rehearsal=rehearsal,
        release=f"/{rehearsal.REPO}/releases/download/sai-cli/v{version}",
    )


def run(tool: Any, step: str, *args: str | Path, **kwargs: Any) -> str:
    result = tool.run(step, *args, check=False, timeout=90, **kwargs)
    assert result.returncode == 0, (step, result.stdout, result.stderr)
    return result.stdout


def offline_env(distribution: SimpleNamespace, base: str, proxy: socket.socket) -> None:
    proxy.bind(("127.0.0.1", 0))
    distribution.tool.env.update(
        distribution.rehearsal.loopback_env(base, proxy.getsockname()[1])
    )
    distribution.tool.env["SAI_NATIVE_RELEASE_BASE"] = base


def verify_mcp_startup(distribution: SimpleNamespace) -> None:
    d = distribution
    command = (d.tool.sai, "mcp", "serve")
    initialize = {"jsonrpc": "2.0", **d.rehearsal.MCP_REQUESTS[0]}
    try:
        with d.tool.stdio("Initialize native MCP", *command) as (send, receive):
            send.write((json.dumps(initialize) + "\n").encode())
            send.flush()
            reply = json.loads(receive.line("MCP initialize"))
    except (d.rehearsal.RehearsalError, BrokenPipeError):
        # MCP registration is available before C8 adds the serve subcommand.
        # Once the server module lands, a missing subcommand fails this test.
        if not (ROOT / "sai" / "src" / "mcp.rs").is_file():
            probe = d.tool.run("Check missing MCP command", *command, check=False)
            if (
                probe.returncode == 2
                and not probe.stdout
                and "No such command 'serve'." in probe.stderr
            ):
                pytest.xfail(MCP_PENDING)
        raise
    assert reply["jsonrpc"] == "2.0" and reply["id"] == initialize["id"]
    assert reply["result"]["protocolVersion"]
    assert reply["result"]["serverInfo"]["name"]


def test_native_archive_initializes_mcp(distribution: SimpleNamespace) -> None:
    d = distribution
    d.replay.add_release(d.wheel, d.version, d.archive)
    with (
        d.rehearsal.keeper_server(d.replay.handler()) as base,
        closing(socket.socket()) as proxy,
    ):
        offline_env(d, base, proxy)
        run(d.tool, "Install the shim wheel", d.tool.uv, "tool", "install", d.wheel)
        verify_mcp_startup(d)


def write_shell_probe_archive(probe_archive: Path) -> None:
    shell = Path("/bin/sh")
    member = tarfile.TarInfo("sai")
    member.size = shell.stat().st_size
    with gzip.open(probe_archive, "wb") as archive, shell.open("rb") as binary:
        archive.write(member.tobuf(format=tarfile.USTAR_FORMAT))
        shutil.copyfileobj(binary, archive)
        archive.write(bytes(-member.size % tarfile.BLOCKSIZE + 2 * tarfile.BLOCKSIZE))


@pytest.mark.parametrize("launcher", ["uv-tool", "uvx"])
def test_shim_passes_the_launching_uv_prefix(
    distribution: SimpleNamespace, tmp_path: Path, launcher: str
) -> None:
    d = distribution
    tool = d.tool
    probe_archive = tmp_path / d.archive.name
    write_shell_probe_archive(probe_archive)
    d.replay.add_release(d.wheel, d.version, probe_archive)
    with (
        d.rehearsal.keeper_server(d.replay.handler()) as base,
        closing(socket.socket()) as proxy,
    ):
        offline_env(d, base, proxy)
        run(tool, "Install the shim wheel", tool.uv, "tool", "install", d.wheel)
        persistent_prefix = tool.python.parent.parent
        if launcher == "uv-tool":
            tool.env["SAI_NATIVE_UV_PREFIX"] = str(tmp_path / "stale-prefix")
            command = (tool.sai,)
            probe = 'test "$SAI_NATIVE_UV_PREFIX" = "$1"'
        else:
            tool.env["SAI_NATIVE_UV_PREFIX"] = str(persistent_prefix)
            command = (
                tool.uv,
                "tool",
                "run",
                "--isolated",
                "--no-index",
                "--find-links",
                d.wheel.parent,
                "--from",
                f"sai-cli=={d.version}",
                "sai",
            )
            probe = (
                'test "$SAI_NATIVE_UV_PREFIX" != "$1" && '
                'test ! -e "$SAI_NATIVE_UV_PREFIX/uv-receipt.toml" && '
                '"$SAI_NATIVE_UV_PREFIX/bin/python" -I -c '
                "'import os, sys; sys.exit(os.environ[\"SAI_NATIVE_UV_PREFIX\"] != sys.prefix)'"
            )
        run(
            tool,
            "Check the prefix in the exec environment",
            *command,
            "-c",
            probe,
            "check-prefix",
            persistent_prefix,
        )


def test_shim_restores_default_file_size_signal(
    distribution: SimpleNamespace,
    tmp_path: Path,
) -> None:
    d = distribution
    probe_archive = tmp_path / d.archive.name
    write_shell_probe_archive(probe_archive)
    d.replay.add_release(d.wheel, d.version, probe_archive)
    command = ("-c", "ulimit -c 0; kill -s XFSZ $$")
    direct = d.tool.run(
        "Measure the native signal exit", "/bin/sh", *command, check=False
    )
    assert direct.returncode == -signal.SIGXFSZ
    with (
        d.rehearsal.keeper_server(d.replay.handler()) as base,
        closing(socket.socket()) as proxy,
    ):
        offline_env(d, base, proxy)
        run(d.tool, "Install the shim wheel", d.tool.uv, "tool", "install", d.wheel)
        through_shim = d.tool.run(
            "Measure the shim signal exit", d.tool.sai, *command, check=False
        )
    assert through_shim.returncode == direct.returncode


def test_shim_repairs_digest_directory_permissions_offline(
    distribution: SimpleNamespace,
) -> None:
    d = distribution
    d.replay.add_release(d.wheel, d.version, d.archive)
    with closing(socket.socket()) as proxy:
        with d.rehearsal.keeper_server(d.replay.handler()) as base:
            offline_env(d, base, proxy)
            run(d.tool, "Install the shim wheel", d.tool.uv, "tool", "install", d.wheel)
            d.tool.verify_version(d.version)
        [binary] = list((d.tool.sai_home / "bin").rglob("sai"))
        binary.parent.chmod(0o755)
        assert (
            run(
                d.tool,
                "Repair the verified cache without a download",
                d.tool.sai,
                "--version",
            )
            == f"sai {d.version}\n"
        )
        assert stat.S_IMODE(binary.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("damage", ["receipt-link", "executable-link", "mode-zero"])
def test_shim_replaces_malformed_cache_entries(
    distribution: SimpleNamespace, tmp_path: Path, damage: str
) -> None:
    d = distribution
    d.replay.add_release(d.wheel, d.version, d.archive)
    requests = [f"{d.release}/SHA256SUMS", f"{d.release}/{d.archive.name}"]
    missing = tmp_path / "missing"
    with (
        d.rehearsal.keeper_server(d.replay.handler()) as base,
        closing(socket.socket()) as proxy,
    ):
        offline_env(d, base, proxy)
        run(d.tool, "Install the shim wheel", d.tool.uv, "tool", "install", d.wheel)
        d.tool.verify_version(d.version)
        assert d.replay.get_requests == requests
        [binary] = list((d.tool.sai_home / "bin").rglob("sai"))
        folder = binary.parent
        receipt = folder.parent / "verified"
        if damage == "mode-zero":
            folder.chmod(0)
        else:
            entry = receipt if damage == "receipt-link" else binary
            entry.unlink()
            entry.symlink_to(missing)
        try:
            assert (
                run(
                    d.tool, "Replace the malformed cache entry", d.tool.sai, "--version"
                )
                == f"sai {d.version}\n"
            )
            assert d.replay.get_requests == requests * 2
            assert stat.S_IMODE(folder.stat().st_mode) == 0o700
            assert not binary.is_symlink() and not receipt.is_symlink()
            assert not missing.exists()
        finally:
            if folder.is_dir():
                folder.chmod(0o700)


def test_distributable_wheel_ignores_loopback_release_override(
    distribution: SimpleNamespace,
) -> None:
    wheel = os.environ.get("SAI_NATIVE_RELEASE_WHEEL")
    if not wheel:
        pytest.skip("set SAI_NATIVE_RELEASE_WHEEL to the distributable shim wheel")
    d = distribution
    d.replay.add_release(Path(wheel), d.version)
    with (
        d.rehearsal.keeper_server(d.replay.handler()) as base,
        closing(socket.socket()) as proxy,
    ):
        offline_env(d, base, proxy)
        run(
            d.tool,
            "Install the distributable wheel",
            d.tool.uv,
            "tool",
            "install",
            Path(wheel).resolve(),
        )
        result = d.tool.run(
            "Probe the official release source", d.tool.sai, "--version", check=False
        )
    assert result.returncode == 1 and result.stdout == ""
    assert d.replay.get_requests == []
    assert (d.tool.sai_home / "bin" / "shim").is_dir()


@pytest.mark.parametrize(
    "permissions",
    [
        "mode",
        pytest.param(
            "acl",
            marks=pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACLs"),
        ),
    ],
)
def test_shim_rejects_untrusted_profile_ancestry(
    distribution: SimpleNamespace,
    tmp_path: Path,
    permissions: str,
) -> None:
    d = distribution
    shared = tmp_path / "shared"
    shared.mkdir()
    if permissions == "acl":
        run(
            d.tool,
            "Set a directory ACL in the temporary profile",
            "/bin/chmod",
            "+a",
            "everyone allow delete_child",
            shared,
        )
    else:
        shared.chmod(0o777)
    profile = shared / "sai-profile"
    profile.mkdir(mode=0o700)
    with (
        d.rehearsal.keeper_server(d.replay.handler()) as base,
        closing(socket.socket()) as proxy,
    ):
        offline_env(d, base, proxy)
        run(d.tool, "Install the shim wheel", d.tool.uv, "tool", "install", d.wheel)
        d.tool.env["SAI_HOME"] = str(profile)
        result = d.tool.run(
            "Reject untrusted profile ancestry", d.tool.sai, "--version", check=False
        )
    assert result.returncode == 1 and result.stdout == ""
    diagnostic = (
        "ACL permitting changes" if permissions == "acl" else "untrusted ancestor"
    )
    assert diagnostic in result.stderr
    assert d.replay.get_requests == []
    assert list(profile.iterdir()) == []


def test_native_shim_selects_exact_asset_and_reuses_it_offline(
    distribution: SimpleNamespace,
) -> None:
    d = distribution
    tool, replay = d.tool, d.replay
    replay.add_release(d.wheel, d.version, d.archive)
    replay.latest = "99.0.0"
    expected_version = f"sai {d.version}\n"
    expected_requests = [f"{d.release}/SHA256SUMS", f"{d.release}/{d.archive.name}"]
    sums, media_type = replay.assets[expected_requests[0]]
    wrong_target = (
        "aarch64-apple-darwin"
        if "x86_64" in d.archive.name
        else "x86_64-unknown-linux-gnu"
    )
    other_archive = f"sai-{d.version}-{wrong_target}.tar.gz"
    decoy = f"{'0' * 64}  {other_archive}\n".encode("ascii")
    replay.assets[expected_requests[0]] = decoy + sums, media_type
    with closing(socket.socket()) as dead_proxy:
        with d.rehearsal.keeper_server(replay.handler()) as base:
            offline_env(d, base, dead_proxy)
            run(tool, "Install the shim wheel", tool.uv, "tool", "install", d.wheel)
            target = tool.sai.resolve(strict=True)
            assert target == tool.python.parent / "sai"
            assert tool.sai.is_symlink()

            # Two cold starts share one verified download and both execute Sai.
            with ThreadPoolExecutor(max_workers=2) as workers:
                launches = [
                    workers.submit(run, tool, "Run cold shim", "sai", "--version")
                    for _ in range(2)
                ]
                assert [launch.result() for launch in launches] == [
                    expected_version
                ] * 2
            assert replay.get_requests == expected_requests
            [native] = list((tool.sai_home / "bin").rglob("sai"))
            assert stat.S_IMODE(native.stat().st_mode) == 0o700
            for parent in native.parents:
                assert stat.S_IMODE(parent.stat().st_mode) == 0o700
                if parent == tool.sai_home:
                    break

            tool.env["SAI_HOME"] = ".sai"
            tool.json(
                "Forward arguments and relative SAI_HOME",
                "config",
                "set",
                "update.auto",
                "false",
            )
            assert tool.json(
                "Read the same profile", "config", "get", "update.auto"
            ) == {"key": "update.auto", "value": False, "source": "file"}
            assert (
                run(
                    tool,
                    "Run a legacy version probe from the neutral directory",
                    sys.executable,
                    "-I",
                    "-c",
                    "import fcntl, os, subprocess, sys; "
                    "lock = os.open('.sai/update-cache.lock', os.O_CREAT | os.O_RDWR, 0o600); "
                    "fcntl.flock(lock, fcntl.LOCK_EX); "
                    "sys.exit(subprocess.call([sys.argv[1], '--version'], cwd='/'))",
                    tool.sai,
                )
                == expected_version
            )
            tool.env["SAI_HOME"] = str(tool.sai_home)

            # `uv tool run` is the same entry point as `uvx`.
            uvx_home = tool.home / "uvx-profile"
            assert (
                run(
                    tool,
                    "Run an isolated exact uvx pin",
                    tool.uv,
                    "tool",
                    "run",
                    "--isolated",
                    "--no-index",
                    "--find-links",
                    d.wheel.parent,
                    "--from",
                    f"sai-cli=={d.version}",
                    "sai",
                    "--version",
                    env={**tool.env, "SAI_HOME": str(uvx_home)},
                )
                == expected_version
            )
            assert replay.get_requests == expected_requests * 2

        # The fixture origin no longer listens, and the proxy cannot connect.
        assert (
            run(tool, "Reuse verified binary offline", tool.sai, "--version")
            == expected_version
        )
        shadow = tool.home / "shadow"
        shadow.mkdir()
        other = shadow / "sai"
        other.write_text("#!/bin/sh\nprintf 'shadowed\\n'\n")
        other.chmod(0o700)
        path = tool.env["PATH"]
        for paths, selected, output in (
            (f"{shadow}{os.pathsep}{path}", other, "shadowed\n"),
            (f"{path}{os.pathsep}{shadow}", tool.sai, expected_version),
        ):
            assert shutil.which("sai", path=paths) == str(selected)
            assert (
                run(
                    tool,
                    "Check sai on PATH",
                    "sai",
                    "--version",
                    env={**tool.env, "PATH": paths},
                )
                == output
            )
        assert tool.sai.resolve(strict=True) == target
        assert other.read_text().startswith("#!/bin/sh")

        # A tampered executable cannot run even if it preserves the mode.
        native.write_bytes(b"#!/bin/sh\nprintf 'tampered\\n'\n")
        rejected = tool.run(
            "Reject tampered offline cache", tool.sai, "--version", check=False
        )
        assert rejected.returncode != 0 and rejected.stdout == ""
        run(tool, "Uninstall the shim with uv", tool.uv, "tool", "uninstall", "sai-cli")
        assert not tool.sai.exists() and not tool.sai.is_symlink()
        assert (tool.sai_home / "config.toml").is_file()


def invalid_tar(kind: str) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        entry = tarfile.TarInfo("../sai" if kind == "traversal" else "sai")
        entry.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE}.get(
            kind, tarfile.REGTYPE
        )
        if kind in {"symlink", "hardlink"}:
            entry.linkname = "outside"
            archive.addfile(entry)
        else:
            entry.size = 1
            archive.addfile(entry, io.BytesIO(b"-"))
            if kind == "duplicate member":
                archive.addfile(entry, io.BytesIO(b"-"))
    return output.getvalue()


@pytest.mark.parametrize(
    ("fault", "diagnostic"),
    [
        ("digest mismatch", "failed SHA-256"),
        ("missing digest", "one checksum"),
        ("duplicate digest", "one checksum"),
        ("oversize manifest", "exceeds its size limit"),
        ("cross-origin redirect", "redirected outside its download hosts"),
        ("traversal", "one regular file"),
        ("symlink", "one regular file"),
        ("hardlink", "one regular file"),
        ("duplicate member", "extra data"),
        ("symlink cache", "owner-controlled directory"),
        ("shared cache", "owner-controlled directory"),
        ("external override", "requires a loopback origin"),
    ],
)
def test_native_shim_rejects_invalid_release_or_cache(
    distribution: SimpleNamespace,
    fault: str,
    diagnostic: str,
    tmp_path: Path,
) -> None:
    d = distribution
    replay, tool = d.replay, d.tool
    replay.add_release(d.wheel, d.version, d.archive)
    sums_path = f"{d.release}/SHA256SUMS"
    archive_path = f"{d.release}/{d.archive.name}"
    if fault == "digest mismatch":
        replay.assets[archive_path] = b"wrong archive", "application/octet-stream"
    elif fault == "missing digest":
        replay.add_release(d.wheel, d.version)
    elif fault == "duplicate digest":
        sums, media_type = replay.assets[sums_path]
        replay.assets[sums_path] = sums * 2, media_type
    elif fault == "oversize manifest":
        replay.assets[sums_path] = b" " * (64 * 1024 + 1), "text/plain"
    elif fault in {"traversal", "symlink", "hardlink", "duplicate member"}:
        payload = invalid_tar(fault)
        digest = hashlib.sha256(payload).hexdigest()
        replay.assets[archive_path] = payload, "application/octet-stream"
        replay.assets[sums_path] = (
            f"{digest}  {d.archive.name}\n".encode(),
            "text/plain",
        )
    elif fault in {"symlink cache", "shared cache"}:
        tool.sai_home.mkdir(mode=0o700)
        cache = tool.sai_home / "bin"
        if fault == "symlink cache":
            outside = tmp_path / "outside"
            outside.mkdir()
            cache.symlink_to(outside, target_is_directory=True)
        else:
            cache.mkdir(mode=0o755)
    handler = replay.handler()

    class Redirect(handler):
        def do_GET(self) -> None:
            replay.get_requests.append(self.path)
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{dead_proxy.getsockname()[1]}/other"
            )
            self.end_headers()

    with (
        d.rehearsal.keeper_server(
            Redirect if fault == "cross-origin redirect" else handler
        ) as base,
        closing(socket.socket()) as dead_proxy,
    ):
        offline_env(d, base, dead_proxy)
        run(tool, "Install the shim wheel", tool.uv, "tool", "install", d.wheel)
        if fault == "external override":
            tool.env["SAI_NATIVE_RELEASE_BASE"] = "https://release-rehearsal.invalid"
        result = tool.run(
            "Reject the invalid asset or cache", tool.sai, "--version", check=False
        )
    assert result.returncode == 1 and result.stdout == ""
    assert diagnostic in result.stderr
    assert not list(tool.sai_home.rglob("verified"))
    if fault in {"external override", "symlink cache", "shared cache"}:
        assert replay.get_requests == []


@pytest.mark.parametrize("check", ["rollback", "mcp-startup", "relative-profile"])
def test_published_python_updater_can_skip_bridge(
    distribution: SimpleNamespace,
    check: str,
) -> None:
    baseline_dir = os.environ.get("SAI_NATIVE_BASELINE")
    if not baseline_dir:
        pytest.skip(
            "set SAI_NATIVE_BASELINE to the pinned Python installer and release files"
        )
    baseline = Path(baseline_dir).resolve(strict=True)
    assert (baseline / "commit").read_text().strip() == BASELINE_COMMIT
    old_wheel = baseline / f"sai_cli-{BASELINE}-py3-none-any.whl"
    d = distribution
    tool, replay = d.tool, d.replay
    replay.add_release(d.wheel, d.version, d.archive)
    replay.add_release(old_wheel, BASELINE)
    replay.assets[
        f"/{d.rehearsal.REPO}/releases/download/sai-cli/v{BASELINE}/SHA256SUMS"
    ] = ((baseline / "SHA256SUMS").read_bytes(), "text/plain")
    # Cache public Python dependencies before the loopback-only operations.
    run(
        tool,
        "Prime uv's dependency cache",
        tool.uv,
        "pip",
        "install",
        "--target",
        tool.home / "wheel-cache",
        old_wheel,
    )
    with closing(socket.socket()) as dead_proxy:
        with d.rehearsal.keeper_server(replay.handler()) as base:
            offline_env(d, base, dead_proxy)
            replay.latest = BASELINE
            run(
                tool,
                "Install the published Python release",
                sys.executable,
                baseline / "install.py",
                "--api-base",
                base,
                "--web-base",
                base,
            )
            tool.verify_version(BASELINE)
            launcher = tool.sai.resolve(strict=True)
            previous_release = json.loads(
                run(
                    tool,
                    "Capture the verified Python release record",
                    tool.python,
                    "-I",
                    "-c",
                    "import json, sys; from pathlib import Path; "
                    "from sai_cli.core.update import read_release_record; "
                    "print(json.dumps(read_release_record(Path(sys.prefix)).to_json()))",
                )
            )
            tool.json("Disable auto-update", "config", "set", "update.auto", "false")
            endpoint = f"{base}/query"
            tool.json(
                "Save a loopback Keeper endpoint",
                "config",
                "set",
                "keeper.endpoint",
                endpoint,
            )
            config = tool.sai_home / "config.toml"
            configured = config.read_bytes()
            intent = "20261124T120000000Z_0123456789ABCDEF"
            run(
                tool,
                "Reserve a fixture intent with Python storage",
                tool.python,
                "-I",
                "-c",
                "import sys; from sai_cli.core.storage import pending; pending(*sys.argv[1:])",
                intent,
                endpoint,
                replay.addr,
                "CLOSE_POSITION",
                "7",
            )
            ledger = tool.sai_home / "trader" / "intents.sqlite3"
            before = ledger.read_bytes()
            inode = ledger.stat().st_ino
            expected = tool.json(
                "Read the Python ledger", "intent", "local", replay.addr
            )
            assert expected == [
                {"intentKey": intent, "kind": "CLOSE_POSITION", "status": "unknown"}
            ]

            replay.latest = d.version
            if check == "relative-profile":
                tool.env["SAI_HOME"] = ".sai"
            upgraded = run(
                tool,
                "Jump from the published Python updater to the shim",
                tool.sai,
                "--json",
                "update",
                "--api-base",
                base,
                "--web-base",
                base,
            )
            assert json.loads(upgraded)["status"] == "updated"
            tool.verify_version(d.version)
            assert tool.sai.resolve(strict=True) == launcher
            assert (
                tool.json(
                    "Read the live ledger through Rust", "intent", "local", replay.addr
                )
                == expected
            )
            assert config.read_bytes() == configured
            assert ledger.stat().st_ino == inode
            if check == "mcp-startup":
                verify_mcp_startup(d)
                return

        tool.verify_version(d.version)
        # Reinstall the baseline wheel already verified and kept by the old installer.
        rollback_wheel = Path(previous_release["wheel"])
        assert rollback_wheel == tool.sai_home / "cache" / "wheels" / old_wheel.name
        assert (
            hashlib.sha256(rollback_wheel.read_bytes()).hexdigest()
            == previous_release["sha256"]
        )
        run(
            tool,
            "Roll back through uv without an asset server",
            tool.uv,
            "tool",
            "install",
            "--offline",
            "--force",
            rollback_wheel,
        )
        tool.verify_version(BASELINE)
        run(
            tool,
            "Restore the verified release record for the reinstalled Python wheel",
            tool.python,
            "-I",
            "-c",
            "import sys; from pathlib import Path; "
            "from sai_cli.core.updater import write_record; "
            "write_record(Path(sys.prefix), sys.argv[1], sys.argv[2], Path(sys.argv[3]))",
            previous_release["tag"],
            previous_release["sha256"],
            rollback_wheel,
        )
        assert tool.sai.resolve(strict=True) == launcher
        assert (
            run(
                tool,
                "Check that the rolled-back install can use sai update",
                tool.python,
                "-I",
                "-c",
                "from sai_cli.core.update import install_kind; print(install_kind().kind)",
            )
            == "uv-tool-release\n"
        )
        assert (
            tool.json(
                "Read the live ledger after rollback", "intent", "local", replay.addr
            )
            == expected
        )
        assert ledger.stat().st_ino == inode
        assert ledger.read_bytes() == before
        assert config.read_bytes() == configured
        assert replay.submitted == []
