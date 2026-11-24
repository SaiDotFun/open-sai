# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Install the newest sai-cli release with uv.

Finds the highest `sai-cli/vX.Y.Z` release of SaiDotFun/open-sai, downloads
its wheel and SHA256SUMS, checks the hash, and runs `uv tool install` on the
downloaded file. `--source` builds that release's tag from Git instead.
Before the first release exists, it installs from `main` and says so.

What it checks: the wheel it downloads must match the release's SHA256SUMS,
and the cache it keeps the wheel in must be private to you. It does not defend
against other local users who can write to the directories above SAI_HOME;
they can already replace your tools.

It needs only uv and the standard library, so it runs straight from GitHub:

    uv run https://raw.githubusercontent.com/SaiDotFun/open-sai/main/scripts/install.py

Usage: install.py [--source] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote, urlsplit

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

NUMBER = r"(0|[1-9]\d*)"
TAG = re.compile(rf"^{re.escape(TAG_PREFIX)}{NUMBER}\.{NUMBER}\.{NUMBER}$")

T = TypeVar("T")


class LookupFailed(Exception):
    """The latest release could not be determined or downloaded."""


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


def within(seconds: float, what: str, work: Callable[[float], T]) -> T:
    """Run WORK(deadline) and give up after SECONDS of wall-clock time.

    Socket timeouts only bound each read, so a server that keeps sending a
    little could hold a request open forever. WORK runs in a daemon thread
    that this stops waiting for at the deadline.
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
    worker.join(seconds)
    if worker.is_alive():
        raise LookupFailed(f"timed out {what}")
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
    api_base: str = API_BASE, web_base: str = WEB_BASE, timeout: float = TOTAL_TIMEOUT
) -> str | None:
    """Return the latest sai-cli release tag, or None when there is none yet."""

    def work(deadline: float) -> str | None:
        answered, tag = from_api(api_base, deadline)
        if answered:
            return tag
        if any_release(web_base, deadline):
            raise LookupFailed(RATE_LIMITED)
        return None

    return within(timeout, "looking up the latest release", work)


def version_of(tag: str) -> str:
    return tag[len(TAG_PREFIX) :]


def wheel_name(version: str) -> str:
    return f"sai_cli-{version}-py3-none-any.whl"


def download_base(tag: str, web_base: str = WEB_BASE) -> str:
    return f"{web_base.rstrip('/')}/{REPO}/releases/download/{quote(tag)}"


def wheel_url(tag: str, web_base: str = WEB_BASE) -> str:
    return f"{download_base(tag, web_base)}/{wheel_name(version_of(tag))}"


def download(url: str, target: Path, limit: int, deadline: float) -> None:
    """Save URL to TARGET, following redirects, within LIMIT bytes."""
    name = url.rsplit("/", 1)[-1]
    what = f"downloading {name}"
    call = urllib.request.Request(url, headers={"User-Agent": "sai-cli"})
    size = 0
    try:
        with (
            urllib.request.urlopen(call, timeout=left(deadline, what)) as response,
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


def home_mode_repairable(path: Path, info: os.stat_result) -> bool:
    # Keep in step with sai_cli.core.paths. The standalone installer runs
    # before it can import the CLI package.
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & (stat.S_ISGID | stat.S_ISVTX | 0o022)
    ):
        return False
    if Path(os.path.abspath(path)) == Path.home() / ".sai":
        return True
    managed = {
        "trader",
        "logs",
        "config.toml",
        ".config.lock",
        "cache",
        "update.json",
        "update.lock",
        "update-cache.lock",
    }
    trader = path / "trader"
    try:
        if any(entry.name not in managed for entry in path.iterdir()):
            return False
        trader_info = trader.lstat()
    except OSError:
        return False
    if (
        not stat.S_ISDIR(trader_info.st_mode)
        or trader_info.st_uid != info.st_uid
        or trader_info.st_mode & 0o022
    ):
        return False
    for name in ("sessions.json", "session.json", "intents.sqlite3"):
        try:
            marker = (trader / name).lstat()
        except OSError:
            continue
        if stat.S_ISREG(marker.st_mode) and marker.st_uid == info.st_uid:
            return True
    return False


def private_dir(path: Path, *, profile_root: bool = False) -> None:
    """Create a private cache path and refuse a writable shared Sai home."""
    if path.is_symlink():
        raise LookupFailed(f"{path} is a symlink; not keeping a wheel under it")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = path.stat()
    if hasattr(os, "getuid") and status.st_uid != os.getuid():
        raise LookupFailed(f"{path} belongs to another user; not keeping a wheel there")
    if os.name == "nt":
        return
    mode = stat.S_IMODE(status.st_mode)
    target = 0o700
    if profile_root:
        if mode & 0o022:
            raise LookupFailed(
                f"{path} is writable by other users; choose a private SAI_HOME"
            )
        if not mode & 0o077 or not home_mode_repairable(path, status):
            return
        target = mode & ~0o077
    if mode != target:
        try:
            path.chmod(target, follow_symlinks=False)
        except OSError as error:
            raise LookupFailed(f"{path} could not be made private") from error


def private_cache() -> Path:
    """Return the wheel cache after making $SAI_HOME, `cache`, and
    `cache/wheels` private to the current user."""
    cache = wheel_cache()
    private_dir(cache.parent.parent, profile_root=True)
    private_dir(cache.parent)
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--source", action="store_true", help="build from Git instead of the wheel"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the command without running it"
    )
    parser.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    parser.add_argument("--web-base", default=WEB_BASE, help=argparse.SUPPRESS)
    parser.add_argument(
        "--timeout", type=float, default=TOTAL_TIMEOUT, help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    git = f"git+{WEB_BASE}/{REPO}"

    def give_up(error: Exception) -> int:
        print(f"error: {error}", file=sys.stderr)
        print(f"To build from source instead: uv tool install {git}", file=sys.stderr)
        return 1

    try:
        tag = find_latest(args.api_base, args.web_base, args.timeout)
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
