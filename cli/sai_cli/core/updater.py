"""Install a sai-cli release over this install, verify it, and roll back.

`sai update` downloads the release wheel and its SHA256SUMS, checks the hash,
installs the wheel with `uv tool install --force`, and then runs the new
`sai --version` and imports the package. A failure at any step leaves or
restores the previous version. Only an official uv tool install is changed.

Interactive commands call `tick`, which prints at most one notice per state
change and starts a detached `sai update --background` at most once a day.

Two locks keep this safe. The tool lock sits beside the uv tool environment
and covers a whole install, verify, and rollback, so every SAI_HOME that
shares the tool takes the same one. The state lock is `$SAI_HOME/update.lock`
and covers each read-modify-write of that profile's `update.json`.

What this protects against is a bad release, a failed network, two commands
racing, and mistakes such as uv pointed at another directory. It does not
defend against someone who can already write to uv's tool directory or to
$SAI_HOME; they can replace `sai` outright.

Everything this module uses from the package and its dependencies is imported
at the top: uv replaces those files while this process runs, so none of them
may be imported after the install starts.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import zipfile
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from .. import mcp_install
from . import log, native_bridge, paths, update
from .errors import ExitCode

EXIT_OK = ExitCode.OK
EXIT_FAILED = ExitCode.ERROR
# The install is one `sai update` does not change.
EXIT_UNSUPPORTED = ExitCode.UNSUPPORTED

MAX_WHEEL = 64 << 20
MAX_SUMS = 1 << 16
DOWNLOAD_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}
DOWNLOAD_TIMEOUT = 120.0
UV_TIMEOUT = 600.0
VERIFY_TIMEOUT = 60.0
# How long a foreground `sai update` waits for another update to finish.
LOCK_WAIT = 300.0
# How long a write to update.json waits for the state lock.
STATE_WAIT = 5.0
# The background check runs at most this often.
CHECK_INTERVAL = 24 * 60 * 60
# A background run that was started but never began work is retried after
# this long instead of a day later.
RETRY_INTERVAL = 15 * 60
TOOL_LOCK_NAME = ".sai-cli-update.lock"
NATIVE_PENDING = ".sai-cli-native-pending.json"
NATIVE_WHEELS = ".sai-cli-native-wheels"
NATIVE_RECOVERY = ".sai-cli-recover"
# A copy of the tool's environment, kept beside it while an update that has
# no verified wheel to go back to replaces it.
SNAPSHOT_PREFIX = ".sai-cli-rollback-"
BROKEN_PREFIX = ".sai-cli-broken-"
# Modules the new version must import: the CLI and the MCP server.
IMPORTS = ("sai_cli.sai_cli", "sai_cli.mcp_server.server")

UPDATED = "updated"
CURRENT = "current"
AVAILABLE = "available"
FAILED = "failed"
UNSUPPORTED = "unsupported"
NO_RELEASE = "no-release"
BUSY = "busy"

MCP_NOTES = {
    "background": "Background updates check user MCP registrations. Run `sai doctor` in each project to check project registrations.",
    "cwd-missing": "The working directory is gone. Run `sai doctor` in a project to check its MCP registrations.",
    "failed": "Could not check MCP registrations. Run `sai doctor` for details.",
}

# Runs a command and returns its exit code and its combined output.
Runner = Callable[[Sequence[str], float], tuple[int, str]]


class UpdateFailed(Exception):
    """A step of the update failed; the message says which."""


# State and locks


def state_lock_path() -> Path:
    """The lock for this profile's update.json."""
    return update.sai_home() / "update.lock"


def cache_lock_path() -> Path:
    """The lock for this profile's wheel cache, held for a whole install,
    verify, and rollback. Two tools that share one SAI_HOME share the cache,
    so neither may prune it while the other still needs its rollback wheel.
    It is always taken after the tool lock."""
    return update.sai_home() / "update-cache.lock"


def tool_lock_path(prefix: Path) -> Path:
    """The lock for installing over the uv tool environment at PREFIX.

    It sits beside the environment, in uv's tool directory, so commands with
    different SAI_HOME values that share the tool take the same lock, and uv
    recreating the environment does not remove it.
    """
    return prefix.parent / TOOL_LOCK_NAME


def read_state(path: Path | None = None) -> dict[str, Any]:
    """Return update.json as a dict, or {} when it is missing or unreadable.

    `latest` and `checkedAt` are the last check. `result` is the last update
    attempt and `notified` the notice already shown for it. `scheduledAt` is
    when a command last started the background run and `startedAt` when that
    run last began work.
    """
    try:
        data = json.loads((path or update.cache_path()).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != update.CACHE_SCHEMA:
        return {}
    return data


def write_state(changes: dict[str, Any], path: Path | None = None) -> None:
    """Merge CHANGES into update.json atomically. Callers hold the state
    lock."""
    target = path or update.cache_path()
    state = {**read_state(target), **changes, "schema": update.CACHE_SCHEMA}
    update.write_json(target, state)


def save_state(changes: dict[str, Any], path: Path | None = None) -> bool:
    """Take the state lock and merge CHANGES into update.json. Return False
    when the lock or the file could not be had; state is best effort."""
    try:
        lock_path = path.with_name("update.lock") if path is not None else None
        with locked(lock_path, wait=STATE_WAIT) as held:
            if held:
                write_state(changes, path)
            return held
    except OSError:
        return False


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


@contextlib.contextmanager
def locked(path: Path | None = None, wait: float = 0.0) -> Generator[bool]:
    """Hold the lock file at PATH (default: the state lock). Yields False
    when another process still holds it after WAIT seconds."""
    path = path or state_lock_path()
    update.prepare_parent(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        held = _try_lock(fd)
        while not held and time.monotonic() < deadline:
            time.sleep(0.1)
            held = _try_lock(fd)
        yield held
    finally:
        # Closing the descriptor releases the lock.
        os.close(fd)


# Download and verify


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _release_host(url: httpx.URL) -> bool:
    return (
        url.scheme == "https"
        and url.host in DOWNLOAD_HOSTS
        and url.port in (None, 443)
        and not url.userinfo
    )


def _download(
    client: httpx.Client, url: str, target: Path, limit: int, deadline: float
) -> None:
    origin = address = httpx.URL(url)
    name = target.name
    official = origin.host == "github.com" and _release_host(origin)
    if not official and (
        origin.scheme not in ("http", "https")
        or origin.userinfo
        or not _loopback(origin.host)
    ):
        raise UpdateFailed("release downloads require official or loopback URLs")
    for _ in range(5):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UpdateFailed(f"timed out downloading {name}")
        timeout = httpx.Timeout(min(remaining, 30.0))
        with client.stream("GET", address, timeout=timeout) as response:
            if response.has_redirect_location:
                address = address.join(response.headers["location"])
                valid = (
                    _release_host(address)
                    if official
                    else (
                        address.scheme == origin.scheme
                        and address.netloc == origin.netloc
                        and not address.userinfo
                    )
                )
                if not valid:
                    raise UpdateFailed(
                        "the release redirected outside its download hosts"
                    )
                continue
            if response.status_code == 404:
                raise UpdateFailed(f"the release has no file {name}")
            if response.status_code != 200:
                raise UpdateFailed(
                    f"downloading {name} answered HTTP {response.status_code}"
                )
            size = 0
            with target.open("wb") as handle:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise UpdateFailed(f"{name} is larger than expected")
                    if time.monotonic() > deadline:
                        raise UpdateFailed(f"timed out downloading {name}")
                    handle.write(chunk)
            return
    raise UpdateFailed("the release redirected too many times")


def expected_hash(sums: str, name: str) -> str | None:
    """Return the SHA-256 that SHA256SUMS text lists for file NAME."""
    matches = [
        fields[0].lower()
        for line in sums.splitlines()
        if len(fields := line.split(maxsplit=1)) == 2
        and fields[1].removeprefix("*") == name
    ]
    return (
        matches[0]
        if len(matches) == 1 and re.fullmatch(r"[0-9a-f]{64}", matches[0])
        else None
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_wheel(
    release: update.Release, directory: Path, timeout: float = DOWNLOAD_TIMEOUT
) -> tuple[Path, str]:
    """Download RELEASE's wheel and SHA256SUMS into DIRECTORY and return the
    wheel and its SHA-256, or raise UpdateFailed when the hash differs.

    TIMEOUT is wall-clock time for both transfers, with their redirects and
    response headers. HTTPX timeouts apply per network operation, so the
    transfers run in a daemon thread that this stops waiting for at the
    deadline, as the release lookup does.
    """
    name = update.wheel_name(release.version)
    wheel, sums = directory / name, directory / "SHA256SUMS"
    deadline = time.monotonic() + timeout
    outcome: dict[str, Exception] = {}

    def work() -> None:
        try:
            headers = {"User-Agent": "sai-cli"}
            with httpx.Client(headers=headers) as client:
                _download(client, release.sums_url, sums, MAX_SUMS, deadline)
                _download(client, release.wheel_url, wheel, MAX_WHEEL, deadline)
        except Exception as exc:  # noqa: BLE001 - re-raised in the caller
            outcome["error"] = exc

    worker = threading.Thread(target=work, name="sai-update-download", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        raise UpdateFailed(f"timed out downloading {name}")
    error = outcome.get("error")
    if isinstance(error, UpdateFailed):
        raise error
    if isinstance(error, httpx.HTTPError):
        raise UpdateFailed(f"download failed: {type(error).__name__}") from error
    if error is not None:
        raise error
    expected = expected_hash(sums.read_text(errors="replace"), name)
    if expected is None:
        raise UpdateFailed(f"SHA256SUMS does not list {name}")
    actual = sha256(wheel)
    if actual != expected:
        raise UpdateFailed(f"{name} does not match its SHA256SUMS entry")
    return wheel, actual


# Install, verify, roll back

# Where uv and the checks run. Never the caller's directory: uv reads
# project settings from its working directory, and Python can import from it.
NEUTRAL_DIR = os.path.abspath(os.sep)


def run(command: Sequence[str], timeout: float) -> tuple[int, str]:
    """Run COMMAND in NEUTRAL_DIR and return its exit code and combined
    output."""
    try:
        done = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            cwd=NEUTRAL_DIR,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:g} s"
    except OSError as exc:
        return 127, f"could not start: {type(exc).__name__}"
    return done.returncode, done.stdout


def find_uv() -> str | None:
    found = shutil.which("uv")
    return os.path.abspath(found) if found else None


def _last_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1][:300] if lines else "no output"


def _executables(environment: Path) -> tuple[Path, Path]:
    """The `sai` and `python` executables inside ENVIRONMENT."""
    if os.name == "nt":
        scripts = environment / "Scripts"
        return scripts / "sai.exe", scripts / "python.exe"
    return environment / "bin" / "sai", environment / "bin" / "python"


def registered_launcher(prefix: Path) -> Path | None:
    """The `sai` executable uv installed for the tool at PREFIX, read from
    its receipt. This is the path MCP clients are registered with."""
    try:
        with (prefix / "uv-receipt.toml").open("rb") as handle:
            receipt = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    entries = receipt.get("tool", {}).get("entrypoints", [])
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("name") in ("sai", "sai.exe"):
            path = entry.get("install-path")
            if isinstance(path, str) and os.path.isabs(path):
                return Path(path)
    return None


def _same(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def launcher_owner_error(prefix: Path, launcher: Path) -> str | None:
    """Return why LAUNCHER does not run the install at PREFIX, or None.

    uv links the executable directory's `sai` to the one inside the tool's
    environment. Two tool directories can share an executable directory, and
    then the last install owns `sai` while the other's receipt still names
    it. Updating that other install would take the launcher over.
    """
    inside = _executables(prefix)[0]
    try:
        if launcher.is_symlink() or os.name != "nt":
            if launcher.resolve(strict=True) == inside.resolve(strict=True):
                return None
            # Not a link to this environment: a copied script names the
            # environment's interpreter in its first line.
            with launcher.open("rb") as handle:
                head = handle.read(4096)
            if os.fsencode(str(prefix.resolve())) in head:
                return None
            return f"{launcher} runs another install, not the one at {prefix}"
    except OSError:
        return f"{launcher} is missing or unreadable"
    # Windows copies a launcher executable; its owner cannot be read here.
    return None


def destination_error(
    prefix: Path, launcher: Path, uv: str, runner: Runner
) -> str | None:
    """Return why `uv tool install` would not replace the install at PREFIX
    and its LAUNCHER, or None when it would.

    uv installs into the tool and executable directories of its current
    settings. If those are not where this install lives, or its launcher
    now belongs to another install, an update would change something else.
    """
    found: list[Path] = []
    for extra in ((), ("--bin",)):
        code, output = runner([uv, "tool", "dir", *extra], VERIFY_TIMEOUT)
        if code != 0:
            return f"`uv tool dir` failed: {_last_line(output)}"
        found.append(Path(_last_line(output)))
    tools, executables = found
    if not _same(tools, prefix.parent):
        return f"uv installs tools into {tools}, but this sai is installed at {prefix}"
    if not _same(executables, launcher.parent):
        return (
            f"uv puts executables in {executables}, but this install's `sai`"
            f" is {launcher}"
        )
    return launcher_owner_error(prefix, launcher)


def check_runs(
    sai: Path,
    python: Path,
    version: str,
    imports: Sequence[str],
    runner: Runner,
    *,
    native: bool = False,
) -> str | None:
    """Return why SAI does not print VERSION or PYTHON cannot import the
    package, or None. Python runs isolated (`-I`), so it imports from its own
    environment and never from a working directory or PYTHONPATH."""
    if native:
        with tempfile.TemporaryDirectory(prefix="sai-bridge-profile-") as home:
            problem = native_bridge.smoke(sai, version, Path(home), VERIFY_TIMEOUT)
        if problem is not None:
            return problem
    else:
        code, output = runner([str(sai), "--version"], VERIFY_TIMEOUT)
        if code != 0:
            return f"`sai --version` failed: {_last_line(output)}"
        if not output.strip().endswith(f" {version}"):
            return f"`sai --version` printed {_last_line(output)!r}, not {version}"
    statement = "import " + ", ".join(imports)
    code, output = runner([str(python), "-I", "-c", statement], VERIFY_TIMEOUT)
    if code != 0:
        return f"the package does not import: {_last_line(output)}"
    return None


def verify(
    prefix: Path,
    launcher: Path,
    version: str,
    imports: Sequence[str],
    runner: Runner,
    *,
    native: bool = False,
) -> str | None:
    """Return why the install at PREFIX is not a working VERSION, or None.

    It runs LAUNCHER, the registered `sai` that clients use, after checking
    that it still runs this install.
    """
    owner = launcher_owner_error(prefix, launcher)
    if owner is not None:
        return owner
    return check_runs(
        launcher, _executables(prefix)[1], version, imports, runner, native=native
    )


def preverify(
    spec: str,
    version: str,
    prefix: Path,
    uv: str,
    options: Options,
    *,
    native: bool = False,
    offline: bool = False,
) -> str | None:
    """Install SPEC into a throwaway environment and return why it is not a
    working VERSION, or None.

    This runs before the real install is touched, so a release that cannot
    start never replaces a working one. The environment uses the tool's own
    interpreter and lives in a private temporary directory.
    """
    runner = options.runner
    with tempfile.TemporaryDirectory(prefix="sai-update-") as temp:
        environment = Path(os.path.abspath(temp)) / "venv"
        sai, python = _executables(environment)
        base = str(_executables(prefix)[1])
        install = [uv, "pip", "install"]
        if offline:
            install.append("--offline")
        steps = (
            [uv, "venv", "--python", base, str(environment)],
            [*install, "--python", str(python), spec],
        )
        for step in steps:
            code, output = runner(step, UV_TIMEOUT)
            if code != 0:
                return f"uv {step[1]} failed: {_last_line(output)}"
        failure = check_runs(
            sai, python, version, options.imports, runner, native=native
        )
        if failure is None and native:
            # The new shim must be able to launch the recovery command if uv
            # is interrupted before we can start the installed-path probes.
            failure = native_bridge.keep_recovery(
                python,
                version,
                update.sai_home(),
                prefix.parent / NATIVE_RECOVERY,
                VERIFY_TIMEOUT,
            )
        return failure


def install_spec(
    uv: str, spec: str, runner: Runner, *, offline: bool = False
) -> str | None:
    """Run `uv tool install --force SPEC`; return the error, or None."""
    args = [uv, "tool", "install", "--force"]
    if offline:
        args.append("--offline")
    code, output = runner([*args, spec], UV_TIMEOUT)
    return None if code == 0 else f"uv tool install failed: {_last_line(output)}"


def write_record(prefix: Path, tag: str, digest: str, wheel: Path) -> None:
    """Record that the install at PREFIX is WHEEL of release TAG, whose
    SHA-256 DIGEST matched the release's SHA256SUMS."""
    installed = update.installed_files_hash(prefix)
    if installed is None:
        raise OSError("the install has no RECORD file")
    record = update.ReleaseRecord(tag, digest, installed, str(wheel))
    update.write_json(update.release_record_path(prefix), record.to_json())


def private_cache() -> Path:
    """Return the private wheel cache beneath user-owned, non-writable parents."""
    cache = update.wheel_cache()
    try:
        home = paths.private_directory(cache.parent.parent, profile_root=True)
        if os.name != "nt" and home.stat().st_mode & 0o022:
            raise OSError(f"{home} is writable by other users")
        parent = paths.private_directory(cache.parent, preserve_mode=True)
        if os.name != "nt" and parent.stat().st_mode & 0o022:
            raise OSError(f"{parent} is writable by other users")
        paths.private_directory(cache)
    except OSError as exc:
        raise UpdateFailed(f"the wheel cache is unusable: {exc}") from exc
    return cache


def wheel_error(wheel: tuple[str, str, Path] | None) -> str | None:
    """Hash a kept wheel again, right before uv opens it. Return why it
    cannot be used, or None."""
    if wheel is None:
        return None
    _, digest, path = wheel
    try:
        private_cache()
        intact = path.is_file() and not path.is_symlink() and sha256(path) == digest
    except (OSError, UpdateFailed) as exc:
        return f"the wheel cache is not private: {exc}"
    return None if intact else f"{path.name} changed after it was verified"


def prune_wheels(keep: Sequence[str]) -> None:
    """After a successful update, delete this updater's cached wheels other
    than the versions in KEEP: regular files named exactly like a sai-cli
    release wheel. The caller holds the cache lock."""
    names = {update.wheel_name(version) for version in keep}
    try:
        cache = private_cache()
        for path in cache.iterdir():
            owned = update.is_wheel_name(path.name) and not path.is_symlink()
            if owned and path.is_file() and path.name not in names:
                path.unlink(missing_ok=True)
    except (OSError, UpdateFailed):
        pass


@dataclass(frozen=True)
class Outcome:
    """What an update did, for the terminal, `--json`, and update.json."""

    status: str
    message: str
    installed: str | None = None
    target: str | None = None
    rolled_back: bool = False
    mcp: tuple[mcp_install.RegistrationCheck, ...] = ()
    mcp_notes: tuple[str, ...] = ()

    @property
    def exit_code(self) -> ExitCode:
        if self.status == UNSUPPORTED:
            return EXIT_UNSUPPORTED
        return EXIT_FAILED if self.status == FAILED else EXIT_OK

    def to_json(self) -> dict[str, Any]:
        result = {
            "status": self.status,
            "message": self.message,
            "installed": self.installed,
            "target": self.target,
            "rolledBack": self.rolled_back,
        }
        if self.mcp:
            result["mcp"] = self.mcp_results()
        if self.mcp_notes:
            result["mcpNotes"] = [MCP_NOTES[note] for note in self.mcp_notes]
        return result

    def mcp_results(self) -> list[dict[str, Any]]:
        return [
            {
                "client": check.client,
                "status": check.status,
                "changes": list(check.changes),
                **({"pinned": True} if check.pinned else {}),
                **(
                    {
                        "manualRepairs": [
                            {
                                "scope": repair.scope,
                                "name": repair.name,
                                **({"resetArgs": True} if repair.reset_args else {}),
                            }
                            for repair in check.manual_repairs
                        ],
                        "fix": check.fix,
                    }
                    if check.manual_repairs
                    else {}
                ),
                **(
                    {"legacyName": mcp_install.RETIRED_NAME}
                    if check.legacy_name
                    else {}
                ),
            }
            for check in self.mcp
        ]


@dataclass(frozen=True)
class Options:
    """One update run. The defaults are what `sai update` does."""

    api_base: str = update.API_BASE
    web_base: str = update.WEB_BASE
    git_url: str = update.GIT_URL
    # Install this version instead of the latest; the only way to go back.
    to: str | None = None
    # Build the release from Git instead of installing its wheel.
    source: bool = False
    # Asked when the release is known and again right before uv replaces the
    # install; False records that an update is available and installs
    # nothing. The background run passes the update.auto setting, read
    # fresh, so an opt-out made at any point before the install holds.
    allowed: Callable[[], bool] | None = None
    # The background run: never waits for the lock and never retries a
    # version that already failed.
    background: bool = False
    prefix: Path | None = None
    # The uv to install with; None finds it on PATH.
    uv: str | None = None
    imports: Sequence[str] = IMPORTS
    runner: Runner = run
    total_timeout: float = update.TOTAL_TIMEOUT
    download_timeout: float = DOWNLOAD_TIMEOUT


def _record(
    outcome: Outcome, before: str | None, seen: bool, path: Path | None = None
) -> None:
    """Save OUTCOME as the last update attempt.

    SEEN says the person watched this run, so no notice repeats it later.
    """
    result = {
        "status": outcome.status,
        "from": before,
        "to": outcome.target,
        "at": update.now(),
        "error": outcome.message if outcome.status == FAILED else None,
        "rolledBack": outcome.rolled_back,
    }
    if outcome.mcp:
        result["mcp"] = outcome.mcp_results()
    if outcome.mcp_notes:
        result["mcpNotes"] = list(outcome.mcp_notes)
    changes: dict[str, Any] = {"result": result}
    if seen:
        changes["notified"] = f"{outcome.status}:{outcome.target}"
    save_state(changes, path)


def _resolve(options: Options) -> update.Release | None:
    """The release to install: `--to`, or the latest. None when sai-cli has
    no release yet."""
    if options.to is not None:
        if update.parse_version(options.to) is None:
            raise UpdateFailed(f"{options.to!r} is not a version like X.Y.Z")
        return update.release_for(update.tag_for(options.to), options.web_base)
    try:
        release = update.fetch_latest(
            api_base=options.api_base,
            web_base=options.web_base,
            total_timeout=options.total_timeout,
        )
    except update.NoRelease:
        save_state({"latest": None, "checkedAt": update.now()})
        return None
    except update.UpdateError as exc:
        raise UpdateFailed(f"update check failed: {exc}") from exc
    save_state({"latest": release.tag, "checkedAt": update.now()})
    return release


@dataclass(frozen=True)
class Current:
    """The install an update starts from, read under the tool lock."""

    install: update.Install
    version: str
    # The registered `sai`, kept for the whole update.
    launcher: Path
    # The release record, when the install came from a verified wheel.
    record: update.ReleaseRecord | None

    @property
    def prefix(self) -> Path:
        return Path(self.install.prefix)


@dataclass(frozen=True)
class Rollback:
    """What puts the current version back, prepared before the install is
    replaced: a copy of the environment, a verified wheel, or both."""

    # A copy of the tool's environment directory; restored first, because
    # swapping it back needs neither uv nor any package source.
    snapshot: Path | None = None
    # (tag, sha256, path) of a wheel verified against its release; used only
    # if the copy cannot be restored.
    wheel: tuple[str, str, Path] | None = None


def _download_to_cache(
    release: update.Release, options: Options
) -> tuple[Path, str, bool]:
    """Download and verify RELEASE's wheel in a private directory, then keep
    it in the wheel cache. Return its absolute path, its SHA-256, and
    whether the cache had no such file before."""
    cache = private_cache()
    with tempfile.TemporaryDirectory(dir=cache, prefix=".download-") as temp:
        wheel, digest = fetch_wheel(release, Path(temp), options.download_timeout)
        kept = cache / wheel.name
        created = not kept.exists()
        os.replace(wheel, kept)
    return kept, digest, created


def clear_snapshots(prefix: Path) -> None:
    """Remove environment copies an interrupted update left beside PREFIX.
    The caller holds the tool lock."""
    with contextlib.suppress(OSError):
        for path in prefix.parent.iterdir():
            if path.name.startswith((SNAPSHOT_PREFIX, BROKEN_PREFIX)):
                shutil.rmtree(path, ignore_errors=True)


def snapshot_environment(prefix: Path) -> Path | None:
    """Copy the tool's environment at PREFIX to a private sibling directory
    and return it, or None when it cannot be copied. The copy sits in uv's
    tool directory, so swapping it back is a rename on one filesystem."""
    target = prefix.parent / f"{SNAPSHOT_PREFIX}{os.getpid()}"
    try:
        shutil.copytree(prefix, target, symlinks=True)
        target.chmod(0o700)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        return None
    return target


def swap_back(prefix: Path, snapshot: Path, launcher: Path) -> str | None:
    """Put the copied environment SNAPSHOT back at PREFIX and make LAUNCHER
    run it again. Return the error, or None."""
    broken = prefix.parent / f"{BROKEN_PREFIX}{os.getpid()}"
    try:
        shutil.rmtree(broken, ignore_errors=True)
        if prefix.exists():
            os.rename(prefix, broken)
        os.rename(snapshot, prefix)
        shutil.rmtree(broken, ignore_errors=True)
        if os.name != "nt" and launcher_owner_error(prefix, launcher) is not None:
            launcher.unlink(missing_ok=True)
            launcher.symlink_to(_executables(prefix)[0])
    except OSError as exc:
        return f"could not put the previous environment back: {type(exc).__name__}"
    return None


def bridge_wheel_path(tools: Path, version: str, digest: str) -> Path:
    return tools / NATIVE_WHEELS / digest / update.wheel_name(version)


def rollback_wheel(current: Current, options: Options) -> tuple[str, str, Path] | None:
    """Use the verified wheel kept by the updater or offline recovery.
    Download the current release's wheel if neither cache has it."""
    record, install = current.record, current.install
    if record is not None and record.wheel:
        kept = Path(record.wheel)
        try:
            recovery_wheel = bridge_wheel_path(
                current.prefix.parent.resolve(), record.version, record.sha256
            )
            if kept == recovery_wheel:
                in_cache = kept.resolve(strict=True) == kept
            else:
                in_cache = kept.is_absolute() and kept.parent == private_cache()
            intact = (
                in_cache
                and kept.is_file()
                and not kept.is_symlink()
                and sha256(kept) == record.sha256
            )
        except (OSError, UpdateFailed):
            intact = False
        if intact:
            return record.tag, record.sha256, kept
    if install.kind != update.UV_TOOL_RELEASE:
        return None
    tag = update.tag_for(current.version)
    try:
        release = update.release_for(tag, options.web_base)
        wheel, digest, _ = _download_to_cache(release, options)
    except (UpdateFailed, OSError):
        return None
    return tag, digest, wheel


def prepare_rollback(current: Current, options: Options) -> Rollback | None:
    """Get what can put CURRENT's version back, before anything is replaced,
    so a rollback needs no network. None when nothing can be had; the update
    then stops.

    The environment is always copied. A wheel reinstall would make uv
    resolve the old version's dependencies again, which can fail when one
    was withdrawn; the copy already holds them. A verified wheel is the
    spare, for when the copy cannot be made or restored.
    """
    snapshot = snapshot_environment(current.prefix)
    wheel = rollback_wheel(current, options)
    if snapshot is None and wheel is None:
        return None
    return Rollback(snapshot, wheel)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError as error:
        if sys.platform != "darwin" or error.errno != errno.EINVAL:
            raise
    finally:
        os.close(fd)


def publish_bridge_wheel(
    tools: Path, wheel: tuple[str, str, Path]
) -> tuple[Path, dict[str, str]]:
    tag, digest, source = wheel
    version = update.tag_version(tag)
    cache = tools / NATIVE_WHEELS
    destination = bridge_wheel_path(tools, version, digest)
    for directory in (cache, destination.parent):
        paths.private_directory(directory)
        if directory.resolve() != directory:
            raise UpdateFailed("the native recovery wheel directory changed")
        sync_directory(directory.parent)
    fd, tmp = tempfile.mkstemp(dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        if sha256(Path(tmp)) != digest:
            raise UpdateFailed("the bridge wheel changed after verification")
        with contextlib.suppress(FileExistsError):
            os.link(tmp, destination)
        if (
            destination.resolve(strict=True) != destination
            or sha256(destination) != digest
        ):
            raise UpdateFailed("the native recovery wheel does not match its digest")
        sync_directory(destination.parent)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return destination, {"version": version, "digest": digest}


def record_bridge(
    current: Current, previous: tuple[str, str, Path], candidate: tuple[str, str, Path]
) -> tuple[Path, Path]:
    """Write the same tool-local rollback record used by the Rust updater."""
    tools = current.prefix.parent.resolve(strict=True)
    python = _executables(current.prefix)[1].resolve(strict=True)
    if python.is_relative_to(current.prefix.resolve()):
        raise UpdateFailed(
            "uv's base Python interpreter must be outside the tool environment"
        )
    original = update.installed_files_hash(current.prefix)
    if original is None:
        raise UpdateFailed("could not identify the current Python installation")
    recovery = tools / NATIVE_RECOVERY
    if recovery.resolve(strict=True) != recovery or not recovery.is_file():
        raise UpdateFailed("the offline recovery binary is missing or changed")
    rollback, old = publish_bridge_wheel(tools, previous)
    path, new = publish_bridge_wheel(tools, candidate)
    pending = tools / NATIVE_PENDING
    update.write_json(
        pending,
        {
            **old,
            "next": new,
            "launcher": str(current.launcher),
            "python": str(python),
            "original_record": original,
            "recovery": str(recovery),
        },
    )
    with pending.open("rb") as handle:
        os.fsync(handle.fileno())
    sync_directory(tools)
    return rollback, path


def clear_bridge_record(prefix: Path) -> None:
    pending = prefix.parent / NATIVE_PENDING
    pending.unlink()
    (pending.parent / NATIVE_RECOVERY).unlink(missing_ok=True)
    sync_directory(pending.parent)


def finish_previous_bridge(current: Current) -> None:
    """Clear an interrupted transaction only when Python is already restored."""
    pending = current.prefix.parent / NATIVE_PENDING
    try:
        with pending.open("rb") as handle:
            record = json.loads(handle.read(MAX_SUMS))
    except FileNotFoundError:
        return
    except ValueError as error:
        raise UpdateFailed("could not read the pending uv install record") from error
    if not isinstance(record, dict) or (
        record.get("version") != current.version
        or record.get("launcher") != str(current.launcher)
        or (
            record.get("original_record") != update.installed_files_hash(current.prefix)
            and (
                current.record is None or current.record.sha256 != record.get("digest")
            )
        )
    ):
        raise UpdateFailed("an interrupted uv bridge requires `sai update --recover`")
    clear_bridge_record(current.prefix)


def restore(
    current: Current,
    rollback: Rollback,
    options: Options,
    uv: str,
    *,
    native: bool = False,
) -> str | None:
    """Restore and verify CURRENT. A shim handoff tries uv's offline wheel
    reinstall first. Full Python updates prefer the environment copy."""

    def runs() -> str | None:
        return verify(
            current.prefix,
            current.launcher,
            current.version,
            options.imports,
            options.runner,
        )

    def reinstall(wheel: tuple[str, str, Path]) -> str | None:
        error = (
            wheel_error(wheel)
            or install_spec(uv, str(wheel[2]), options.runner, offline=native)
            or runs()
        )
        if error is None:
            with contextlib.suppress(OSError):
                write_record(current.prefix, *wheel)
        return error

    error: str | None = "nothing to roll back to"
    if native and rollback.wheel is not None:
        error = reinstall(rollback.wheel)
        if error is None:
            return None
    if rollback.snapshot is not None:
        swapped = swap_back(current.prefix, rollback.snapshot, current.launcher)
        error = swapped or runs()
        if error is None:
            return None
    if not native and rollback.wheel is not None:
        error = reinstall(rollback.wheel)
    return error


@contextlib.contextmanager
def _uninterrupted() -> Generator[None]:
    """Ignore Ctrl-C while a rollback runs. Child processes inherit the
    ignored signal, so the terminal's interrupt does not stop uv either."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _install(
    options: Options, current: Current, release: update.Release, uv: str
) -> tuple[Outcome, bool]:
    """Install RELEASE over CURRENT. Returns the outcome and whether Ctrl-C
    interrupted the run. The caller holds the tool lock and the cache lock.

    The order is what keeps a working `sai`: stage and hash the new wheel,
    run it in a throwaway environment, copy the current environment to roll
    back to, and only then let uv replace the install. Without a rollback it stops. The
    check afterwards and the rollback are the second line. The cache is
    pruned only after the new version has passed that check.
    """
    prefix, installed, target = current.prefix, current.version, release.version
    runner, imports = options.runner, options.imports
    wheel: tuple[str, str, Path] | None = None
    created = False
    native = False

    def discard() -> None:
        """Remove the wheel this run downloaded and did not install."""
        if wheel is not None and created and target != installed:
            with contextlib.suppress(OSError):
                wheel[2].unlink(missing_ok=True)
        if native and not (prefix.parent / NATIVE_PENDING).exists():
            with contextlib.suppress(OSError):
                (prefix.parent / NATIVE_RECOVERY).unlink(missing_ok=True)

    def unchanged(reason: str) -> tuple[Outcome, bool]:
        discard()
        message = (
            f"Could not update to {target}: {reason}. sai {installed} is unchanged."
        )
        return Outcome(FAILED, message, installed, target), False

    def available() -> tuple[Outcome, bool]:
        discard()
        message = f"sai {target} is available. Run `sai update` to install it."
        return Outcome(AVAILABLE, message, installed, target), False

    if options.source:
        # The full ref: a branch named like the tag must not shadow it.
        spec = f"git+{options.git_url}@refs/tags/{release.tag}"
    else:
        try:
            kept, digest, created = _download_to_cache(release, options)
        except (UpdateFailed, OSError) as exc:
            return unchanged(
                str(exc) if isinstance(exc, UpdateFailed) else type(exc).__name__
            )
        spec, wheel = str(kept), (release.tag, digest, kept)
        try:
            native = native_bridge.is_shim(kept)
        except zipfile.BadZipFile:
            # Keep uv's existing trial-install diagnostic for invalid wheels.
            pass
        except OSError as error:
            return unchanged(
                f"could not inspect the release wheel: {type(error).__name__}"
            )
        if native and not native_bridge.supported():
            discard()
            message = (
                f"Sai {target} requires a native binary for a supported platform. "
                f"Keep your full Python release `sai-cli=={installed}`."
            )
            return Outcome(UNSUPPORTED, message, installed, target), False
        if native and current.install.kind == update.UV_TOOL_GIT:
            return unchanged(
                "this uv Git install needs a verified full-Python bridge wheel first."
                " Run `uv tool uninstall sai-cli`, then"
                f" `{update.INSTALL_COMMAND} --version VERSION` with the published"
                " full-Python bridge version. Then run `sai update`"
            )

    trial = wheel_error(wheel) or preverify(
        spec, target, prefix, uv, options, native=native
    )
    if trial is not None:
        return unchanged(f"the release failed its trial install: {trial}")
    rollback = prepare_rollback(current, options)
    if rollback is None:
        return unchanged(
            f"nothing to roll back to could be prepared: no copy of the"
            f" environment and no verified wheel of {installed}"
        )
    retain_snapshot = False
    pending_written = False
    try:
        if options.allowed is not None and not options.allowed():
            # Turned off while this run downloaded and tested the release.
            return available()

        def failed(reason: str) -> tuple[Outcome, bool]:
            """Report REASON after making sure the previous version runs."""
            was_replaced = native and update.installed_version(prefix) != installed
            if (
                not was_replaced
                and verify(prefix, current.launcher, installed, imports, runner) is None
            ):
                message = f"{reason}. sai {installed} is unchanged."
                return Outcome(FAILED, message, installed, target), True
            error = restore(current, rollback, options, uv, native=native)
            if error is None:
                message = f"{reason}. Rolled back to sai {installed}."
                return Outcome(
                    FAILED, message, installed, target, rolled_back=True
                ), True
            message = (
                f"{reason}. Rolling back to sai {installed} also failed: {error}."
                f" Reinstall with: {update.INSTALL_COMMAND}"
            )
            return Outcome(FAILED, message, installed, target), False

        interrupted = False
        try:
            # The wheel is hashed once more right before uv opens it.
            error = wheel_error(wheel)
            if error is None and native:
                assert wheel is not None
                if rollback.wheel is None:
                    return unchanged(
                        "the bridge requires a verified previous Python wheel"
                    )
                trial = preverify(
                    str(rollback.wheel[2]), installed, prefix, uv, options, offline=True
                )
                if trial is not None:
                    return unchanged(
                        f"the rollback wheel failed its offline trial install: {trial}"
                    )
                previous, kept = record_bridge(current, rollback.wheel, wheel)
                pending_written = True
                if options.allowed is not None and not options.allowed():
                    clear_bridge_record(prefix)
                    return available()
                rollback = replace(
                    rollback, wheel=(rollback.wheel[0], rollback.wheel[1], previous)
                )
                spec, wheel = str(kept), (release.tag, wheel[1], kept)
                # The tool-local wheel is the durable recovery source.
                created = False
            if error is None:
                if native and not options.background:
                    recovery = shlex.join(
                        [str(prefix.parent / NATIVE_RECOVERY), "update", "--recover"]
                    )
                    print(
                        f"If the uv switch is interrupted, run {recovery}.",
                        file=sys.stderr,
                        flush=True,
                    )
                error = install_spec(uv, spec, runner)
            if error is None:
                error = verify(
                    prefix, current.launcher, target, imports, runner, native=native
                )
            if error is None and native:
                # The isolated probes leave the invoking profile's cache cold.
                error = native_bridge.smoke(
                    current.launcher,
                    target,
                    update.sai_home(),
                    VERIFY_TIMEOUT,
                    version_only=True,
                )
            if error is None and wheel is not None:
                try:
                    write_record(prefix, *wheel)
                except OSError as exc:
                    error = f"could not record the release: {type(exc).__name__}"
        except KeyboardInterrupt:
            interrupted = True
            error = "interrupted"
        except (OSError, UpdateFailed) as exc:
            error = str(exc)
        if error is not None:
            with _uninterrupted():
                outcome, recovered = failed(f"Could not update to {target}: {error}")
                retain_snapshot = native and not recovered
                if recovered and pending_written:
                    with contextlib.suppress(OSError):
                        clear_bridge_record(prefix)
                if not retain_snapshot:
                    discard()
            return outcome, interrupted
        if wheel is None:
            # A Git install has no release record; drop a stale one.
            with contextlib.suppress(OSError):
                update.release_record_path(prefix).unlink(missing_ok=True)
        # Only now, verified: keep the new wheel and the one to go back to.
        prune_wheels((installed, target))
        retain_snapshot = native
        if pending_written:
            try:
                clear_bridge_record(prefix)
            except OSError as exc:
                return Outcome(
                    FAILED, f"Could not finish the uv bridge: {exc}", target, target
                ), False
        message = (
            f"Updated sai from {installed} to {target}. Restart agent clients and"
            " any open `sai` shell so they load it."
        )
        return Outcome(UPDATED, message, target, target), False
    finally:
        if rollback.snapshot is not None and not retain_snapshot:
            shutil.rmtree(rollback.snapshot, ignore_errors=True)


def _unsupported(install: update.Install) -> Outcome:
    guide = update.guidance(install)
    note = guide.note + (f" {guide.command}" if guide.command else "")
    message = f"`sai update` only updates an official uv tool install. {note}"
    version = update.installed_version(Path(install.prefix))
    return Outcome(UNSUPPORTED, message, version)


def update_now(options: Options | None = None) -> Outcome:
    """Check for a release and install it. Never raises for an update error;
    the outcome carries it. Ctrl-C is raised again after any rollback."""
    options = options or Options()
    install = update.install_kind(options.prefix, options.web_base)
    if not install.official:
        return _unsupported(install)
    prefix = Path(os.path.abspath(install.prefix))
    wait = 0.0 if options.background else LOCK_WAIT
    with locked(tool_lock_path(prefix), wait) as held:
        if not held:
            message = "Another sai update is still running; try again shortly."
            status = BUSY if options.background else FAILED
            return Outcome(status, message, update.installed_version(prefix))
        # Everything about the install is read after taking the lock: an
        # update that just finished may have changed its version and origin.
        install = update.install_kind(prefix, options.web_base)
        if not install.official:
            return _unsupported(install)
        installed = update.installed_version(prefix)
        if installed is None:
            return Outcome(FAILED, "Could not read the installed sai version.")
        if options.background:
            save_state({"startedAt": time.time()})
        try:
            release = _resolve(options)
        except UpdateFailed as exc:
            # A failed lookup is not an update attempt, so it is not recorded.
            return Outcome(FAILED, f"{exc}.", installed, options.to)
        if release is None:
            message = "No sai-cli release is published yet; nothing to update to."
            return Outcome(NO_RELEASE, message, installed)
        target = release.version
        if target == installed:
            return Outcome(
                CURRENT, f"sai {installed} is up to date.", installed, target
            )
        if options.to is None and not update.is_newer(release.tag, installed):
            message = (
                f"sai {installed} is newer than the latest release {target};"
                f" nothing to do. `sai update --to {target}` installs it anyway."
            )
            return Outcome(CURRENT, message, installed, target)
        seen = not options.background
        if options.allowed is not None and not options.allowed():
            message = f"sai {target} is available. Run `sai update` to install it."
            outcome = Outcome(AVAILABLE, message, installed, target)
            _record(outcome, installed, seen)
            return outcome
        last = read_state().get("result")
        if (
            options.background
            and isinstance(last, dict)
            and (last.get("status"), last.get("to")) == (FAILED, target)
        ):
            message = f"The update to {target} already failed; run `sai update`."
            return Outcome(FAILED, message, installed, target)
        found = options.uv or find_uv()
        if found is None:
            outcome = Outcome(
                FAILED, "uv is not on PATH, so sai cannot update.", installed, target
            )
            _record(outcome, installed, seen)
            return outcome
        uv = os.path.abspath(found)
        launcher = registered_launcher(prefix)
        mismatch = (
            "this install's uv receipt names no `sai` executable"
            if launcher is None
            else destination_error(prefix, launcher, uv, options.runner)
        )
        if mismatch is not None or launcher is None:
            message = (
                f"`sai update` stopped before changing anything: {mismatch}."
                " Run it with the uv settings this sai was installed with."
            )
            return Outcome(UNSUPPORTED, message, installed, target)
        record = update.read_release_record(prefix)
        current = Current(install, installed, launcher, record)
        try:
            finish_previous_bridge(current)
        except (OSError, UpdateFailed) as error:
            return Outcome(FAILED, str(error), installed, target)
        clear_snapshots(prefix)
        # The cache lock comes second, always after the tool lock, and is
        # held until the install is verified or rolled back.
        # Capture the absolute state path while a relative SAI_HOME can
        # still be resolved, even if the working directory goes away.
        state_path = update.cache_path()
        with contextlib.ExitStack() as stack:
            try:
                cache_held = stack.enter_context(locked(cache_lock_path(), wait))
            except OSError as exc:
                return Outcome(
                    FAILED, f"Could not lock the wheel cache: {exc}", installed, target
                )
            if not cache_held:
                message = (
                    "Another sai update is using this SAI_HOME's wheel cache;"
                    " try again shortly."
                )
                status = BUSY if options.background else FAILED
                return Outcome(status, message, installed, target)
            outcome, interrupted = _install(options, current, release, uv)
        try:
            if outcome.status == UPDATED:
                outcome = repair_after_update(
                    outcome, current.launcher, options.background
                )
        finally:
            _record(outcome, installed, seen, state_path)
        if interrupted:
            raise KeyboardInterrupt
        return outcome


def repair_after_update(outcome: Outcome, launcher: Path, background: bool) -> Outcome:
    """Keep registration errors separate from the verified install outcome."""
    notes: list[str] = []
    cwd = None
    if background:
        notes.append("background")
    else:
        try:
            cwd = Path.cwd()
        except OSError:
            notes.append("cwd-missing")
    checks: list[mcp_install.RegistrationCheck] = []
    try:
        target = mcp_install.launcher_target(str(launcher), {})
        checks = mcp_install.check_registrations(target, fix=True, cwd=cwd)
    except Exception:  # noqa: BLE001 - the installation has already succeeded
        notes.append("failed")
    outcome = replace(
        outcome,
        mcp=tuple(
            check
            for check in checks
            if check.status in ("fixed", "fail") or check.pinned or check.legacy_name
        ),
        mcp_notes=tuple(notes),
    )
    if outcome.mcp or notes:
        level = (
            "warn"
            if "failed" in notes
            or any(c.status in ("warn", "fail") for c in outcome.mcp)
            else "info"
        )
        log.event(
            "update.mcp_repair",
            level,
            background=background,
            clients=outcome.mcp_results(),
            notes=notes,
        )
    return outcome


# Automatic updates


def mcp_notice(results: Any, notes: Any = None) -> str:
    """Summarize this update's repairs using only known client names."""
    repaired, failed, pinned, legacy = [], [], [], []
    claude_manual: list[mcp_install.ClaudeRepair] = []
    old_claude_notice = False
    for entry in results if isinstance(results, list) else []:
        if (
            not isinstance(entry, dict)
            or entry.get("client") not in mcp_install.CLIENTS
        ):
            continue
        if entry.get("status") == "fixed" or (
            entry.get("status") == "warn" and entry.get("changes")
        ):
            repaired.append(entry["client"])
        manual = entry.get("manualRepairs")
        if entry["client"] == "claude" and isinstance(manual, list):
            claude_manual.extend(
                mcp_install.ClaudeRepair(
                    repair["scope"], repair["name"], repair.get("resetArgs") is True
                )
                for repair in manual
                if isinstance(repair, dict)
                and repair.get("scope") in mcp_install.UNINSTALL_SCOPES
                and repair.get("name") in mcp_install.SERVER_NAMES
            )
        if entry.get("status") == "fail" and not manual:
            failed.append(entry["client"])
        keeps_name = entry.get("legacyName") == mcp_install.RETIRED_NAME
        if keeps_name:
            legacy.append(entry["client"])
        old_blocked = (
            entry.get("client") == "claude" and entry.get("policyBlocked") is True
        )
        old_claude_notice |= old_blocked
        # Older update records used warn exclusively for a pinned launcher.
        if entry.get("pinned") is True or (
            entry.get("status") == "warn" and not keeps_name and not old_blocked
        ):
            pinned.append(entry["client"])
    messages = []
    if repaired:
        messages.append(f"Repaired Sai MCP registrations for {', '.join(repaired)}.")
    if failed:
        messages.append(
            f"Sai MCP registrations need attention for {', '.join(failed)}."
            " Run `sai doctor` for details."
        )
    if pinned:
        messages.append(f"Sai MCP uses a pinned launcher for {', '.join(pinned)}.")
    if claude_manual:
        messages.append(
            "Claude Code MCP registrations need manual repair. "
            + mcp_install.claude_repair_hint(tuple(claude_manual))
        )
    elif old_claude_notice:
        messages.append(
            "Claude Code MCP registrations need manual repair. Run `sai doctor` for the repair command."
        )
    if legacy:
        messages.append(
            f"Sai MCP kept the existing {mcp_install.RETIRED_NAME} name"
            f" for {', '.join(legacy)}."
        )
    if isinstance(notes, (list, tuple)):
        messages.extend(
            MCP_NOTES[note]
            for note in notes
            if isinstance(note, str) and note in MCP_NOTES
        )
    return " ".join(messages)


def notice(state: dict[str, Any], installed: str | None) -> tuple[str, str] | None:
    """Return (key, text) of the notice STATE calls for, or None.

    The key names the state, so each state is announced once.
    """
    result = state.get("result")
    if not isinstance(result, dict):
        return None
    status, target = result.get("status"), result.get("to")
    if not isinstance(target, str) or update.parse_version(target) is None:
        return None
    key = f"{status}:{target}"
    if state.get("notified") == key:
        return None
    if status == UPDATED:
        message = (
            f"sai updated itself to {target}. Restart agent clients so they load it."
        )
        return key, " ".join(
            filter(
                None, [message, mcp_notice(result.get("mcp"), result.get("mcpNotes"))]
            )
        )
    if installed is not None and not update.is_newer(update.tag_for(target), installed):
        return None
    if status == AVAILABLE:
        return key, f"sai {target} is available. Run `sai update` to install it."
    if status == FAILED:
        return key, (
            f"sai could not update itself to {target}. Run `sai update` to retry,"
            " or `sai doctor` for the reason."
        )
    return None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def due(state: dict[str, Any], now: float | None = None) -> bool:
    """True when a background run should start.

    That is a day after the last one was started, or never. A run that was
    started but never began work, because the process did not start or an
    update held the tool lock, is retried after RETRY_INTERVAL.
    """
    scheduled, started = (
        _number(state.get("scheduledAt")),
        _number(state.get("startedAt")),
    )
    if scheduled is None:
        return True
    age = (time.time() if now is None else now) - scheduled
    if age >= CHECK_INTERVAL:
        return True
    began = started is not None and started >= scheduled
    return not began and age >= RETRY_INTERVAL


def background_command() -> list[str]:
    """The detached worker. `-I` keeps Python from importing from the
    directory the person happens to be in, or from PYTHONPATH."""
    return [sys.executable, "-I", "-m", "sai_cli", "update", "--background"]


def spawn(command: Sequence[str]) -> None:
    """Start COMMAND detached, so it outlives this process and shares no
    terminal with it."""
    # A neutral directory, and SAI_HOME made absolute first so a relative
    # one still means the same place there.
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": NEUTRAL_DIR,
        "env": {**os.environ, "SAI_HOME": str(update.sai_home())},
    }
    if sys.platform == "win32":
        options["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        options["start_new_session"] = True
    subprocess.Popen(list(command), **options)


def tick(
    echo: Callable[[str], None],
    start: Callable[[Sequence[str]], None] = spawn,
    prefix: Path | None = None,
) -> None:
    """Run on an interactive command: print a pending notice once, and start
    the daily background update when it is due.

    An install `sai update` does not change gets neither. The notice is
    read, printed, and marked under the state lock, and the daily run is
    reserved under it, so two terminals neither repeat a notice nor start two
    runs. It never raises and never waits on the network; the command the
    person ran comes first.
    """
    try:
        if not update.install_kind(prefix).official:
            return
        launch = False
        with locked() as held:
            if not held:
                # Another command is doing this right now.
                return
            state = read_state()
            changes: dict[str, Any] = {}
            pending = notice(state, update.installed_version(prefix))
            if pending is not None:
                echo(pending[1])
                changes["notified"] = pending[0]
            if due(state):
                changes["scheduledAt"] = time.time()
                launch = True
            if changes:
                write_state(changes)
        if launch:
            start(background_command())
            log.event("update.scheduled")
    except Exception:  # noqa: BLE001 - an update problem must not break a command
        return
