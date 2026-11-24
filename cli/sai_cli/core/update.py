"""Read-only update support: release tags, the latest release, install kind,
and the cached check result.

Nothing here installs anything; `updater` does. Release tags are `sai-cli/vMAJOR.MINOR.PATCH`,
so other components of the repository can release under their own prefixes.
Pre-release tags such as `sai-cli/v0.2.0-rc.1` never count as the latest
version.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from . import paths

REPO = "SaiDotFun/open-sai"
API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"
GIT_URL = f"https://github.com/{REPO}"
TAG_PREFIX = "sai-cli/v"
# Installs the newest release; scripts/install.py in this repository.
INSTALL_COMMAND = (
    f"uv run https://raw.githubusercontent.com/{REPO}/main/scripts/install.py"
)
# The check runs in the background, so the budget is generous; it is still a
# hard wall-clock limit.
CONNECT_TIMEOUT = 10.0
TOTAL_TIMEOUT = 30.0
CACHE_SCHEMA = 1
# GitHub's API limits unauthenticated clients; the web redirect is not metered.
FALLBACK_STATUSES = frozenset({403, 429})
# Other components share the release list, so every page is read. Past
# MAX_PAGES the lookup fails instead of guessing.
PAGE_SIZE = 100
MAX_PAGES = 10

# Canonical components only: packaging turns 0.01.0 into 0.1.0, so a tag with
# a leading zero would name a wheel that does not exist.
_NUMBER = r"(0|[1-9]\d*)"
_VERSION = re.compile(rf"^{_NUMBER}\.{_NUMBER}\.{_NUMBER}$")
_TAG = re.compile(rf"^{re.escape(TAG_PREFIX)}{_NUMBER}\.{_NUMBER}\.{_NUMBER}$")

Version = tuple[int, int, int]


class UpdateError(Exception):
    """The latest release could not be determined."""


class NoRelease(UpdateError):
    """The repository has no sai-cli release yet."""


def parse_tag(tag: str) -> Version | None:
    """Return the version of a sai-cli release tag, or None for anything else,
    including pre-release tags and other components' tags."""
    match = _TAG.match(tag)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def parse_version(text: str) -> Version | None:
    """Return the version of a plain `X.Y.Z` package version, or None."""
    match = _VERSION.match(text)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def tag_for(version: str) -> str:
    """The release tag of package version VERSION."""
    return f"{TAG_PREFIX}{version}"


def tag_version(tag: str) -> str:
    """The package version a release tag names: `sai-cli/v0.2.0` is `0.2.0`."""
    return tag.removeprefix(TAG_PREFIX)


def is_newer(latest: str, installed: str) -> bool:
    """True when release tag LATEST is newer than package version INSTALLED.

    An unparseable side never counts as newer, so a local or pre-release
    build is never told to move.
    """
    new, old = parse_tag(latest), parse_version(installed)
    return new is not None and old is not None and new > old


# Releases


@dataclass(frozen=True)
class Release:
    """A published sai-cli release and where its files download from."""

    tag: str
    wheel_url: str
    sums_url: str

    @property
    def version(self) -> str:
        return tag_version(self.tag)


def wheel_name(version: str) -> str:
    """The wheel file a release of VERSION attaches. sai-cli is pure Python,
    so every platform shares one wheel."""
    return f"sai_cli-{version}-py3-none-any.whl"


_WHEEL_NAME = re.compile(
    rf"^sai_cli-{_NUMBER}\.{_NUMBER}\.{_NUMBER}-py3-none-any\.whl$"
)


def is_wheel_name(name: str) -> bool:
    """True for exactly the file name of a sai-cli release wheel."""
    return _WHEEL_NAME.match(name) is not None


def release_for(tag: str, web_base: str = WEB_BASE, repo: str = REPO) -> Release:
    """Build the release of TAG with its download URLs.

    The URLs come from the tag alone, never from a URL an API answer names.
    """
    base = f"{web_base.rstrip('/')}/{repo}/releases/download/{quote(tag)}"
    return Release(tag, f"{base}/{wheel_name(tag_version(tag))}", f"{base}/SHA256SUMS")


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sai-cli",
    }


# A page of a hundred releases with long notes can reach a megabyte or two;
# refuse anything far larger.
MAX_BODY = 16 << 20
TIMED_OUT = "timed out checking the latest release"
NO_RELEASE = "no sai-cli release is published yet"


def _timeout(deadline: float, connect: float) -> httpx.Timeout:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UpdateError(TIMED_OUT)
    return httpx.Timeout(remaining, connect=min(connect, remaining))


def _read_body(response: httpx.Response, deadline: float) -> bytes:
    """Read the body, giving up at DEADLINE even if bytes keep arriving."""
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if time.monotonic() > deadline:
            raise UpdateError(TIMED_OUT)
        if len(body) > MAX_BODY:
            raise UpdateError("GitHub API answer is too large")
    return bytes(body)


def latest_tag(releases: list[Any]) -> str | None:
    """Return the highest sai-cli release tag in RELEASES, or None.

    Drafts, pre-releases, and tags of other components do not count.
    """
    best: tuple[Version, str] | None = None
    for item in releases:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        tag = item.get("tag_name")
        version = parse_tag(tag) if isinstance(tag, str) else None
        if version is not None and (best is None or version > best[0]):
            best = (version, tag)
    return best[1] if best else None


def _has_next(response: httpx.Response) -> bool:
    """True when the Link header names a next page."""
    return 'rel="next"' in response.headers.get("link", "")


def _page(
    client: httpx.Client, url: str, page: int, deadline: float, connect: float
) -> tuple[list[Any], bool] | None:
    """Return one page of releases and whether another follows, or None when
    the API is rate limited or failing."""
    params = {"per_page": PAGE_SIZE, "page": page}
    timeout = _timeout(deadline, connect)
    with client.stream("GET", url, params=params, timeout=timeout) as response:
        status = response.status_code
        if status in FALLBACK_STATUSES or status >= 500:
            return None
        if status != 200:
            raise UpdateError(f"GitHub API answered HTTP {status}")
        more = _has_next(response)
        body = _read_body(response, deadline)
    try:
        releases = json.loads(body)
    except ValueError as exc:
        raise UpdateError("GitHub API answered with malformed JSON") from exc
    if not isinstance(releases, list):
        raise UpdateError("GitHub API answered with malformed JSON")
    return releases, more


def _from_api(
    client: httpx.Client, url: str, deadline: float, connect: float
) -> str | None:
    """Return the highest sai-cli tag across every page of the release list,
    or None when the API is rate limited or failing.

    The list is ordered by date, not version, so a maintenance release can
    sit on an earlier page than a higher version; no page is skipped. Only
    the last page proves that sai-cli has no release.
    """
    tags: list[Any] = []
    for page in range(1, MAX_PAGES + 1):
        answer = _page(client, url, page, deadline, connect)
        if answer is None:
            return None
        releases, more = answer
        tags.extend(releases)
        if not more:
            tag = latest_tag(tags)
            if tag is None:
                raise NoRelease(NO_RELEASE)
            return tag
    raise UpdateError(
        f"the release list is longer than {MAX_PAGES} pages; the lookup is incomplete"
    )


RATE_LIMITED = (
    "GitHub's API is rate limited or failing, so the latest sai-cli release"
    " cannot be determined; try again later"
)


def _is_release_list(location: str, latest_url: str) -> bool:
    """True when LOCATION is exactly the repository's release list, the
    page LATEST_URL redirects to when no release exists. A tag whose name
    ends in `/releases`, such as `docs/releases`, is not the list."""
    expected = urlsplit(latest_url).path.removesuffix("/latest")
    return urlsplit(location).path.rstrip("/").lower() == expected.lower()


def _from_redirect(
    client: httpx.Client, url: str, deadline: float, connect: float
) -> str:
    """Learn from the web /releases/latest redirect whether any release
    exists. It never returns a tag.

    That page follows GitHub's single latest release for the whole
    repository, which can be another component's or an older sai-cli
    maintenance release, so its tag is not an answer. A redirect to the
    release list proves there is no release at all.
    """
    response = client.head(url, timeout=_timeout(deadline, connect))
    location = response.headers.get("location")
    if not response.is_redirect or not location:
        raise UpdateError(
            f"release page answered HTTP {response.status_code} without a redirect"
        )
    if _is_release_list(location, url):
        raise NoRelease(NO_RELEASE)
    raise UpdateError(RATE_LIMITED)


def _fetch(api: str, web: str, deadline: float, connect: float) -> str:
    try:
        with httpx.Client(headers=_headers(), follow_redirects=False) as client:
            tag = _from_api(client, api, deadline, connect)
            if tag is not None:
                return tag
            return _from_redirect(client, web, deadline, connect)
    except httpx.TimeoutException as exc:
        raise UpdateError(TIMED_OUT) from exc
    except httpx.HTTPError as exc:
        raise UpdateError(f"could not reach GitHub: {type(exc).__name__}") from exc


def fetch_latest(
    api_base: str = API_BASE,
    web_base: str = WEB_BASE,
    repo: str = REPO,
    connect_timeout: float = CONNECT_TIMEOUT,
    total_timeout: float = TOTAL_TIMEOUT,
) -> Release:
    """Return the latest sai-cli release of REPO.

    Lists every release through the API and takes the highest `sai-cli/v*`
    tag that is not a pre-release. On 403, 429, or a 5xx it asks the web
    release page instead, which can only say that no release exists; with
    releases present and no API, the lookup fails. Raises NoRelease when
    sai-cli has no release yet.

    All requests share one TOTAL_TIMEOUT wall-clock budget. HTTPX timeouts
    apply per network operation, so a server that sends a byte at a time
    could outlast them; the lookup runs in a daemon thread that the caller
    stops waiting for at the deadline.
    """
    deadline = time.monotonic() + total_timeout
    api = f"{api_base.rstrip('/')}/repos/{repo}/releases"
    web = f"{web_base.rstrip('/')}/{repo}/releases/latest"
    outcome: dict[str, str | Exception] = {}

    def work() -> None:
        try:
            outcome["tag"] = _fetch(api, web, deadline, connect_timeout)
        except Exception as exc:  # noqa: BLE001 - re-raised in the caller
            outcome["error"] = exc

    worker = threading.Thread(target=work, name="sai-update-check", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        raise UpdateError(TIMED_OUT)
    error = outcome.get("error")
    if isinstance(error, Exception):
        raise error
    tag = outcome.get("tag")
    assert isinstance(tag, str)
    return release_for(tag, web_base, repo)


# Install kind

CHECKOUT = "checkout"
UV_TOOL_RELEASE = "uv-tool-release"
UV_TOOL_GIT = "uv-tool-git"
UV_TOOL_OTHER = "uv-tool-other"
UVX = "uvx"
OTHER = "other"


@dataclass(frozen=True)
class Install:
    """How this copy of sai-cli was installed.

    `sai update` only changes an official install: a `uv tool install` of a
    release wheel (UV_TOOL_RELEASE) or of this repository's Git source
    (UV_TOOL_GIT).
    """

    kind: str
    prefix: str
    url: str | None = None
    commit: str | None = None
    requested: str | None = None

    @property
    def official(self) -> bool:
        return self.kind in (UV_TOOL_RELEASE, UV_TOOL_GIT)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _dist_info(prefix: Path) -> Path | None:
    """The sai-cli dist-info directory of the environment at PREFIX."""
    patterns = (
        "lib/python*/site-packages/sai_cli-*.dist-info",
        "Lib/site-packages/sai_cli-*.dist-info",
    )
    for pattern in patterns:
        for path in sorted(prefix.glob(pattern)):
            return path
    return None


def _direct_url(prefix: Path) -> dict[str, Any] | None:
    """Read direct_url.json of the sai-cli dist-info under PREFIX."""
    info = _dist_info(prefix)
    if info is None:
        return None
    try:
        data = json.loads((info / "direct_url.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def installed_version(prefix: Path | None = None) -> str | None:
    """The sai-cli version installed at PREFIX (default: sys.prefix), read
    from disk so it is current after a reinstall."""
    info = _dist_info(Path(sys.prefix if prefix is None else prefix))
    if info is None:
        return None
    try:
        lines = (info / "METADATA").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.strip():
            break
        name, _, value = line.partition(":")
        if name.lower() == "version":
            return value.strip()
    return None


# Release record
#
# A release wheel is hashed against the release's SHA256SUMS and then
# installed from the local file, so uv records a `file://` origin. The
# installer therefore writes a record next to the tool's environment, in uv's
# tool directory: which release the wheel came from, its SHA-256, and the
# hash of the RECORD file uv wrote for the install. An install counts as a
# release install only while that record still describes what is installed.

RECORD_NAME = ".sai-cli-release.json"
RECORD_SCHEMA = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def release_record_path(prefix: Path) -> Path:
    """The release record of the uv tool environment at PREFIX. It sits
    beside the environment, so every SAI_HOME sharing the tool reads the
    same record and uv recreating the environment does not delete it."""
    return prefix.parent / RECORD_NAME


def installed_files_hash(prefix: Path) -> str | None:
    """SHA-256 of the RECORD file of the sai-cli install at PREFIX, which
    lists every installed file with its hash."""
    info = _dist_info(prefix)
    if info is None:
        return None
    try:
        return hashlib.sha256((info / "RECORD").read_bytes()).hexdigest()
    except OSError:
        return None


@dataclass(frozen=True)
class ReleaseRecord:
    """Which release wheel an install came from."""

    tag: str
    sha256: str
    # SHA-256 of the install's RECORD file when the record was written.
    installed: str
    # The verified wheel, when the installer kept it.
    wheel: str | None = None

    @property
    def version(self) -> str:
        return tag_version(self.tag)

    def to_json(self) -> dict[str, Any]:
        return {"schema": RECORD_SCHEMA, **asdict(self)}


def _record_from(data: Any) -> ReleaseRecord | None:
    if not isinstance(data, dict) or data.get("schema") != RECORD_SCHEMA:
        return None
    tag, digest = data.get("tag"), data.get("sha256")
    installed, wheel = data.get("installed"), data.get("wheel")
    if not isinstance(tag, str) or parse_tag(tag) is None:
        return None
    for value in (digest, installed):
        if not isinstance(value, str) or _SHA256.match(value) is None:
            return None
    if wheel is not None and not isinstance(wheel, str):
        return None
    return ReleaseRecord(tag, str(digest), str(installed), wheel)


def read_release_record(prefix: Path) -> ReleaseRecord | None:
    """Return the release record beside PREFIX when it describes the install
    at PREFIX: the same version and the same installed files. A record left
    from another install does not count."""
    try:
        data = json.loads(release_record_path(prefix).read_text())
    except (OSError, ValueError):
        return None
    record = _record_from(data)
    if record is None or record.version != installed_version(prefix):
        return None
    if record.installed != installed_files_hash(prefix):
        return None
    return record


def _is_recorded_wheel(prefix: Path, url: str) -> bool:
    """True for an install from a local wheel file that the release record
    beside PREFIX vouches for."""
    parts = urlsplit(url)
    if parts.scheme != "file":
        return False
    record = read_release_record(prefix)
    if record is None:
        return False
    return Path(unquote(parts.path)).name == wheel_name(record.version)


def _is_this_repo(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path.rstrip("/").removesuffix(".git").lower()
    return parts.hostname == "github.com" and path == f"/{REPO.lower()}"


def _is_release_wheel(url: str, web_base: str = WEB_BASE) -> bool:
    """True for a wheel under this repository's sai-cli release downloads."""
    parts, base = urlsplit(url), urlsplit(web_base)
    prefix = f"{base.path.rstrip('/')}/{REPO}/releases/download/{TAG_PREFIX}"
    return (
        (parts.scheme, parts.netloc) == (base.scheme, base.netloc)
        and unquote(parts.path).lower().startswith(prefix.lower())
        and parts.path.endswith(".whl")
    )


def _public(url: str | None) -> str | None:
    """Drop user info, query, and fragment, which can hold credentials."""
    if not url:
        return None
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path}" if parts.scheme else url


def install_kind(prefix: Path | None = None, web_base: str = WEB_BASE) -> Install:
    """Classify the environment at PREFIX (default: sys.prefix).

    - checkout: an editable install, as `uv sync` makes in a clone.
    - uv-tool-release: `uv tool install` of a sai-cli release wheel: by its
      GitHub URL, or from a local file the release record vouches for.
    - uv-tool-git: `uv tool install` from this repository's Git source.
    - uv-tool-other: `uv tool install` from a directory, fork, or index.
    - uvx: a cached `uvx` environment.
    - other: anything else, such as pip.
    """
    root = Path(sys.prefix if prefix is None else prefix)
    direct = _direct_url(root) or {}
    url = direct.get("url") if isinstance(direct.get("url"), str) else None
    vcs = direct.get("vcs_info") if isinstance(direct.get("vcs_info"), dict) else {}
    directory = direct.get("dir_info")
    commit = vcs.get("commit_id") if vcs else None
    requested = vcs.get("requested_revision") if vcs else None

    def made(kind: str) -> Install:
        return Install(kind, str(root), _public(url), commit, requested)

    if isinstance(directory, dict) and directory.get("editable"):
        return made(CHECKOUT)
    if (root / "uv-receipt.toml").is_file():
        from_release = bool(url) and (
            _is_release_wheel(url, web_base) or _is_recorded_wheel(root, url)
        )
        if not vcs and from_release:
            return made(UV_TOOL_RELEASE)
        is_git = vcs.get("vcs") == "git" if vcs else False
        if is_git and url and _is_this_repo(url.removeprefix("git+")):
            return made(UV_TOOL_GIT)
        return made(UV_TOOL_OTHER)
    if root.parent.name == "archive-v0":
        return made(UVX)
    return made(OTHER)


@dataclass(frozen=True)
class Guidance:
    """How to move an install to a newer release."""

    note: str
    command: str | None = None


def guidance(install: Install) -> Guidance:
    """Say how to move INSTALL to the latest release.

    Only an official uv tool install is supported. A checkout updates with
    git; anything else is pointed at the supported install.
    """
    if install.kind == CHECKOUT:
        return Guidance("This is a source checkout; pull, then run `uv sync`.")
    if install.official:
        return Guidance("Run `sai update` to install it.")
    return Guidance(
        f"Only uv tool installs from {REPO} are supported. To switch to one, run:",
        INSTALL_COMMAND,
    )


# Cache


def sai_home() -> Path:
    """$SAI_HOME or ~/.sai, as an absolute path, so a relative SAI_HOME
    means the same directory to every process this one starts."""
    return Path(os.path.abspath(paths.sai_home()))


def wheel_cache() -> Path:
    """Where the install script and `sai update` keep the verified wheel uv
    installed from. uv's receipt names that file, so it has to stay."""
    return sai_home() / "cache" / "wheels"


def cache_path() -> Path:
    return sai_home() / "update.json"


@dataclass(frozen=True)
class Cache:
    """The last successful check: the latest tag and when it was seen."""

    latest: str
    checked_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": CACHE_SCHEMA,
            "latest": self.latest,
            "checkedAt": self.checked_at,
        }


def read_cache(path: Path | None = None) -> Cache | None:
    """Return the cached check, or None when it is missing or unreadable.

    A corrupt file or another schema counts as missing.
    """
    try:
        data = json.loads((path or cache_path()).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA:
        return None
    latest, checked_at = data.get("latest"), data.get("checkedAt")
    if not isinstance(latest, str) or parse_tag(latest) is None:
        return None
    if not isinstance(checked_at, str):
        return None
    return Cache(latest, checked_at)


def prepare_parent(target: Path) -> None:
    """Create the parent for profile state or for uv tool metadata."""
    if target.parent.absolute() == sai_home():
        paths.private_directory(target.parent)
    else:
        target.parent.mkdir(mode=paths.PRIVATE_MODE, parents=True, exist_ok=True)


def write_json(target: Path, value: dict[str, Any]) -> None:
    """Replace the JSON file at TARGET atomically."""
    prepare_parent(target)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".update-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def write_cache(cache: Cache | None, path: Path | None = None) -> None:
    """Replace the cache file. None records a check that found no release,
    so a withdrawn release stops being reported as the latest."""
    if cache is None:
        value = {"schema": CACHE_SCHEMA, "latest": None, "checkedAt": now()}
    else:
        value = cache.to_json()
    write_json(path or cache_path(), value)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def latest_version(path: Path | None = None) -> str | None:
    """The cached latest version as `X.Y.Z`, read with no network."""
    cache = read_cache(path)
    return tag_version(cache.latest) if cache else None
