"""scripts/install.py against a loopback release, with a fake `uv` and with
the real one in temporary tool directories."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fake_release import build_wheel, downloads, release_routes
from sai_cli.core import storage, update
from test_update import API_PATH, WEB_PATH, Routes, Stub, listing, redirect

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
SCRIPT = SCRIPTS / "install.py"
sys.path.insert(0, str(SCRIPTS))

import install

GIT = "git+https://github.com/SaiDotFun/open-sai"
REAL_UV = shutil.which("uv")

# Logs each call. `tool install FILE` also logs the file's SHA-256 and lays
# down the dist-info uv would; `tool dir` prints the tool directory.
FAKE_UV = """#!{python}
import hashlib, json, os, pathlib, sys
args = sys.argv[1:]
entry = {{"args": args}}
tools = pathlib.Path(os.environ["FAKE_UV_TOOLS"])
if args[:2] == ["tool", "install"] and os.path.isfile(args[-1]):
    entry["sha256"] = hashlib.sha256(open(args[-1], "rb").read()).hexdigest()
    info = tools / "sai-cli" / "lib" / "python3.11" / "site-packages"
    info = info / "sai_cli-0.0.0.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "RECORD").write_text("installed by the fake uv\\n")
with open(os.environ["FAKE_UV_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\\n")
if args == ["tool", "dir"]:
    print(tools)
else:
    print("fake uv ran")
sys.exit(int(os.environ.get("FAKE_UV_EXIT", "0")))
"""

WHEELS = {"0.2.0": build_wheel("0.2.0"), "0.10.0": build_wheel("0.10.0")}


@pytest.fixture
def fake_uv(tmp_path) -> dict[str, str]:
    """An environment whose only `uv` records its arguments and exits."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(FAKE_UV.format(python=sys.executable))
    uv.chmod(0o755)
    (tmp_path / "tools").mkdir()
    return {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path / "home"),
        "SAI_HOME": str(tmp_path / "sai-home"),
        "TMPDIR": str(tmp_path),
        "FAKE_UV_LOG": str(tmp_path / "uv.log"),
        "FAKE_UV_TOOLS": str(tmp_path / "tools"),
    }


def run(env: dict[str, str], stub: Stub, *args: str) -> subprocess.CompletedProcess:
    """Run the script as a person would, with nothing of this process's
    environment, so a real uv is never reached."""
    command = [sys.executable, str(SCRIPT), "--api-base", stub.url]
    command += ["--web-base", stub.url, *args]
    return subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=180, check=False
    )


def calls(env: dict[str, str]) -> list[dict]:
    log = Path(env["FAKE_UV_LOG"])
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def installs(env: dict[str, str]) -> list[list[str]]:
    return [c["args"] for c in calls(env) if c["args"][:2] == ["tool", "install"]]


def test_the_script_agrees_with_the_cli_about_releases():
    names = ("REPO", "API_BASE", "WEB_BASE", "TAG_PREFIX", "PAGE_SIZE", "MAX_PAGES")
    for name in (*names, "MAX_BODY", "RECORD_NAME", "RECORD_SCHEMA"):
        assert getattr(install, name) == getattr(update, name), name
    tags = (
        "sai-cli/v0.1.0",
        "sai-cli/v0.2.0-rc.1",
        "sai-cli/v0.01.0",
        "v0.1.0",
        "sai-rs/v0.1.0",
        "",
    )
    for tag in tags:
        assert install.parse_tag(tag) == update.parse_tag(tag), tag
    assert install.wheel_url("sai-cli/v1.2.3") == (
        update.release_for("sai-cli/v1.2.3").wheel_url
    )
    assert install.wheel_name("1.2.3") == update.wheel_name("1.2.3")
    assert update.INSTALL_COMMAND.endswith("/main/scripts/install.py")


def test_installs_the_verified_wheel_of_the_highest_release(fake_uv, tmp_path):
    routes = release_routes(WHEELS)
    routes[API_PATH] = listing(
        "sai-rs/v9.0.0", "sai-cli/v0.3.0-rc.1", "sai-cli/v0.2.0", "sai-cli/v0.10.0"
    )
    with Stub(routes) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 0, result.stderr
    install_call = next(
        c for c in calls(fake_uv) if c["args"][:2] == ["tool", "install"]
    )
    wheel = Path(install_call["args"][2])
    # uv got a local file holding exactly the bytes the release hashes.
    assert wheel.name == "sai_cli-0.10.0-py3-none-any.whl"
    digest = hashlib.sha256(WHEELS["0.10.0"]).hexdigest()
    assert install_call["sha256"] == digest
    assert "://" not in str(wheel)
    # The wheel stays where uv's receipt points, in the private wheel cache,
    # so `uv tool upgrade --all` can read it again.
    assert wheel == tmp_path / "sai-home" / "cache" / "wheels" / wheel.name
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == digest
    assert wheel.parent.stat().st_mode & 0o777 == 0o700
    assert [path.name for path in wheel.parent.iterdir()] == [wheel.name]
    lines = result.stdout.splitlines()
    assert lines[0] == "Installing sai-cli 0.10.0 from its release wheel."
    assert lines[1] == f"Verified {wheel.name} against the release's SHA256SUMS."
    assert lines[-1] == "Installed. Run `sai --help` to start."
    assert result.stderr == ""
    record = json.loads((tmp_path / "tools" / ".sai-cli-release.json").read_text())
    assert record == {
        "schema": 1,
        "tag": "sai-cli/v0.10.0",
        "sha256": digest,
        "installed": hashlib.sha256(b"installed by the fake uv\n").hexdigest(),
        "wheel": str(wheel),
    }


def test_a_wheel_that_differs_from_sha256sums_is_not_installed(fake_uv, tmp_path):
    with Stub(release_routes(WHEELS, bad_hash=["0.10.0"])) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 1
    assert "does not match its SHA256SUMS entry" in result.stderr
    assert calls(fake_uv) == []
    assert list((tmp_path / "sai-home" / "cache" / "wheels").iterdir()) == []


def test_a_relative_sai_home_still_gives_uv_an_absolute_wheel(fake_uv, tmp_path):
    with Stub(release_routes(WHEELS)) as stub:
        command = [sys.executable, str(SCRIPT), "--api-base", stub.url]
        command += ["--web-base", stub.url]
        result = subprocess.run(
            command,
            env={**fake_uv, "SAI_HOME": "relative-home"},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    wheel = Path(installs(fake_uv)[0][2])
    assert wheel.is_absolute()
    assert wheel.parent == tmp_path / "relative-home" / "cache" / "wheels"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_a_loose_wheel_cache_is_made_private_before_uv_reads_it(fake_uv, tmp_path):
    """An existing cache that other users can write to would let them swap
    the verified wheel. Ours is tightened to 0700 first."""
    home = tmp_path / "sai-home"
    wheels = home / "cache" / "wheels"
    wheels.mkdir(parents=True)
    home.chmod(0o700)
    for directory in (home / "cache", wheels):
        directory.chmod(0o777)
    with Stub(release_routes(WHEELS)) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 0, result.stderr
    for directory in (home, home / "cache", wheels):
        assert directory.stat().st_mode & 0o777 == 0o700, directory
    assert len(installs(fake_uv)) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
@pytest.mark.parametrize(
    ("profile", "mode", "expected"),
    [
        ("dedicated", 0o755, 0o700),
        ("empty", 0o755, 0o755),
        ("mixed-use", 0o755, 0o755),
        ("dedicated", 0o2770, 0o2770),
    ],
)
def test_installer_respects_existing_root_mode(
    tmp_path, monkeypatch, profile, mode, expected
):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    if profile != "empty":
        storage.db().close()
    if profile == "mixed-use":
        (tmp_path / "project.txt").touch()
    tmp_path.chmod(mode)

    if mode & 0o022:
        with pytest.raises(install.LookupFailed, match="writable by other users"):
            install.private_cache()
        assert not (tmp_path / "cache").exists()
    else:
        wheels = install.private_cache()
        assert wheels.stat().st_mode & 0o777 == 0o700
    assert tmp_path.stat().st_mode & 0o7777 == expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership")
def test_a_cache_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    home = tmp_path / "sai-home"
    (home / "cache" / "wheels").mkdir(parents=True)
    monkeypatch.setenv("SAI_HOME", str(home))
    assert install.private_cache() == home / "cache" / "wheels"
    # The same directories, seen by a different user.
    other = os.getuid() + 1
    monkeypatch.setattr(install.os, "getuid", lambda: other)
    with pytest.raises(install.LookupFailed, match="belongs to another user"):
        install.private_cache()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership")
def test_a_cache_that_is_not_ours_stops_the_install(fake_uv, tmp_path):
    """Run as a person would: a cache that cannot be made private means
    exit 1 before uv runs."""
    home = tmp_path / "sai-home"
    home.mkdir()
    (home / "cache").write_text("not a directory")
    with Stub(release_routes(WHEELS)) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 1
    assert "could not keep the wheel" in result.stderr
    assert calls(fake_uv) == []


def test_a_symlinked_wheel_cache_is_refused(fake_uv, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (tmp_path / "sai-home").mkdir()
    (tmp_path / "sai-home" / "cache").symlink_to(shared)
    with Stub(release_routes(WHEELS)) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 1
    assert "is a symlink" in result.stderr
    assert calls(fake_uv) == []
    assert list(shared.iterdir()) == []


def test_a_release_without_its_files_is_not_installed(fake_uv):
    routes = release_routes(WHEELS)
    routes.pop(f"{downloads('0.10.0')}/SHA256SUMS")
    unlisted = release_routes(WHEELS)
    unlisted[f"{downloads('0.10.0')}/SHA256SUMS"] = (200, {}, b"")
    with Stub(routes) as stub:
        missing = run(fake_uv, stub)
    with Stub(unlisted) as stub:
        empty = run(fake_uv, stub)
    assert missing.returncode == empty.returncode == 1
    assert "downloading SHA256SUMS answered HTTP 404" in missing.stderr
    assert "SHA256SUMS does not list" in empty.stderr
    assert calls(fake_uv) == []


def paged(*pages: tuple[str, ...]) -> Routes:
    routes: Routes = release_routes(WHEELS)
    routes.pop(API_PATH)
    for number, tags in enumerate(pages, start=1):
        query = f"per_page={install.PAGE_SIZE}&page={number}"
        routes[f"{API_PATH}?{query}"] = listing(*tags, more=number < len(pages))
    return routes


def test_reads_every_page_and_takes_the_highest_version(fake_uv):
    """A later maintenance release on page one does not hide 0.10.0."""
    routes = paged(("sai-rs/v9.0.0", "sai-cli/v0.2.0"), ("sai-cli/v0.10.0",))
    with Stub(routes) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 0, result.stderr
    assert "Installing sai-cli 0.10.0" in result.stdout
    assert len(stub.queries) >= 2


def test_past_the_page_limit_nothing_is_installed(fake_uv):
    """An incomplete list is an error; it must not fall back to main."""
    routes = paged(*[("sai-rs/v1.0.0",) for _ in range(install.MAX_PAGES + 1)])
    with Stub(routes) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 1
    assert "lookup is incomplete" in result.stderr
    assert calls(fake_uv) == []


def test_source_builds_the_exact_release_tag_from_git(fake_uv):
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        result = run(fake_uv, stub, "--source")
    assert result.returncode == 0, result.stderr
    # refs/tags/…: a branch named like the tag cannot shadow it.
    assert installs(fake_uv) == [["tool", "install", f"{GIT}@refs/tags/sai-cli/v0.2.0"]]
    assert "Building sai-cli 0.2.0 from source." in result.stdout
    assert stub.requests == [("GET", API_PATH)]


@pytest.mark.parametrize("flags", [(), ("--source",)])
@pytest.mark.parametrize("tags", [(), ("sai-rs/v1.0.0", "sai-cli/v0.1.0-rc.1")])
def test_falls_back_to_git_main_with_a_note_before_the_first_release(
    fake_uv, flags, tags
):
    with Stub({API_PATH: listing(*tags)}) as stub:
        result = run(fake_uv, stub, *flags)
    assert result.returncode == 0, result.stderr
    assert installs(fake_uv) == [["tool", "install", GIT]]
    assert result.stdout.splitlines()[0] == (
        "No sai-cli release is published yet; installing from source (main)."
    )


@pytest.mark.parametrize(
    "tag", ["sai-cli%2Fv0.3.1", "sai-rs/v1.0.0", "docs%2Freleases", "docs/releases"]
)
def test_a_rate_limited_api_with_releases_installs_nothing(fake_uv, tag):
    """The web page's latest release can be stale, so it is never installed.
    A tag that ends in `/releases` is still a release, not the empty list:
    the script must not fall back to main."""
    location = f"https://github.com/{update.REPO}/releases/tag/{tag}"
    routes: Routes = {API_PATH: (403, {}, b"{}"), WEB_PATH: redirect(location)}
    with Stub(routes) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 1
    assert stub.requests == [("GET", API_PATH), ("HEAD", WEB_PATH)]
    assert "rate limited or failing" in result.stderr
    assert f"uv tool install {GIT}" in result.stderr
    assert calls(fake_uv) == []


def test_rate_limited_with_no_release_still_falls_back_to_git(fake_uv):
    routes: Routes = {
        API_PATH: (429, {}, b""),
        WEB_PATH: redirect(f"https://github.com/{update.REPO}/releases"),
    }
    with Stub(routes) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 0, result.stderr
    assert installs(fake_uv) == [["tool", "install", GIT]]


@pytest.mark.parametrize(
    ("routes", "message"),
    [
        ({API_PATH: (200, {}, b"not json")}, "malformed JSON"),
        ({}, "HTTP 404"),
        ({API_PATH: (500, {}, b""), WEB_PATH: (200, {}, b"")}, "without a redirect"),
    ],
)
def test_a_failed_lookup_installs_nothing_and_names_the_source_install(
    fake_uv, routes, message
):
    with Stub(routes) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 1
    assert message in result.stderr
    assert f"uv tool install {GIT}" in result.stderr
    assert calls(fake_uv) == []


@pytest.mark.parametrize("slow", ["list", "wheel"])
def test_a_slow_drip_cannot_outlast_the_wall_clock_budget(fake_uv, slow):
    """Bytes that keep arriving reset no clock: the lookup and the download
    each stop at their deadline."""
    routes = release_routes({"0.2.0": WHEELS["0.2.0"] + b" " * 4000})
    wheel_path = f"{downloads('0.2.0')}/{update.wheel_name('0.2.0')}"
    if slow == "list":
        status, headers, body = routes[API_PATH]
        routes[API_PATH] = (status, headers, body + b" " * 4000)
    drip_paths = frozenset({API_PATH if slow == "list" else wheel_path})
    with Stub(routes, drip=0.02, drip_paths=drip_paths) as stub:
        started = time.monotonic()
        result = run(fake_uv, stub, "--timeout", "0.5")
        elapsed = time.monotonic() - started
    assert result.returncode == 1
    assert "timed out" in result.stderr
    assert installs(fake_uv) == []
    # Far below the 80 s the drip would take; generous for a loaded machine.
    assert elapsed < 30


def test_dry_run_downloads_and_runs_nothing(fake_uv):
    with Stub(release_routes(WHEELS)) as stub:
        result = run(fake_uv, stub, "--dry-run")
        requests = list(stub.requests)
    assert result.returncode == 0, result.stderr
    assert "Would download http" in result.stdout
    assert "Dry run: nothing was installed." in result.stdout
    assert requests == [("GET", API_PATH)]
    assert calls(fake_uv) == []


def test_reports_a_failed_uv_and_a_missing_uv(fake_uv, tmp_path):
    with Stub(release_routes(WHEELS)) as stub:
        failed = run({**fake_uv, "FAKE_UV_EXIT": "2"}, stub)
        empty = tmp_path / "empty"
        empty.mkdir()
        missing = run({**fake_uv, "PATH": str(empty)}, stub)
    assert failed.returncode == 2
    assert "uv tool install failed with exit code 2" in failed.stderr
    assert "Installed." not in failed.stdout
    assert not (tmp_path / "tools" / ".sai-cli-release.json").exists()
    assert missing.returncode == 1
    assert "uv is not on PATH" in missing.stderr


def test_prefers_the_uv_that_runs_the_script(fake_uv, tmp_path):
    """`uv run` sets UV to its own path; the script installs with that uv."""
    runner = tmp_path / "runner-uv"
    runner.write_text(FAKE_UV.format(python=sys.executable))
    runner.chmod(0o755)
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {**fake_uv, "PATH": str(empty), "UV": str(runner)}
    with Stub(release_routes(WHEELS)) as stub:
        result = run(env, stub)
    assert result.returncode == 0, result.stderr
    assert len(installs(env)) == 1


@pytest.mark.slow
@pytest.mark.skipif(REAL_UV is None, reason="uv is not on PATH")
def test_real_uv_install_is_recognized_as_a_release_install(tmp_path):
    """The record the script writes is the one the CLI reads."""
    assert REAL_UV is not None
    env = {
        "PATH": f"{Path(REAL_UV).parent}:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "TMPDIR": str(tmp_path),
        "SAI_HOME": str(tmp_path / "sai-home"),
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_NO_CONFIG": "1",
    }
    with Stub(release_routes(WHEELS)) as stub:
        result = run(env, stub)
    assert result.returncode == 0, result.stderr
    version = subprocess.run(
        [str(tmp_path / "bin" / "sai"), "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    assert version.strip() == "sai, version 0.10.0"
    prefix = tmp_path / "tools" / "sai-cli"
    installed = update.install_kind(prefix)
    assert installed.kind == update.UV_TOOL_RELEASE
    assert installed.official
    record = update.read_release_record(prefix)
    assert record is not None
    assert record.tag == "sai-cli/v0.10.0"
    assert record.sha256 == hashlib.sha256(WHEELS["0.10.0"]).hexdigest()
    # uv's saved requirement still resolves: the bulk upgrade works.
    upgrade = subprocess.run(
        [REAL_UV, "tool", "upgrade", "--all"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert upgrade.returncode == 0, upgrade.stderr
    assert update.install_kind(prefix).kind == update.UV_TOOL_RELEASE
