# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Install a verified Sai release.

Finds the highest `sai-cli/vX.Y.Z` release of SaiDotFun/open-sai, downloads
its wheel and SHA256SUMS, checks the hash, and runs `uv tool install` on the
downloaded file. `--source` builds that release's tag from Git instead.
Before the first release exists, it installs from `main` and says so.
Pass `--native` for the standalone Linux and macOS install.

What it checks: the wheel it downloads must match the release's SHA256SUMS,
and the cache it keeps the wheel in must be private to you. It does not defend
against other local users who can write to the directories above SAI_HOME;
they can already replace your tools.

It needs only uv and the standard library, so it runs straight from GitHub:

    uv run https://raw.githubusercontent.com/SaiDotFun/open-sai/main/scripts/install.py

Usage: install.py [--native | --source] [--version VERSION] [--dry-run]
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import gzip
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Generator
from contextlib import contextmanager
from http.client import HTTPException
from pathlib import Path
from typing import Any, BinaryIO, TypeVar
from urllib.parse import quote, urljoin, urlsplit

# Keep these in step with cli/sai_cli/core/update.py; a test compares them.
REPO = "SaiDotFun/open-sai"
API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"
TAG_PREFIX = "sai-cli/v"
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_BODY = 16 << 20
MAX_WHEEL = 64 << 20
MAX_SUMS = 1 << 16
REQUEST_TIMEOUT = 10.0
# Wall-clock limits for the release lookup and for the two downloads.
TOTAL_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0
# GitHub's API limits unauthenticated clients.
FALLBACK_STATUSES = frozenset({403, 429})
RECORD_NAME = ".sai-cli-release.json"
RECORD_SCHEMA = 1

NUMBER = r"(0|[1-9][0-9]*)"
TAG = re.compile(rf"^{re.escape(TAG_PREFIX)}{NUMBER}\.{NUMBER}\.{NUMBER}$")
NATIVE_VERSION = rf"{NUMBER}\.{NUMBER}\.{NUMBER}(?:-rc\.{NUMBER})?"
DIGEST = r"[0-9a-f]{64}"
NATIVE_DIRECTORY = re.compile(rf"{NATIVE_VERSION}-{DIGEST}")
NATIVE_ROOT = ".sai-native"
NATIVE_LOCK = ".sai-update.lock"
NATIVE_PENDING = "pending.json"
LAUNCHER_SWITCH = ".launcher-switch/sai"
MAX_BINARY = 96 << 20
VERIFY_TIMEOUT = 60

T = TypeVar("T")


class LookupFailed(Exception):
    """The latest release could not be determined or downloaded."""


class LauncherChanged(LookupFailed):
    """A different install now owns the launcher."""


def parse_tag(tag: str) -> tuple[int, int, int] | None:
    """Return the version of a sai-cli release tag, or None for anything else,
    including pre-release tags and other components' tags."""
    match = TAG.match(tag)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def latest_tag(releases: list[Any]) -> str | None:
    """Return the highest sai-cli release tag in RELEASES, or None."""
    best: tuple[tuple[int, int, int], str] | None = None
    for item in releases:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        tag = item.get("tag_name")
        version = parse_tag(tag) if isinstance(tag, str) else None
        if version is not None and (best is None or version > best[0]):
            best = (version, tag)
    return best[1] if best else None


def within(
    seconds: float,
    what: str,
    work: Callable[[float], T],
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> T:
    """Run WORK(deadline) and give up after SECONDS of wall-clock time.

    Socket timeouts only bound each read, so a server that keeps sending a
    little could hold a request open forever. WORK runs in a daemon thread
    that this stops waiting for at the deadline or when CHECK_CANCELLED raises.
    """
    deadline = time.monotonic() + seconds
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = work(deadline)
        except BaseException as error:  # noqa: BLE001 - re-raised in the caller
            outcome["error"] = error

    worker = threading.Thread(target=run, name="sai-install", daemon=True)
    worker.start()
    while worker.is_alive():
        if check_cancelled is not None:
            check_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LookupFailed(f"timed out {what}")
        worker.join(min(0.1, remaining) if check_cancelled is not None else remaining)
    if check_cancelled is not None:
        check_cancelled()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def left(deadline: float, what: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LookupFailed(f"timed out {what}")
    return min(REQUEST_TIMEOUT, remaining)


def request(
    url: str, method: str, deadline: float
) -> tuple[int, dict[str, str], bytes]:
    """Return the status, lower-cased headers, and body of URL without
    following redirects."""
    what = "looking up the latest release"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "sai-cli"}
    opener = urllib.request.build_opener(NoRedirect)
    call = urllib.request.Request(url, method=method, headers=headers)
    try:
        with opener.open(call, timeout=left(deadline, what)) as response:
            body = bytearray()
            while len(body) <= MAX_BODY:
                left(deadline, what)
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                body += chunk
            found = {name.lower(): value for name, value in response.headers.items()}
            return response.status, found, bytes(body)
    except urllib.error.HTTPError as error:
        found = {name.lower(): value for name, value in error.headers.items()}
        return error.code, found, b""
    except HTTPException as error:
        raise LookupFailed(
            f"incomplete or invalid GitHub HTTP response: {type(error).__name__}"
        ) from error
    except (OSError, ValueError) as error:
        raise LookupFailed(f"could not reach GitHub: {type(error).__name__}") from error


RATE_LIMITED = (
    "GitHub's API is rate limited or failing, so the latest sai-cli release"
    " cannot be determined; try again later"
)


def from_api(api_base: str, deadline: float) -> tuple[bool, str | None]:
    """Return (answered, tag) after reading every page of the release list.

    `answered` is False when the API is rate limited or failing. The list is
    ordered by date, not version, so no page is skipped, and only the last
    page proves that sai-cli has no release.
    """
    releases: list[Any] = []
    for page in range(1, MAX_PAGES + 1):
        url = (
            f"{api_base.rstrip('/')}/repos/{REPO}/releases"
            f"?per_page={PAGE_SIZE}&page={page}"
        )
        status, headers, body = request(url, "GET", deadline)
        if status in FALLBACK_STATUSES or status >= 500:
            return False, None
        if status != 200:
            raise LookupFailed(f"GitHub API answered HTTP {status}")
        if len(body) > MAX_BODY:
            raise LookupFailed("GitHub API answer is too large")
        try:
            found = json.loads(body)
        except ValueError as error:
            raise LookupFailed("GitHub API answered with malformed JSON") from error
        if not isinstance(found, list):
            raise LookupFailed("GitHub API answered with malformed JSON")
        releases.extend(found)
        if 'rel="next"' not in headers.get("link", ""):
            return True, latest_tag(releases)
    raise LookupFailed(
        f"the release list is longer than {MAX_PAGES} pages; the lookup is incomplete"
    )


def any_release(web_base: str, deadline: float) -> bool:
    """Ask the web /releases/latest redirect whether any release exists.

    The page follows GitHub's single latest release for the whole
    repository, so the tag it names is not an answer, only proof of a
    release.
    """
    url = f"{web_base.rstrip('/')}/{REPO}/releases/latest"
    status, headers, _ = request(url, "HEAD", deadline)
    location = headers.get("location")
    if status not in (301, 302, 303, 307, 308) or not location:
        raise LookupFailed(f"release page answered HTTP {status} without a redirect")
    # Exactly the release list means no release. A tag whose name ends in
    # `/releases`, such as `docs/releases`, is a release.
    listed = urlsplit(location).path.rstrip("/").lower()
    return listed != f"/{REPO}/releases".lower()


def find_latest(
    api_base: str = API_BASE,
    web_base: str = WEB_BASE,
    timeout: float = TOTAL_TIMEOUT,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> str | None:
    """Return the latest sai-cli release tag, or None when there is none yet."""

    def work(deadline: float) -> str | None:
        answered, tag = from_api(api_base, deadline)
        if answered:
            return tag
        if any_release(web_base, deadline):
            raise LookupFailed(RATE_LIMITED)
        return None

    return within(
        timeout, "looking up the latest release", work, check_cancelled=check_cancelled
    )


def version_of(tag: str) -> str:
    return tag[len(TAG_PREFIX) :]


def wheel_name(version: str) -> str:
    return f"sai_cli-{version}-py3-none-any.whl"


def download_base(tag: str, web_base: str = WEB_BASE) -> str:
    return f"{web_base.rstrip('/')}/{REPO}/releases/download/{quote(tag)}"


def wheel_url(tag: str, web_base: str = WEB_BASE) -> str:
    return f"{download_base(tag, web_base)}/{wheel_name(version_of(tag))}"


class AssetRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: str):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(urljoin(req.full_url, newurl))
        origin = f"{target.scheme}://{target.netloc}"
        official = (
            self.origin == WEB_BASE
            and target.scheme == "https"
            and (
                target.netloc
                in (
                    "github.com",
                    "release-assets.githubusercontent.com",
                    "objects.githubusercontent.com",
                )
            )
        )
        if origin != self.origin and not official:
            raise LookupFailed("the release redirected outside its download hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(
    url: str, target: Path, limit: int, deadline: float, *, origin: str | None = None
) -> None:
    """Save URL to TARGET, following redirects, within LIMIT bytes."""
    name = url.rsplit("/", 1)[-1]
    what = f"downloading {name}"
    call = urllib.request.Request(url, headers={"User-Agent": "sai-cli"})
    size = 0
    open_url = (
        urllib.request.urlopen
        if origin is None
        else urllib.request.build_opener(AssetRedirect(origin)).open
    )
    try:
        with (
            open_url(call, timeout=left(deadline, what)) as response,
            target.open("wb") as handle,
        ):
            while True:
                left(deadline, what)
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise LookupFailed(f"{name} is larger than expected")
                handle.write(chunk)
    except urllib.error.HTTPError as error:
        raise LookupFailed(f"{what} answered HTTP {error.code}") from error
    except (OSError, ValueError) as error:
        raise LookupFailed(f"{what} failed: {type(error).__name__}") from error


def expected_hash(sums: str, name: str) -> str | None:
    """Return the SHA-256 that SHA256SUMS text lists for file NAME."""
    for line in sums.splitlines():
        digest, _, listed = line.strip().partition(" ")
        if listed.strip().lstrip("*") == name and re.fullmatch(
            r"[0-9a-fA-F]{64}", digest
        ):
            return digest.lower()
    return None


def fetch_wheel(
    tag: str,
    directory: Path,
    web_base: str = WEB_BASE,
    timeout: float = DOWNLOAD_TIMEOUT,
) -> tuple[Path, str]:
    """Download TAG's wheel and SHA256SUMS into DIRECTORY, check the hash,
    and return the wheel and its SHA-256."""
    name = wheel_name(version_of(tag))
    wheel, sums = directory / name, directory / "SHA256SUMS"
    base = download_base(tag, web_base)

    def work(deadline: float) -> None:
        download(f"{base}/SHA256SUMS", sums, MAX_SUMS, deadline)
        download(f"{base}/{name}", wheel, MAX_WHEEL, deadline)

    within(timeout, "downloading the release", work)
    expected = expected_hash(sums.read_text(errors="replace"), name)
    if expected is None:
        raise LookupFailed(f"SHA256SUMS does not list {name}")
    actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if actual != expected:
        raise LookupFailed(f"{name} does not match its SHA256SUMS entry")
    return wheel, actual


def wheel_cache() -> Path:
    """$SAI_HOME/cache/wheels, where `sai update` also keeps its wheels.

    uv's receipt names the wheel file it installed from, and
    `uv tool upgrade --all` reads it again, so the verified wheel is kept
    here instead of in a temporary directory.
    """
    home = os.environ.get("SAI_HOME") or os.path.join(os.path.expanduser("~"), ".sai")
    return Path(os.path.abspath(home)) / "cache" / "wheels"


def private_dir(path: Path, *, preserve_mode: bool = False) -> None:
    """Create a private cache directory or check a parent without changing it."""
    if path.is_symlink():
        raise LookupFailed(f"{path} is a symlink; not keeping a wheel under it")
    try:
        path.mkdir(mode=0o700, parents=True)
        created = True
    except FileExistsError:
        created = False
    status = path.stat()
    if not stat.S_ISDIR(status.st_mode):
        raise NotADirectoryError(f"{path} is not a directory")
    if hasattr(os, "getuid") and status.st_uid != os.getuid():
        raise LookupFailed(f"{path} belongs to another user; not keeping a wheel there")
    if os.name == "nt":
        return
    mode = stat.S_IMODE(status.st_mode)
    if preserve_mode and not created:
        if mode & 0o022:
            raise LookupFailed(
                f"{path} is writable by other users; choose a private SAI_HOME"
            )
        return
    if mode != 0o700:
        try:
            path.chmod(0o700, follow_symlinks=False)
        except OSError as error:
            raise LookupFailed(f"{path} could not be made private") from error


def private_cache() -> Path:
    """Return the private wheel cache beneath user-owned, non-writable parents."""
    cache = wheel_cache()
    private_dir(cache.parent.parent, preserve_mode=True)
    private_dir(cache.parent, preserve_mode=True)
    private_dir(cache)
    return cache


def keep_wheel(tag: str, web_base: str, timeout: float) -> tuple[Path, str]:
    """Download and verify TAG's wheel in a private directory, then move it
    into the wheel cache. Return the kept wheel and its SHA-256."""
    try:
        cache = private_cache()
        with tempfile.TemporaryDirectory(dir=cache, prefix=".download-") as temp:
            wheel, digest = fetch_wheel(tag, Path(temp), web_base, timeout)
            kept = cache / wheel.name
            os.replace(wheel, kept)
    except OSError as error:
        raise LookupFailed(
            f"could not keep the wheel: {type(error).__name__}"
        ) from error
    return kept, digest


def find_uv() -> str | None:
    """The uv that runs this script, or the one on PATH."""
    return os.environ.get("UV") or shutil.which("uv")


def write_record(uv: str, tag: str, digest: str, wheel: Path) -> str | None:
    """Record which release the installed wheel came from, beside the tool's
    environment, so `sai update` knows this is a release install. Return why
    it could not be written, or None."""
    try:
        done = subprocess.run(
            [uv, "tool", "dir"], capture_output=True, text=True, check=True, timeout=60
        )
        tools = Path(done.stdout.strip())
        infos = sorted(
            (tools / "sai-cli").glob("lib/python*/site-packages/sai_cli-*.dist-info")
        )
        infos += sorted(
            (tools / "sai-cli").glob("Lib/site-packages/sai_cli-*.dist-info")
        )
        installed = hashlib.sha256((infos[0] / "RECORD").read_bytes()).hexdigest()
        record = {
            "schema": RECORD_SCHEMA,
            "tag": tag,
            "sha256": digest,
            "installed": installed,
            "wheel": str(wheel),
        }
        fd, temp = tempfile.mkstemp(dir=tools, prefix=".sai-cli-release-")
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle)
        os.replace(temp, tools / RECORD_NAME)
    except (OSError, subprocess.SubprocessError, IndexError) as error:
        return type(error).__name__
    return None


def uv_tool_install(uv: str | None, requirement: str, shown: str, dry_run: bool) -> int:
    """Print and run `uv tool install REQUIREMENT`; return the exit code."""
    print(f"$ {shlex.join(['uv', 'tool', 'install', shown])}", flush=True)
    if dry_run:
        print("Dry run: nothing was installed.")
        return 0
    if uv is None:
        print(
            "error: uv is not on PATH; install it from https://docs.astral.sh/uv/",
            file=sys.stderr,
        )
        return 1
    code = subprocess.run([uv, "tool", "install", requirement], check=False).returncode
    if code != 0:
        print(f"error: uv tool install failed with exit code {code}", file=sys.stderr)
    return code


def native_asset(version: str) -> str:
    system = {"Linux": "unknown-linux-gnu", "Darwin": "apple-darwin"}.get(
        platform.system()
    )
    cpu = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(platform.machine().lower())
    if system is None or cpu is None:
        raise LookupFailed(
            "native Sai supports Linux and macOS on x86-64 and ARM64."
            " Run this installer with --version VERSION using a full Python version"
            " from the release notes."
        )
    return f"sai-{version}-{cpu}-{system}.tar.gz"


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_native(
    tag: str, name: str, stage: Path, web_base: str, timeout: float
) -> str:
    archive, manifest = stage / name, stage / "SHA256SUMS"
    base = download_base(tag, web_base)

    def work(deadline: float) -> None:
        download(f"{base}/SHA256SUMS", manifest, MAX_SUMS, deadline, origin=web_base)
        download(f"{base}/{name}", archive, MAX_WHEEL, deadline, origin=web_base)

    within(timeout, "downloading the native release", work)
    matches = [
        fields[0].lower()
        for line in manifest.read_text(errors="replace").splitlines()
        if len(fields := line.split(maxsplit=1)) == 2 and fields[1].lstrip("*") == name
    ]
    if len(matches) != 1 or re.fullmatch(DIGEST, matches[0]) is None:
        raise LookupFailed(f"SHA256SUMS must contain one checksum for {name}")
    if file_digest(archive) != matches[0]:
        raise LookupFailed(f"{name} does not match its SHA256SUMS entry")
    unpack_native(archive, stage / "sai")
    archive.unlink()
    manifest.unlink()
    return matches[0]


def unpack_native(archive: Path, binary: Path) -> None:
    """Read a single USTAR member and allow only bounded zero padding."""
    block = tarfile.BLOCKSIZE
    try:
        with gzip.open(archive, "rb") as source, binary.open("xb") as output:
            member = tarfile.TarInfo.frombuf(source.read(block), "utf-8", "strict")
            if (
                member.name != "sai"
                or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                or member.linkname
                or not 0 < member.size <= MAX_BINARY
            ):
                raise LookupFailed(
                    "the native archive must contain one regular file named sai"
                )
            remaining = member.size
            while remaining:
                data = source.read(min(remaining, 1 << 16))
                if not data:
                    raise LookupFailed(
                        "the native archive contains a truncated sai binary"
                    )
                output.write(data)
                remaining -= len(data)
            minimum = -member.size % block + 2 * block
            padding = source.read(minimum + 62 * block + 1)
            if (
                not minimum <= len(padding) <= minimum + 62 * block
                or (len(padding) - minimum) % block
                or any(padding)
            ):
                raise LookupFailed(
                    "the native archive contains invalid padding or extra members"
                )
            output.flush()
            os.fchmod(output.fileno(), 0o700)
            os.fsync(output.fileno())
    except (EOFError, OSError, UnicodeError, tarfile.HeaderError, zlib.error) as error:
        raise LookupFailed("could not extract the verified native archive") from error


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError as error:
        if sys.platform != "darwin" or error.errno != errno.EINVAL:
            raise
    finally:
        os.close(fd)


def checked_directory(path: Path, *, private: bool = False) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & (0o077 if private else 0o022)
    ):
        raise LookupFailed(
            f"{path} must be a user-owned directory with safe permissions"
        )


def check_ancestors(path: Path, *, create: bool = False) -> None:
    trusted_owners = {0, os.getuid(), Path(path.anchor).stat().st_uid}
    for parent in reversed(path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            if not create:
                continue
            parent.mkdir(mode=0o700, exist_ok=True)
            info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in trusted_owners
            or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)
        ):
            raise LookupFailed(f"{parent} is not a trusted launcher directory")


@contextmanager
def native_lock(path: Path) -> Generator[None]:
    from fcntl import LOCK_EX, LOCK_NB, flock

    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise LookupFailed(f"{path} must be a private lock file")
        try:
            flock(fd, LOCK_EX | LOCK_NB)
        except BlockingIOError as error:
            raise LookupFailed("another Sai install or update is running") from error
        yield
    finally:
        os.close(fd)


def native_binary(root: Path, directory: str) -> Path:
    if not NATIVE_DIRECTORY.fullmatch(directory):
        raise LookupFailed("the Sai install record names an invalid version directory")
    binary = root / directory / "sai"
    info = binary.lstat()
    parent = binary.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o022
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or not info.st_mode & stat.S_IXUSR
    ):
        raise LookupFailed(f"{binary} must be an owner-controlled executable")
    return binary


def launcher_target(launcher: Path) -> Path | None:
    try:
        info = launcher.lstat()
    except FileNotFoundError:
        return None
    if info.st_uid != os.getuid() or not stat.S_ISLNK(info.st_mode):
        raise LookupFailed(
            f"{launcher} already exists. Update it through its installer"
        )
    return launcher.parent / launcher.readlink()


def uv_launcher(target: Path) -> bool:
    prefix = target.parent.parent
    return (
        target.name == "sai"
        and target.parent.name == "bin"
        and prefix.name == "sai-cli"
        and (prefix / "uv-receipt.toml").is_file()
    )


def check_path(launcher: Path) -> None:
    for directory in os.get_exec_path():
        path = Path(directory or ".").resolve() / "sai"
        if path == launcher:
            found = shutil.which("sai")
            if found is not None and Path(found).parent.resolve() / "sai" != launcher:
                print(
                    f"PATH currently launches {found}. After install it will launch {launcher}.",
                    file=sys.stderr,
                )
            return
        if path.is_file() and os.access(path, os.X_OK):
            raise LookupFailed(f"PATH launches {path}, which shadows {launcher}")
    print(f"Add {launcher.parent} to PATH to run sai.", file=sys.stderr)


def installed_directory(launcher: Path, root: Path) -> str | None:
    target = launcher_target(launcher)
    if target is None:
        return None
    if target.parent.parent != root or target.name != "sai":
        raise LookupFailed(
            f"{launcher} points to {target}. Update it through its installer"
        )
    directory = target.parent.name
    native_binary(root, directory)
    return directory


def launcher_entry(path: Path) -> tuple[int, int, str | None] | None:
    try:
        info = path.lstat()
        link = os.fspath(path.readlink()) if stat.S_ISLNK(info.st_mode) else None
        if os.path.samestat(info, path.lstat()):
            return info.st_dev, info.st_ino, link
    except FileNotFoundError:
        return None
    raise LauncherChanged("the Sai launcher changed while reading its owner")


def rename_launcher(
    source: Path,
    destination: Path,
    *,
    exchange: bool,
    private_destination: bool = False,
) -> None:
    """Exchange two paths, or move into an empty destination, on this filesystem."""
    sys.audit("sai.launcher.rename", os.fspath(source), os.fspath(destination))
    libc = ctypes.CDLL(None, use_errno=True)
    code = errno.ENOSYS
    try:
        if sys.platform == "darwin":
            rename = libc.renamex_np
            args = (os.fsencode(source), os.fsencode(destination), 2 if exchange else 4)
        else:
            rename = libc.renameat2
            cwd = -100  # AT_FDCWD
            args = (
                cwd,
                os.fsencode(source),
                cwd,
                os.fsencode(destination),
                2 if exchange else 1,
            )
    except AttributeError:
        pass
    else:
        code = ctypes.get_errno() if rename(*args) != 0 else 0
    if code:
        if exchange or code not in (errno.ENOSYS, errno.ENOTSUP, errno.EINVAL):
            raise OSError(code, os.strerror(code), os.fspath(destination))
        if private_destination:
            # The installer lock protects this vacant staging path.
            os.rename(source, destination)
        else:
            # Preserve the inode and fail atomically if another owner published.
            # Only the source in our private stage is ever unlinked.
            os.link(source, destination, follow_symlinks=False)
            sync_dir(destination.parent)
            source.unlink()
    sys.audit("sai.launcher.renamed", os.fspath(source), os.fspath(destination))


def protect_displaced_launcher(
    displaced: Path, launcher: Path, published: tuple[int, int, str | None] | None
) -> bool:
    """Return a competing installer's entry to its path, or leave it recoverable."""
    try:
        active = launcher_entry(launcher)
        if active is not None and active == launcher_entry(displaced):
            return True
        if published is not None:
            withdrawn = displaced.with_name("public")
            if launcher_entry(withdrawn) is None and active == published:
                # Capture and check the public entry before putting anything back.
                rename_launcher(
                    launcher, withdrawn, exchange=False, private_destination=True
                )
            captured = launcher_entry(withdrawn)
            if captured != published:
                if captured is not None:
                    rename_launcher(withdrawn, launcher, exchange=False)
                    sync_dir(launcher.parent)
                return False
        rename_launcher(displaced, launcher, exchange=False)
        return True
    except (OSError, LauncherChanged):
        return False


def write_native_pending(root: Path, record: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as handle:
        json.dump(record, handle)
        handle.flush()
        os.fsync(handle.fileno())
    Path(handle.name).replace(root / NATIVE_PENDING)
    sync_dir(root)


def read_native_pending(root: Path) -> dict[str, Any] | None:
    try:
        with (root / NATIVE_PENDING).open("rb") as handle:
            record = json.loads(handle.read(4096))
    except FileNotFoundError:
        return None
    except ValueError as error:
        raise LookupFailed("could not read the pending Sai install record") from error
    if (
        not isinstance(record, dict)
        or not {"old", "new"} <= record.keys() <= {"old", "new", "launcher_switch"}
        or not isinstance(record["new"], str)
        or (record["old"] is not None and not isinstance(record["old"], str))
    ):
        raise LookupFailed("the pending Sai install record is invalid")
    return record


def settle_launcher_switch(
    launcher: Path, root: Path, pending: dict[str, Any] | None
) -> None:
    displaced = root / LAUNCHER_SWITCH
    stage = displaced.parent
    if not os.path.lexists(stage):
        return
    checked_directory(stage, private=True)
    switch = pending.get("launcher_switch") if pending else None
    if switch is None:
        # Older installers recorded the switch beside the staged launcher.
        try:
            with (stage / "owner.json").open("rb") as handle:
                switch = json.loads(handle.read(4096))
            switch["staged"] = LAUNCHER_SWITCH
        except FileNotFoundError:
            # No launcher can have been displaced before the record was written.
            shutil.rmtree(stage)
            sync_dir(root)
            return
        except (TypeError, ValueError) as error:
            raise LauncherChanged(
                f"could not read the launcher switch at {stage}"
            ) from error
    try:
        if switch["staged"] != LAUNCHER_SWITCH:
            raise ValueError("unexpected staging path")
        expected = tuple(switch["expected"])
        published = None if switch["published"] is None else tuple(switch["published"])
    except (ValueError, TypeError, KeyError) as error:
        raise LauncherChanged(
            f"could not read the launcher switch at {stage}"
        ) from error

    if launcher_entry(displaced) in (None, expected, published):
        sync_dir(launcher.parent)
        shutil.rmtree(stage)
        sync_dir(root)
        return
    if protect_displaced_launcher(displaced, launcher, published):
        sync_dir(launcher.parent)
        shutil.rmtree(stage)
        if pending is not None:
            (root / NATIVE_PENDING).unlink()
        sync_dir(root)
        raise LauncherChanged("the Sai launcher changed during the switch")
    raise LauncherChanged(
        f"the Sai launcher changed during the switch. The displaced launcher is at {displaced}"
    )


def switch_launcher(
    launcher: Path, root: Path, directory: str | None, *, previous: str | None
) -> None:
    target = (
        None
        if directory is None
        else native_binary(root, directory).relative_to(launcher.parent)
    )
    if previous is None:
        assert target is not None
        staged = root / LAUNCHER_SWITCH
        staged.parent.mkdir(mode=0o700)
        try:
            staged.symlink_to(target)
            rename_launcher(staged, launcher, exchange=False)
        finally:
            staged.unlink(missing_ok=True)
            staged.parent.rmdir()
            sync_dir(root)
    else:
        expected = launcher_entry(launcher)
        if expected is None or installed_directory(launcher, root) != previous:
            raise LauncherChanged("the Sai launcher changed during the switch")
        displaced = root / LAUNCHER_SWITCH
        stage = displaced.parent
        stage.mkdir(mode=0o700)
        if target is not None:
            displaced.symlink_to(target)
        pending = read_native_pending(root)
        assert pending is not None
        pending["launcher_switch"] = {
            "staged": LAUNCHER_SWITCH,
            "expected": expected,
            "published": launcher_entry(displaced),
        }
        sync_dir(stage)
        write_native_pending(root, pending)
        if target is None:
            rename_launcher(
                launcher, displaced, exchange=False, private_destination=True
            )
        else:
            rename_launcher(displaced, launcher, exchange=True)
        settle_launcher_switch(launcher, root, pending)
    sync_dir(launcher.parent)


def recover_native(launcher: Path, root: Path) -> str | None:
    record = read_native_pending(root)
    settle_launcher_switch(launcher, root, record)
    active = installed_directory(launcher, root)
    if record is None:
        return active
    pending = root / NATIVE_PENDING
    old, new = record["old"], record["new"]
    native_binary(root, new)
    if old is not None:
        native_binary(root, old)
    if active == new and old != new:
        switch_launcher(launcher, root, old, previous=new)
    elif active != old:
        raise LookupFailed("the launcher changed during an interrupted Sai install")
    pending.unlink()
    sync_dir(root)
    return old


def smoke_native(binary: Path, home: Path, version: str) -> None:
    env = {**os.environ, "SAI_HOME": str(home), "SAI_LOG": "false", "CI": "1"}
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        for args in (["--version"], ["config", "get", "update.auto"], ["mcp", "serve"]):
            label = f"sai {' '.join(args)}"
            try:
                with subprocess.Popen(
                    [binary, *args],
                    cwd=home,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=errors,
                ) as process:
                    try:
                        if args[0] == "mcp":
                            assert process.stdin is not None
                            process.stdin.write(MCP_INITIALIZE)
                            process.stdin.flush()
                            read_mcp(output, errors, process)
                        process.communicate(timeout=VERIFY_TIMEOUT)
                    except BaseException:
                        process.kill()
                        raise
                    if process.returncode != 0:
                        errors.seek(0)
                        diagnostic = errors.read(512).decode(errors="replace").strip()
                        raise LookupFailed(
                            f"`{label}` failed: {diagnostic or 'no diagnostic'}"
                        )
                if args[0] == "--version":
                    output.seek(0)
                    if output.read(512).strip() != f"sai {version}".encode():
                        raise LookupFailed(f"`{label}` did not report {version}")
            except subprocess.TimeoutExpired as error:
                raise LookupFailed(f"`{label}` timed out") from error
            finally:
                for stream in (output, errors):
                    stream.seek(0)
                    stream.truncate()


MCP_INITIALIZE = (
    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
    b'{"protocolVersion":"2025-11-25","capabilities":{},'
    b'"clientInfo":{"name":"sai-install","version":"1.0.0"}}}\n'
)


def read_mcp(
    output: BinaryIO, errors: BinaryIO, process: subprocess.Popen[bytes]
) -> None:
    # pread keeps the writer's offset independent of the probe.
    deadline = time.monotonic() + VERIFY_TIMEOUT
    while True:
        first = os.pread(output.fileno(), 1 << 16, 0)
        if b"\n" in first:
            break
        if process.poll() is not None:
            diagnostic = (
                os.pread(errors.fileno(), 512, 0).decode(errors="replace").strip()
            )
            raise LookupFailed(
                f"`sai mcp serve` did not initialize: {diagnostic or 'no diagnostic'}"
            )
        if len(first) >= (1 << 16):
            raise LookupFailed("`sai mcp serve` did not initialize")
        left(deadline, "waiting for sai mcp serve")
        time.sleep(0.02)
    try:
        reply = json.loads(first.split(b"\n", 1)[0])
        result = reply["result"]
        if (
            reply["jsonrpc"] == "2.0"
            and reply["id"] == 1
            and isinstance(result["protocolVersion"], str)
            and result["protocolVersion"]
            and isinstance(result["serverInfo"]["name"], str)
            and result["serverInfo"]["name"]
            and isinstance(result["capabilities"], dict)
        ):
            return
    except (KeyError, TypeError, ValueError):
        pass
    raise LookupFailed("`sai mcp serve` returned an invalid initialization reply")


def native_release(args: argparse.Namespace) -> tuple[str, str, str]:
    tag = (
        TAG_PREFIX + args.version
        if args.version
        else find_latest(args.api_base, args.web_base, args.timeout)
    )
    if tag is None:
        raise LookupFailed("no stable Sai release was found")
    version = version_of(tag)
    return tag, version, native_asset(version)


def repair_mcp(launcher: Path, profile: str) -> None:
    # Doctor reads registrations from HOME and the caller's working directory.
    try:
        result = subprocess.run(
            [
                launcher,
                "--json",
                "doctor",
                "--offline",
                "--mcp-profile=" + os.environ.get("SAI_HOME", ""),
            ],
            env={**os.environ, "SAI_HOME": profile, "SAI_LOG": "false", "CI": "1"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=VERIFY_TIMEOUT,
            check=False,
        )
        report = json.loads(result.stdout)
        if "error" in report:
            report = report["error"]["details"]
        checks = [check for check in report["checks"] if check["id"].startswith("mcp")]
        for check in checks:
            if check["status"] in ("warn", "fail"):
                hint = check.get("fix")
                print(f"{check['id']}: {check['summary']}", file=sys.stderr)
                if hint:
                    print(hint, file=sys.stderr)
        if checks or result.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError):
        pass
    print(
        "Could not finish MCP registration repair. Run `sai doctor`.", file=sys.stderr
    )


def install_native(args: argparse.Namespace) -> int:
    # Validate the platform before looking up a release or creating directories.
    native_asset("")
    for given, official in ((args.api_base, API_BASE), (args.web_base, WEB_BASE)):
        if (
            given != official
            and re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", given) is None
        ):
            raise LookupFailed(
                "the native installer requires official or loopback release URLs"
            )
    home = Path.home().resolve()
    launcher = (home / ".local" / "bin").resolve() / "sai"
    root = launcher.parent / NATIVE_ROOT
    check_ancestors(launcher.parent)
    check_path(launcher)
    target = launcher_target(launcher)
    if target is not None and uv_launcher(target):
        if args.version and parse_tag(TAG_PREFIX + args.version) is None:
            raise LookupFailed(f"{args.version!r} is not a version like X.Y.Z.")
        command = [str(launcher), "update"]
        if args.version:
            command += ["--to", args.version]
        if args.api_base != API_BASE:
            command += ["--api-base", args.api_base]
        if args.web_base != WEB_BASE:
            command += ["--web-base", args.web_base]
        print(f"{launcher} is owned by uv. Running {shlex.join(command)}.", flush=True)
        return 0 if args.dry_run else subprocess.run(command, check=False).returncode
    if target is not None:
        installed_directory(launcher, root)
    if args.dry_run:
        _, _, asset = native_release(args)
        print(f"Would verify and install {asset} at {launcher}.")
        return 0
    # A symlinked bin directory can be on a separate filesystem. Stage beside
    # its canonical launcher so the version and symlink renames stay local.
    check_ancestors(launcher.parent, create=True)
    checked_directory(launcher.parent)
    with native_lock(launcher.parent / NATIVE_LOCK):
        check_path(launcher)
        checked_directory(root, private=True)
        old = recover_native(launcher, root)
        tag, version, asset = native_release(args)
        with (
            tempfile.TemporaryDirectory(dir=root, prefix=".stage-") as scratch,
            tempfile.TemporaryDirectory(prefix="sai-install-profile-") as profile,
        ):
            staging = Path(scratch)
            digest = fetch_native(tag, asset, staging, args.web_base, args.timeout * 4)
            new = f"{version}-{digest}"
            smoke_native(staging / "sai", Path(profile), version)
            published = root / new
            if published.exists() or published.is_symlink():
                existing = native_binary(root, new)
                if file_digest(existing) != file_digest(staging / "sai"):
                    raise LookupFailed("the existing Sai version has different bytes")
            else:
                sync_dir(staging)
                staging.rename(published)
                sync_dir(root)
            pending = root / NATIVE_PENDING
            # The native updater reads this record too. null means the first
            # install had no launcher to restore.
            write_native_pending(root, {"old": old, "new": new})
            try:
                if installed_directory(launcher, root) != old:
                    raise LookupFailed("the Sai launcher changed during the install")
                try:
                    switch_launcher(launcher, root, new, previous=old)
                except FileExistsError as error:
                    if old is None:
                        pending.unlink()
                        sync_dir(root)
                        raise LookupFailed(
                            f"{launcher} was created by another installer"
                        ) from error
                    raise
                smoke_native(launcher, Path(profile), version)
            except LauncherChanged:
                raise
            except (LookupFailed, OSError, KeyboardInterrupt) as error:
                if not pending.exists():
                    raise
                recover_native(launcher, root)
                raise LookupFailed(
                    f"the installed launcher failed verification and was rolled back: {error}"
                ) from error
            pending.unlink()
            sync_dir(root)
            if old is not None:
                repair_mcp(launcher, profile)
    print(f"Installed sai {version} at {launcher}.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--source", action="store_true", help="build from Git instead of the wheel"
    )
    mode.add_argument(
        "--native",
        action="store_true",
        help="install the standalone Linux or macOS binary",
    )
    parser.add_argument("--version", help="install an exact release version")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the command without running it"
    )
    parser.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    parser.add_argument("--web-base", default=WEB_BASE, help=argparse.SUPPRESS)
    parser.add_argument(
        "--timeout", type=float, default=TOTAL_TIMEOUT, help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    if args.version and (
        re.fullmatch(NATIVE_VERSION, args.version) is None
        if args.native
        else parse_tag(TAG_PREFIX + args.version) is None
    ):
        parser.error("--version requires X.Y.Z (or X.Y.Z-rc.N with --native)")
    if args.native:
        try:
            return install_native(args)
        except (LookupFailed, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    git = f"git+{WEB_BASE}/{REPO}"

    def give_up(error: Exception) -> int:
        print(f"error: {error}", file=sys.stderr)
        print(f"To build from source instead: uv tool install {git}", file=sys.stderr)
        return 1

    try:
        tag = (
            TAG_PREFIX + args.version
            if args.version
            else find_latest(args.api_base, args.web_base, args.timeout)
        )
    except LookupFailed as error:
        return give_up(error)
    uv = find_uv()
    if tag is None or args.source:
        if tag is None:
            print("No sai-cli release is published yet; installing from source (main).")
            requirement = git
        else:
            print(f"Building sai-cli {version_of(tag)} from source.")
            # The full ref: a branch with the tag's name must not shadow it.
            requirement = f"{git}@refs/tags/{tag}"
        code = uv_tool_install(uv, requirement, requirement, args.dry_run)
    else:
        print(f"Installing sai-cli {version_of(tag)} from its release wheel.")
        if args.dry_run:
            print(
                f"Would download {wheel_url(tag, args.web_base)}, check it against"
                " the release's SHA256SUMS, and run `uv tool install` on the file."
            )
            print("Dry run: nothing was installed.")
            return 0
        try:
            wheel, digest = keep_wheel(tag, args.web_base, args.timeout * 4)
        except LookupFailed as error:
            return give_up(error)
        print(f"Verified {wheel.name} against the release's SHA256SUMS.")
        # Hash the kept file once more, right before uv opens it.
        try:
            private_cache()
            intact = hashlib.sha256(wheel.read_bytes()).hexdigest() == digest
        except (OSError, LookupFailed):
            intact = False
        if not intact:
            return give_up(LookupFailed(f"{wheel} changed after it was verified"))
        code = uv_tool_install(uv, str(wheel), str(wheel), False)
        if code == 0 and uv is not None:
            problem = write_record(uv, tag, digest, wheel)
            if problem is not None:
                print(
                    "warning: could not record the release this install came"
                    f" from ({problem}); `sai update` will not update it.",
                    file=sys.stderr,
                )
    if code == 0 and not args.dry_run:
        print("Installed. Run `sai --help` to start.")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
