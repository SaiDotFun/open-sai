"""Structured logs: one JSON object per line under $SAI_HOME/logs.

Each surface writes its own file per UTC day, `<surface>-YYYY-MM-DD.jsonl`,
where the surface is `cli`, `mcp`, or `update`. The directory has mode 0700
and the files 0600.

The event registry in `core.log_events` defines the common fields and the
fields for each event. Fields bound with `bind` accompany the event.

`event` does no I/O and never blocks: it stamps the line, copies its fields
within a fixed budget, and puts it on a bounded queue. One daemon thread
drains the queue and does everything that can wait on a disk: reading the configuration, scrubbing, opening, writing,
and deleting old files. The queue is bounded by count and by size, and each
event's fields are cut to a fixed budget as they are copied. When the queue
is full the event is dropped and counted, and a later `log.dropped` line
reports the count. The process waits a fraction of a second at exit for the
queue to drain and drops what is left. So a slow or stuck filesystem
can cost log lines, never an MCP reply or the time between reserving an
intent and submitting it.

Callers pass only values that are safe to store: never the session token, a
signature, or clipboard text. Every string is still treated as untrusted.
`event` removes each session token this process has already seen as it
copies the fields, so a later sign-out here cannot leave a queued token
unredacted. The writer rereads the session store, removes its tokens again,
reduces URLs to their origin, and removes anything shaped like a JWT and
long hex strings such as signatures.

Known limit: a token that only another process ever stored, and removed
again before this process's writer reached the queued line, is caught by
its shape alone. Sai session tokens are JWTs, which that always removes; an
opaque token in that window would be written.
Text from outside sai, wrapped in `untrusted`, is cut shorter and loses hex
strings of 64 digits or more.

The writer opens the logs directory without following a symlink, checks that
this user owns it, and opens, checks, and deletes files relative to that
descriptor. It appends only to a regular file with one link that this user
owns, after removing any group and other access from it. Anything else is left alone and the line is
lost.

Logging never writes to stdout or stderr and never raises. Commands that
promise to change nothing, such as `sai doctor --no-fix`, run inside
`silenced` and write no lines.

`logs.enabled` (SAI_LOG) turns logging off; `logs.keep_days`
(SAI_LOG_KEEP_DAYS) sets how many days of files are kept. A logs setting or
config file that cannot be read also turns logging off, since it cannot say
what the person chose. Old files are deleted once per UTC day.
"""

from __future__ import annotations

import atexit
import hashlib
import hmac
import itertools
import json
import math
import os
import queue
import re
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import ExitStack, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from importlib import metadata
from pathlib import Path
from types import MappingProxyType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from . import paths
from .errors import Code
from .log_events import COMMON_REQUIRED, EVENTS, SCHEMA_VERSION, valid_event

DIR_NAME = "logs"
SURFACES = ("cli", "mcp", "update")
LEVELS = ("info", "warn", "error")
FILE_MODE = 0o600
DEFAULT_KEEP_DAYS = 14
PRUNE_MARKER = ".pruned"
ENDPOINT_KEY = ".endpoint-key"
ENDPOINT_KEY_LOCK = ".endpoint-key.lock"
# Longest string kept in a field, longest text from outside sai, and
# longest line written.
MAX_TEXT = 2000
MAX_UNTRUSTED = 300
MAX_LINE = 16 * 1024
# What `event` copies from one call: characters of text in all, items of
# any one list or mapping, and nesting depth. The rest is cut.
EVENT_CHARS = 12 * 1024
# Values copied from one call, counting every number, flag, string, list,
# and mapping, and what each costs against EVENT_CHARS besides its text.
EVENT_NODES = 512
NODE_CHARS = 16
MAX_ITEMS = 64
MAX_DEPTH = 6
CUT = "[cut]"
# Events waiting for the writer, by count and by characters of text. Past
# either, events are dropped and counted.
QUEUE_SIZE = 2048
QUEUE_CHARS = 4 * 1024 * 1024
# How long the process waits at exit for the queue to drain.
EXIT_FLUSH_SECONDS = 0.3
# Extra lines `tail` reads from each file, so an event that was appended
# after newer ones still sorts into place.
TAIL_SLACK = 256
# How long `event` skips a home whose logging is off or failing before it
# lets the writer look again.
RECHECK_SECONDS = 5.0
TAIL_CHUNK = 64 * 1024
REDACTED = "[redacted session token]"
REDACTED_HEX = "[redacted hex]"
REDACTED_URL = "[redacted url]"
COMMON = tuple(COMMON_REQUIRED)
_GRAPHQL_ERROR_CODES = frozenset(
    {
        "GRAPHQL_PARSE_FAILED",
        "GRAPHQL_VALIDATION_FAILED",
        "INTENT_NOT_ACCEPTED",
        "BAD_USER_INPUT",
        "FORBIDDEN",
        "INTERNAL_SERVER_ERROR",
        "UNAUTHENTICATED",
    }
)

RUN_ID = uuid.uuid4().hex
_seq = itertools.count(1)
_FILE = re.compile(
    rf"(?P<surface>{'|'.join(SURFACES)})-(?P<date>\d{{4}}-\d{{2}}-\d{{2}})\.jsonl"
)
# Three base64url segments, the first a JSON object: a JWT such as the
# session token.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
# A URL in running text: from its scheme to the next whitespace. Quotes,
# brackets, commas, and semicolons are all legal inside a URL, so nothing
# else ends it; whatever follows without a space is removed with it.
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
# The same, to the end of the string: for a value that is one token.
_URL_REST = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://.*", re.DOTALL)
_HOST = re.compile(r"[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\]")
# A 65-byte signature is 130 hex digits. A transaction hash or a preview
# digest is 64, and stays unless the text is `untrusted`.
_LONG_HEX = re.compile(r"(?:0x)?[0-9A-Fa-f]{100,}")
_HEX = re.compile(r"(?:0x)?[0-9A-Fa-f]{64,}")
_TRUE = ("true", "1", "yes", "on")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# Open files relative to a directory descriptor where the platform can.
_DIR_FD = hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd

_context: ContextVar[Mapping[str, Any]] = ContextVar(
    "sai_log_context", default=MappingProxyType({})
)
# How many `silenced` blocks are open, in any thread.
_silenced = 0


def home(env: Mapping[str, str] | None = None) -> Path:
    return paths.sai_home(env)


def directory(env: Mapping[str, str] | None = None) -> Path:
    """Where the log files live. Reading this creates nothing."""
    return home(env) / DIR_NAME


def _version() -> str:
    try:
        return metadata.version("sai-cli")
    except metadata.PackageNotFoundError:
        return "unknown"


VERSION = _version()


def now() -> datetime:
    return datetime.now(UTC)


def stamp(instant: datetime) -> str:
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Configuration


def _stat_key(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_ino, info.st_mtime_ns, info.st_size)


def _off(raw: str | None) -> bool:
    """True when the SAI_LOG value RAW turns logging off, or is not a
    boolean at all. Reads nothing."""
    text = (raw or "").strip().lower()
    return bool(text) and text not in _TRUE


_ConfigStat = tuple[int, int, int, int, int]
_config_cache: dict[tuple[Any, ...], tuple[float, tuple[bool, int]]] = {}
_config_generation = object()


def _config_stat(path: Path) -> _ConfigStat | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)


def invalidate_config() -> None:
    """Refresh the writer after a successful config edit."""
    global _config_generation
    # An in-flight read or write can still finish under its previous generation.
    _config_generation = object()
    _config_cache.clear()
    _skip.clear()


def resolve_config(
    root: Path, raw_enabled: str | None, raw_keep: str | None
) -> tuple[bool, int]:
    """Return (`logs.enabled`, `logs.keep_days`) for the home ROOT from the
    two environment values and its config file.

    Each key resolves on its own, environment first. With logging turned
    off, `logs.keep_days` is not validated. Otherwise any logs value that is
    invalid, or a config file that cannot be read or parsed, turns logging
    off. The answer is cached by file metadata and environment values.
    """
    value, _ = _resolve_config(root, raw_enabled, raw_keep)
    return value


def _resolve_config(
    root: Path, raw_enabled: str | None, raw_keep: str | None
) -> tuple[tuple[bool, int], _ConfigStat | None]:
    """Return the settings and the file metadata they were checked against."""
    from . import settings

    generation = _config_generation
    path = root / settings.FILE_NAME
    try:
        signature = _config_stat(path)
    except OSError:
        return (False, DEFAULT_KEEP_DAYS), None
    key = (str(path), signature, raw_enabled, raw_keep, generation)
    checked_at = time.monotonic()
    cached = _config_cache.get(key)
    # Expire unchanged entries too, for filesystems with coarse timestamps
    # and Windows versions that report creation time as ctime.
    if cached is not None and checked_at - cached[0] < RECHECK_SECONDS:
        return cached[1], signature

    def pick(name: str, raw: str | None, file: Mapping[str, Any]) -> Any:
        if raw is not None and raw.strip():
            return settings.parse_value(name, raw)
        value = file.get(name, settings.BY_KEY[name].default)
        if isinstance(value, settings.Invalid):
            raise settings.SettingsError(value.message)
        return value

    value = (False, DEFAULT_KEEP_DAYS)
    try:
        file, _warnings = settings.read_file(path)
        if pick("logs.enabled", raw_enabled, file) is True:
            # Only now does the other value matter.
            value = (True, int(pick("logs.keep_days", raw_keep, file)))
    except Exception:  # noqa: BLE001 - unreadable settings mean no logging
        value = (False, DEFAULT_KEEP_DAYS)
    if len(_config_cache) > 64:
        _config_cache.clear()
    _config_cache[key] = (checked_at, value)
    return value, signature


def config() -> tuple[bool, int]:
    """Return (`logs.enabled`, `logs.keep_days`) for this process's
    environment and config file."""
    return resolve_config(
        home(), os.environ.get("SAI_LOG"), os.environ.get("SAI_LOG_KEEP_DAYS")
    )


def enabled() -> bool:
    return config()[0]


# Redaction


class Untrusted(str):
    """Text from outside sai, such as an error message Keeper returned."""

    __slots__ = ()


class Endpoint(str):
    """A whole endpoint URL, logged as its origin only."""

    __slots__ = ()


def untrusted(value: Any) -> Untrusted | None:
    """Mark VALUE as text from outside sai, or text that can quote input:
    it is cut to MAX_UNTRUSTED characters, loses hex strings of 64 digits
    or more, and ends at the first URL, which is cut to its origin."""
    return None if value is None else Untrusted(value)


def endpoint(value: Any) -> Endpoint | None:
    """Mark VALUE as an endpoint URL: only its scheme, host, and port are
    logged, since the path and query can hold an API key. Parses nothing."""
    return None if value is None else Endpoint(value)


class Arg(str):
    """One whole value that may be a URL, such as a command-line token or
    a tool argument: from a scheme to its end it is logged as an origin."""

    __slots__ = ()


def argv(tokens: Any) -> list[Arg]:
    """Mark each of TOKENS as one whole value. Parses nothing."""
    return [Arg(token) for token in tokens]


def origin(value: str) -> str:
    """The scheme, host, and port of the URL VALUE, or a placeholder when it
    does not parse. Never raises."""
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        elif not host.isascii():
            host = httpx.URL(scheme=parts.scheme, host=host).raw_host.decode("ascii")
        # A host with any other character is not a host: it is text that ran
        # into the URL, and may be the secret.
        if not parts.scheme or not _HOST.fullmatch(host):
            return REDACTED_URL
        port = parts.port
        return f"{parts.scheme}://{host}" + ("" if port is None else f":{port}")
    except (ValueError, httpx.InvalidURL):
        return REDACTED_URL


class EndpointIdentity(str):
    """An intermediate digest. Only the writer may serialize its keyed form."""

    __slots__ = ()


def endpoint_identity(value: str) -> EndpointIdentity | None:
    """Hash the normalized request URL before handing it to the writer."""
    site = origin(value)
    if site == REDACTED_URL:
        return None
    parts = urlsplit(value.strip())
    request_url = site + (parts.path or "/") + "?" + parts.query
    return EndpointIdentity(
        hashlib.sha256(request_url.encode(errors="surrogatepass")).hexdigest()
    )


def _keyed_endpoint(identity: EndpointIdentity, key: bytes) -> str:
    return "h1_" + hmac.digest(key, bytes.fromhex(identity), "sha256").hex()


def endpoint_id(value: str, root: Path | None = None) -> str | None:
    """Read the persisted identity for VALUE without creating a key."""
    identity = endpoint_identity(value)
    if identity is None:
        return None
    opened = LogDir.open(root or directory())
    if opened is None:
        return None
    with opened:
        key = opened.endpoint_key()
    return _keyed_endpoint(identity, key) if key is not None else None


# Session tokens this process has seen. Tokens are only added: one that was
# signed out or replaced may still sit in an event waiting to be written,
# and Keeper keeps honoring it until it expires.
_known: set[str] = set()
_token_lock = threading.Lock()
_token_key: Any = None


def remember(tokens: Any) -> None:
    """Add TOKENS to the session tokens every later event is cleared of.
    `core.storage` calls this whenever it reads or writes the accounts."""
    found = [token for token in tokens if isinstance(token, str) and token]
    with _token_lock:
        _known.update(found)


def known_tokens(*, check: bool = False) -> tuple[str, ...]:
    """The session tokens to remove, longest first.

    Without CHECK this reads nothing: it returns what this process already
    saw. `event` uses it that way, so making an event never waits on the
    session store. With CHECK, as the writer thread and `redact` call it,
    the session store is reread first when its file changed. A failed read
    is retried at the next check instead of being taken for "no tokens".
    """
    global _token_key
    if check:
        try:
            from . import storage

            paths = (storage.home() / "sessions.json", storage.home() / "session.json")
            key = (str(paths[0]), *(_stat_key(path) for path in paths))
            if key != _token_key:
                # load_accounts calls remember().
                storage.load_accounts()
                _token_key = key
        except Exception:  # noqa: BLE001 - retried at the next check
            _token_key = None
    with _token_lock:
        return tuple(sorted(_known, key=len, reverse=True))


def redact(text: str) -> str:
    """Remove every session token this process knows of from TEXT, after
    checking the session store for new ones."""
    for token in known_tokens(check=True):
        text = text.replace(token, REDACTED)
    return text


def _cut(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + f"... [{len(text) - limit} more characters]"
    return text


def scrub(value: str, tokens: tuple[str, ...] | None = None) -> str:
    """VALUE with secrets removed and its length capped. TOKENS are the
    session tokens to remove, looked up when not given."""
    if isinstance(value, Endpoint):
        return origin(value)
    text = str(value)
    for token in known_tokens(check=True) if tokens is None else tokens:
        text = text.replace(token, REDACTED)
    text = _JWT.sub(REDACTED, text)
    if isinstance(value, Untrusted):
        # Where a URL ends inside someone else's text is a guess, and what
        # follows may be part of it: everything from the URL on goes.
        text = _URL_REST.sub(lambda match: f"{origin(match.group())} {CUT}", text)
        return _cut(_HEX.sub(REDACTED_HEX, text), MAX_UNTRUSTED)
    pattern = _URL_REST if isinstance(value, Arg) else _URL
    text = pattern.sub(lambda match: origin(match.group()), text)
    return _cut(_LONG_HEX.sub(REDACTED_HEX, text), MAX_TEXT)


def clean(value: Any, tokens: tuple[str, ...] | None = None) -> Any:
    """Return VALUE as JSON-ready data with every string scrubbed."""
    found = known_tokens(check=True) if tokens is None else tokens
    if isinstance(value, str):
        return scrub(value, found)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            scrub(str(key), found): clean(item, found) for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [clean(item, found) for item in value]
    return scrub(str(value), found)


def keeper_error_fields(
    errors: object, *, retained: bool = False, total: object = None
) -> dict[str, int | list[dict[str, str]]]:
    """Project Keeper's response errors or a stored event's error fields.

    Preserve the total when recorded. Older events only have the list
    that was saved.
    """
    codes: list[dict[str, str]] = []
    for item in errors[:3] if isinstance(errors, list) else ():
        if not isinstance(item, dict):
            continue
        fields = item if retained else item.get("extensions")
        code = fields.get("code") if isinstance(fields, dict) else None
        if not isinstance(code, str) or code not in _GRAPHQL_ERROR_CODES:
            code = Code.KEEPER_ERROR
        codes.append({"code": code})
    count = len(errors) if isinstance(errors, list) else 1
    if type(total) is int:
        count = max(count, total)
    return {"keeperErrors": codes, "keeperErrorCount": count}


def _clip(text: str, limit: int) -> str:
    """TEXT cut to LIMIT characters at a space, so no half of a token, URL,
    or signature is left at the end for the scrubbing to miss. A single
    unbroken value that is too long is kept only if it starts as a URL,
    which the writer reduces to its origin."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    space = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
    if space < 0:
        if _URL.match(head):
            return head
        return f"{CUT} {len(text)} characters"
    return f"{head[:space]} {CUT} {len(text) - space} more characters"


def _snapshot(
    value: Any, budget: list[int], tokens: tuple[str, ...], depth: int = 0
) -> Any:
    """Copy VALUE for the writer, within a fixed amount of work.

    Strings lose the session tokens known now, are cut to MAX_TEXT, and
    keep their `Untrusted`, `Endpoint`, or `Arg` type. Lists and mappings
    keep MAX_ITEMS items to MAX_DEPTH levels. BUDGET is the characters and
    the values left for this event. Every value is charged, numbers and
    containers included, before it is copied; past either limit a value
    becomes a marker.
    """
    budget[0] -= NODE_CHARS
    budget[1] -= 1
    if budget[0] < 0 or budget[1] < 0:
        return CUT
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        # An integer of any size is legal JSON; one this long is not a count.
        return value if value.bit_length() <= 64 else CUT
    if isinstance(value, EndpointIdentity):
        budget[0] -= len(value)
        return value
    if isinstance(value, str):
        # Look a little past the cut, so a token that straddles it goes.
        text = value[: MAX_TEXT + 4096]
        whole = len(text) == len(value)
        for token in tokens:
            if token in text:
                text = text.replace(token, REDACTED)
        if not whole or len(text) > MAX_TEXT:
            # Room is left for the marker, so the writer's own cap keeps it.
            text = _clip(text, min(MAX_TEXT - 64, len(text) - 1))
        budget[0] -= len(text)
        kind = type(value)
        return text if kind is str else kind(text)
    if depth >= MAX_DEPTH:
        return CUT
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ITEMS or budget[0] <= 0 or budget[1] <= 0:
                copied[CUT] = len(value) - index
                break
            name = _snapshot(str(key)[: MAX_UNTRUSTED + 1], budget, tokens, depth + 1)
            copied[str(name)] = _snapshot(item, budget, tokens, depth + 1)
        return copied
    if isinstance(value, (list, tuple, set, frozenset)):
        items: list[Any] = []
        for index, item in enumerate(value):
            if index >= MAX_ITEMS or budget[0] <= 0 or budget[1] <= 0:
                items.append(f"{CUT} {len(value) - index} more items")
                break
            items.append(_snapshot(item, budget, tokens, depth + 1))
        return items
    budget[0] += NODE_CHARS
    budget[1] += 1
    return _snapshot(str(value), budget, tokens, depth)


# Context


@contextmanager
def bind(**fields: Any) -> Generator[None]:
    """Add FIELDS to every event written inside the block, in this thread
    and in the threads and tasks it starts. `surface` picks the file."""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def silenced() -> Generator[None]:
    """Write no events inside the block, in any thread of this process."""
    global _silenced
    _silenced += 1
    try:
        yield
    finally:
        _silenced -= 1


def bound(name: str) -> Any:
    """The value `bind` gave NAME in this context, or None."""
    return _context.get().get(name)


# The logs directory


def redirected(path: Path) -> bool:
    """True when PATH is a symlink, a Windows junction, or any other
    reparse point, so that writing "inside" it would land somewhere else.
    Also true on Windows when that cannot be checked: without directory
    descriptors, the path is all there is to go on."""
    try:
        if path.is_symlink():
            return True
        junction = getattr(path, "is_junction", None)
        if junction is not None and junction():
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", None)
    except OSError:
        return os.name == "nt"
    if attributes is None:
        return os.name == "nt"
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


class LogDir:
    """The logs directory, opened without following a symlink.

    Files are opened, checked, and deleted relative to the directory's
    descriptor, so replacing the directory with a symlink after it was
    opened redirects nothing. Where the platform has no directory
    descriptors, as on Windows, paths are used, and a directory that is a
    symlink, a junction, or another reparse point is refused.
    """

    def __init__(self, root: Path, fd: int | None) -> None:
        self.root = root
        self.fd = fd

    @classmethod
    def open(cls, root: Path, *, create: bool = False) -> LogDir | None:
        """Open ROOT, or return None when it is missing, a symlink, not a
        directory, or owned by someone else. With CREATE, make it first, and
        remove group and other access from a directory this user owns."""
        try:
            if create:
                paths.private_directory(root.parent, profile_root=True)
                # Preserve owner access on existing log directories. The
                # descriptor below is used to clear group and other access.
                if not root.exists():
                    paths.private_directory(root)
            if not _DIR_FD:
                if redirected(root) or not root.is_dir():
                    return None
                return cls(root, None)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                os.close(fd)
                return None
            mode = stat.S_IMODE(info.st_mode)
            if create and mode & 0o077:
                os.fchmod(fd, mode & ~0o077)
        except OSError:
            os.close(fd)
            return None
        return cls(root, fd)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _target(self, name: str) -> tuple[str | Path, dict[str, Any]]:
        if self.fd is None:
            return self.root / name, {}
        return name, {"dir_fd": self.fd}

    def names(self) -> list[str]:
        try:
            return sorted(os.listdir(self.root if self.fd is None else self.fd))
        except OSError:
            return []

    def _open_file(self, name: str, flags: int) -> int:
        """Open NAME and return its descriptor, or raise OSError unless it
        is a regular file with one link that this user owns. Never waits:
        a FIFO or device in its place fails instead of blocking."""
        target, extra = self._target(name)
        if self.fd is None and Path(target).is_symlink():
            raise OSError("log file is a symlink")
        fd = os.open(target, flags | _NOFOLLOW | _NONBLOCK, FILE_MODE, **extra)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise OSError("log file is not a private regular file")
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise OSError("log file belongs to another user")
        except OSError:
            os.close(fd)
            raise
        return fd

    def append(self, name: str, data: bytes) -> None:
        """Append DATA to NAME with one write, creating it at mode 0600 and
        removing group and other access from an existing file first."""
        fd = self._open_file(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
            if mode & 0o077 and hasattr(os, "fchmod"):
                os.fchmod(fd, mode & ~0o077)
            os.write(fd, data)
        finally:
            os.close(fd)

    def _read_endpoint_key(self) -> bytes | None:
        try:
            fd = self.read_fd(ENDPOINT_KEY)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as source:
            if os.name != "nt" and os.fstat(source.fileno()).st_mode & 0o077:
                raise OSError("log identity key is not private")
            key = source.read(33)
            if len(key) != 32:
                raise OSError("invalid log identity key")
            return key

    def endpoint_key(self, *, create: bool = False) -> bytes | None:
        try:
            key = self._read_endpoint_key()
            if key is not None or not create:
                return key
            fd = self._open_file(ENDPOINT_KEY_LOCK, os.O_RDWR | os.O_CREAT)
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX)
                key = self._read_endpoint_key()
                if key is not None:
                    return key
                key = os.urandom(32)
                name = f".endpoint-key-{uuid.uuid4().hex}"
                try:
                    staged = self._open_file(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    with os.fdopen(staged, "wb") as output:
                        output.write(key)
                    source, _ = self._target(name)
                    destination, _ = self._target(ENDPOINT_KEY)
                    if self.fd is None:
                        os.replace(source, destination)
                    else:
                        os.replace(
                            source, destination, src_dir_fd=self.fd, dst_dir_fd=self.fd
                        )
                finally:
                    self.unlink(name)
                return key
            finally:
                os.close(fd)
        except OSError:
            return None

    def read_fd(self, name: str) -> int:
        return self._open_file(name, os.O_RDONLY)

    def modified(self, name: str) -> float | None:
        target, extra = self._target(name)
        try:
            return os.stat(target, follow_symlinks=False, **extra).st_mtime
        except OSError:
            return None

    def touch(self, name: str) -> None:
        """Create NAME if missing and set its modification time to now."""
        fd = self._open_file(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            if os.utime in os.supports_fd:
                os.utime(fd)
            else:
                os.utime(self.root / name)
        finally:
            os.close(fd)

    def is_regular(self, name: str) -> bool:
        target, extra = self._target(name)
        try:
            info = os.stat(target, follow_symlinks=False, **extra)
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode)

    def unlink(self, name: str) -> bool:
        target, extra = self._target(name)
        try:
            os.unlink(target, **extra)
        except OSError:
            return False
        return True

    def refused(self) -> list[tuple[str, str]]:
        """Names shaped like sai's log files, or the prune marker, that the
        writer will not append to, each with the reason."""
        found: list[tuple[str, str]] = []
        for name in self.names():
            if parse_name(name) is None and name != PRUNE_MARKER:
                continue
            target, extra = self._target(name)
            try:
                info = os.stat(target, follow_symlinks=False, **extra)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                found.append((name, "a symlink"))
            elif not stat.S_ISREG(info.st_mode):
                found.append((name, "not a regular file"))
            elif info.st_nlink > 1:
                found.append((name, "hard-linked elsewhere"))
            elif hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                found.append((name, "owned by another user"))
        return found

    def files(self) -> list[tuple[date, str, str]]:
        """Sai's log files as (day, surface, name), oldest first: regular
        files named exactly `<surface>-YYYY-MM-DD.jsonl`."""
        found: list[tuple[date, str, str]] = []
        for name in self.names():
            parsed = parse_name(name)
            if parsed is not None and self.is_regular(name):
                found.append((*parsed, name))
        return sorted(found)


def parse_name(name: str) -> tuple[date, str] | None:
    """The day and surface in a log file NAME, or None for any other name."""
    match = _FILE.fullmatch(name)
    if match is None:
        return None
    try:
        return date.fromisoformat(match["date"]), match["surface"]
    except ValueError:
        return None


def file_name(surface: str, day: date) -> str:
    return f"{surface}-{day.isoformat()}.jsonl"


def file_path(surface: str, day: date, root: Path | None = None) -> Path:
    return (root or directory()) / file_name(surface, day)


def log_files(root: Path | None = None) -> list[tuple[date, str, Path]]:
    """Every log file under ROOT as (day, surface, path), oldest first.
    Empty when ROOT is missing, a symlink, or not a directory."""
    base = root or directory()
    opened = LogDir.open(base)
    if opened is None:
        return []
    with opened:
        return [(day, surface, base / name) for day, surface, name in opened.files()]


def refused_files(root: Path | None = None) -> list[tuple[Path, str]]:
    """Entries under ROOT that sit where a log file goes and that the
    writer refuses, each with the reason: for `sai doctor`."""
    base = root or directory()
    opened = LogDir.open(base)
    if opened is None:
        return []
    with opened:
        return [(base / name, reason) for name, reason in opened.refused()]


# Retention


def expired_files(
    files: list[tuple[date, str, Any]], today: date, keep_days: int
) -> list[Any]:
    """The files older than KEEP_DAYS days: today and the KEEP_DAYS - 1
    days before it are kept."""
    oldest = today - timedelta(days=max(keep_days, 1) - 1)
    return [path for day, _surface, path in files if day < oldest]


def prune(opened: LogDir, keep_days: int, today: date) -> list[str]:
    """Delete log files older than KEEP_DAYS days, once per UTC day.

    The marker file's modification time records the last run across
    processes: a marker from TODAY or later means some process already
    pruned. Returns the names deleted.
    """
    last = opened.modified(PRUNE_MARKER)
    if last is not None and datetime.fromtimestamp(last, UTC).date() >= today:
        return []
    opened.touch(PRUNE_MARKER)
    expired = expired_files(opened.files(), today, keep_days)
    return [name for name in expired if opened.unlink(name)]


# Writing


class _Item:
    """One event waiting for the writer, with the home and the logging
    values of the environment it was written under."""

    __slots__ = ("keep", "line", "on", "root", "size")

    def __init__(
        self,
        root: str,
        on: str | None,
        keep: str | None,
        line: dict[str, Any],
        size: int,
    ) -> None:
        self.root = root
        self.on = on
        self.keep = keep
        self.line = line
        self.size = size


_queue: queue.Queue[_Item] = queue.Queue(maxsize=QUEUE_SIZE)
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
# Held by the writer while it handles one event.
_io_lock = threading.Lock()
_dropped = 0
# Characters of text in the queue, under _size_lock.
_queued_chars = 0
_size_lock = threading.Lock()
# Homes whose logging is off or failing, with the config the writer checked.
_skip: dict[
    tuple[str, str | None, str | None], tuple[object, float, _ConfigStat | None]
] = {}
# The UTC day each home was last checked for old files.
_pruned: dict[str, date] = {}


def encode(line: Mapping[str, Any]) -> bytes:
    """One JSON line. A line over MAX_LINE keeps its common fields and says
    which fields it dropped."""
    data = json.dumps(line, separators=(",", ":"), default=str)
    if len(data.encode()) > MAX_LINE:
        kept = {key: line[key] for key in COMMON if key in line}
        kept["truncated"] = sorted(key for key in line if key not in COMMON)
        data = json.dumps(kept, separators=(",", ":"), default=str)
    return data.encode() + b"\n"


def _write(item: _Item) -> None:
    """Redact and append one event. Runs on the writer thread."""
    global _dropped
    key = (item.root, item.on, item.keep)
    root = Path(item.root)
    generation = _config_generation
    (on, keep_days), signature = _resolve_config(root, item.on, item.keep)
    if not on:
        _skip[key] = (generation, time.monotonic(), signature)
        return
    opened = LogDir.open(root / DIR_NAME, create=True)
    if opened is None:
        _skip[key] = (generation, time.monotonic(), signature)
        return
    with opened:
        _skip.pop(key, None)
        row = item.line
        identity = row.get("endpointId")
        if isinstance(identity, EndpointIdentity):
            row = dict(row)
            row.pop("endpointId")
            if identity_key := opened.endpoint_key(create=True):
                row["endpointId"] = _keyed_endpoint(identity, identity_key)
        line = clean(row, known_tokens(check=True))
        today = now().date()
        # The file is the one for the day the event happened, which a slow
        # write across midnight would otherwise get wrong.
        try:
            day = date.fromisoformat(str(line["ts"])[:10])
        except ValueError:
            day = today
        if line["event"] in EVENTS and not valid_event(line):
            _dropped += 1
            return
        data = encode(line)
        if _dropped:
            lost, _dropped = _dropped, 0
            note = {k: line[k] for k in COMMON}
            note.update(level="warn", event="log.dropped", count=lost)
            data += encode(note)
        # A file that cannot be appended to loses this line only; the other
        # surfaces' files may be fine.
        with suppress(OSError):
            opened.append(file_name(line["surface"], day), data)
        if _pruned.get(item.root) != today:
            _pruned[item.root] = today
            with suppress(OSError):
                prune(opened, keep_days, today)


def _drain() -> None:
    global _queued_chars
    while True:
        item = _queue.get()
        with _size_lock:
            _queued_chars -= item.size
        try:
            with _io_lock:
                _write(item)
        except Exception:  # noqa: BLE001, S110 - a log line must not break a command
            pass
        finally:
            _queue.task_done()


def _start() -> None:
    global _thread
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_drain, name="sai-log", daemon=True)
            _thread.start()


def _target() -> tuple[str, str | None, str | None] | None:
    """The home and logging values an event would be written under now, or
    None when it would not be written: logging is silenced, SAI_LOG is off,
    or the writer found this home off or unusable moments ago."""
    if _silenced:
        return None
    raw_on = os.environ.get("SAI_LOG")
    if _off(raw_on):
        return None
    key = (str(home()), raw_on, os.environ.get("SAI_LOG_KEEP_DAYS"))
    if skipped := _skip.get(key):
        generation, checked_at, signature = skipped
        if (
            generation is _config_generation
            and time.monotonic() - checked_at < RECHECK_SECONDS
        ):
            from . import settings

            try:
                if _config_stat(Path(key[0]) / settings.FILE_NAME) == signature:
                    return None
            except OSError:
                return None
    return key


def active() -> bool:
    """Whether an event written now would be queued. Cheap, and never
    raises: callers use it to skip preparing fields nobody will read."""
    try:
        return _target() is not None
    except Exception:  # noqa: BLE001 - a log check must not break a command
        return False


def event(name: str, level: str = "info", /, **fields: Any) -> None:
    """Queue one event for the writer without waiting for the writer.

    During a retry delay, check the config's metadata before deciding to
    skip. FIELDS are copied within a fixed budget and cleared of the session
    tokens this process has seen. The writer reads the session store and
    scrubs again.
    """
    global _dropped, _queued_chars
    try:
        key = _target()
        if key is None:
            return
        context = dict(_context.get())
        surface = context.pop("surface", "cli")
        extra = {
            k: v
            for k, v in {**context, **fields}.items()
            if v is not None and k not in COMMON
        }
        budget = [EVENT_CHARS, EVENT_NODES]
        line = {
            "schema": SCHEMA_VERSION,
            "ts": stamp(now()),
            "level": level if level in LEVELS else "info",
            "event": str(name)[:MAX_UNTRUSTED],
            "surface": surface if surface in SURFACES else "cli",
            "pid": os.getpid(),
            "version": VERSION,
            "runId": RUN_ID,
            "seq": next(_seq),
            **_snapshot(extra, budget, known_tokens()),
        }
        size = EVENT_CHARS - budget[0]
        with _size_lock:
            full = _queued_chars + size > QUEUE_CHARS
            if not full:
                _queued_chars += size
        if full:
            _dropped += 1
            return
        try:
            _queue.put_nowait(_Item(*key, line, size))
        except queue.Full:
            with _size_lock:
                _queued_chars -= size
            _dropped += 1
            return
        if _thread is None or not _thread.is_alive():
            _start()
    except Exception:  # noqa: BLE001, S110 - a log line must not break a command
        pass


def flush(timeout: float = 5.0) -> bool:
    """Wait up to TIMEOUT seconds for queued events to be written. Returns
    whether the queue drained."""
    end = time.monotonic() + timeout
    with _queue.all_tasks_done:
        while _queue.unfinished_tasks:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            _queue.all_tasks_done.wait(remaining)
    return True


atexit.register(flush, EXIT_FLUSH_SECONDS)


def reset() -> None:
    """Write what is queued, then forget cached state, so the next event
    rereads the configuration and retries a directory that failed. For
    tests."""
    global _token_key, _silenced, _dropped
    flush()
    _silenced = 0
    _dropped = 0
    _token_key = None
    with _token_lock:
        _known.clear()
    invalidate_config()
    _pruned.clear()


# Reading


def _line_blocks(
    fd: int,
    chunk: int,
    *,
    end: int,
    read: Callable[[int, int], bytes] = os.read,
    max_line: int | None = None,
    complete_end: bool = True,
) -> Iterator[list[bytes]]:
    """Read a snapshot backwards, yielding once per disk chunk."""
    for _, block in _positioned_blocks(
        fd, chunk, end=end, read=read, max_line=max_line, complete_end=complete_end
    ):
        yield [raw for _, raw in block]


def _positioned_blocks(
    fd: int,
    chunk: int,
    *,
    end: int,
    read: Callable[[int, int], bytes],
    max_line: int | None,
    complete_end: bool,
) -> Iterator[tuple[int, list[tuple[int, bytes]]]]:
    """Yield each row's start and a safe resume offset after each chunk."""
    position = end
    resume = end
    rest: bytes | None = b"" if complete_end else None
    while position:
        size = min(chunk, position)
        position -= size
        os.lseek(fd, position, os.SEEK_SET)
        data = read(fd, size)
        if len(data) != size:
            return
        pieces = data.split(b"\n")
        if len(pieces) == 1:
            if rest is not None:
                combined = pieces[0] + rest
                rest = (
                    combined if max_line is None or len(combined) <= max_line else None
                )
            if rest is None:
                resume = position
            if position == 0:
                yield 0, [(0, rest)] if rest else []
                return
            yield resume, []
            continue
        offset = position + size
        block = []
        for index, piece in enumerate(reversed(pieces[1:])):
            offset -= len(piece)
            raw = piece if index else (piece + rest if rest is not None else b"")
            if raw and (max_line is None or len(raw) <= max_line):
                block.append((offset, raw))
            offset -= 1
        rest = pieces[0] if max_line is None or len(pieces[0]) <= max_line else None
        resume = position + len(pieces[0]) + 1
        if position == 0:
            if rest:
                block.append((0, rest))
            resume = 0
        yield resume, block


def _last_lines(fd: int, chunk: int) -> Iterator[bytes]:
    for block in _line_blocks(fd, chunk, end=os.fstat(fd).st_size, read=os.read):
        yield from block


def matches(line: Mapping[str, Any], name: str | None) -> bool:
    """True when the line's event is NAME or starts with `NAME.`."""
    if name is None:
        return True
    found = str(line.get("event", ""))
    return found == name or found.startswith(f"{name}.")


def _file_tail(
    opened: LogDir, file: str, count: int, name: str | None, chunk: int
) -> list[dict[str, Any]]:
    """The last COUNT matching events in FILE, newest first."""
    found: list[dict[str, Any]] = []
    try:
        fd = opened.read_fd(file)
    except OSError:
        return found
    try:
        for raw in _last_lines(fd, chunk):
            try:
                line = decode_row(raw)
            except (ValueError, RecursionError):
                continue
            if isinstance(line, dict) and matches(line, name):
                found.append(line)
                if len(found) >= count:
                    break
    except OSError:
        pass
    finally:
        os.close(fd)
    return found


def _number(value: Any) -> int:
    return value if isinstance(value, int) else 0


def event_order(line: Mapping[str, Any]) -> tuple[str, int]:
    return str(line.get("ts", "")), _number(line.get("seq"))


def _finite_number(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def _invalid_constant(_text: str) -> Any:
    raise ValueError("Invalid JSON constant")


def decode_row(raw: bytes) -> Any:
    line = json.loads(raw, parse_float=_finite_number, parse_constant=_invalid_constant)
    if (
        isinstance(line, dict)
        and line.get("event") == "keeper.request"
        and "keeperErrors" in line
    ):
        line.update(
            keeper_error_fields(
                line["keeperErrors"], retained=True, total=line.get("keeperErrorCount")
            )
        )
    return line


MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_SCAN_ROWS = 20_000
MAX_SCAN_FILES = 128


# Writers put ts before any event data. This recognizes that header in
# both schema versions, so older rows can be skipped before JSON decoding.
_TS_HEADER = re.compile(rb'^\{"(?:schema":[0-9]+,"|)ts":"([0-9TZ:.-]{24})"')


@dataclass
class LogScan:
    max_bytes: int = MAX_SCAN_BYTES
    max_rows: int = MAX_SCAN_ROWS
    max_files: int = MAX_SCAN_FILES
    truncated: bool = False
    frontier: str | None = None
    visited: set[str] = field(default_factory=set)

    def _stop_at_frontier(self, pending: Mapping[str, str | None]) -> None:
        # A fully read sparse file cannot advance a cursor past an
        # unfinished busy file on the same UTC day.
        known = [ts for ts in pending.values() if ts is not None]
        if known and len(known) == len(pending):
            self.frontier = max(known)

    def rows(
        self,
        root: Path | None = None,
        *,
        first_day: date | None = None,
        last_day: date | None = None,
        since: str | None = None,
        before: str | None = None,
        days: set[date] | None = None,
        skip: set[str] | None = None,
        prefix: bool = False,
        accept: Callable[[dict[str, Any]], bool] | None = None,
        frontier_before: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Read bounded snapshots in UTC day order, alternating surfaces."""
        opened = LogDir.open(root or directory())
        if opened is None:
            return
        remaining_bytes, remaining_rows = self.max_bytes, self.max_rows
        lower = since.encode() if since else None
        upper = before.encode() if before else None
        byte_limit = False

        def read(fd: int, size: int) -> bytes:
            nonlocal remaining_bytes, byte_limit
            if size > remaining_bytes:
                self.truncated = True
                byte_limit = True
                return b""
            data = os.read(fd, size)
            remaining_bytes -= len(data)
            return data

        with opened:
            files = [
                (day, name)
                for day, _, name in reversed(opened.files())
                if (first_day is None or day >= first_day)
                and (last_day is None or day <= last_day)
                and (days is None or day in days)
                and (skip is None or name not in skip)
            ]
            self.truncated = len(files) > self.max_files
            for _, daily in itertools.groupby(
                files[: self.max_files], key=lambda item: item[0]
            ):
                if remaining_bytes == 0:
                    self.truncated = True
                    return
                names = [name for _, name in daily]
                with ExitStack() as active:
                    readers = []
                    for name in names:
                        try:
                            fd = opened.read_fd(name)
                            active.callback(os.close, fd)
                            size = os.fstat(fd).st_size
                            if not size:
                                continue
                            if prefix:
                                # Context after midnight starts at the head of each file.
                                length = min(size, self.max_bytes // len(names))
                                data = read(fd, length)
                                self.truncated |= len(data) < size
                                readers.append((name, iter([data.split(b"\n")[:-1]])))
                            else:
                                os.lseek(fd, size - 1, os.SEEK_SET)
                                complete = read(fd, 1) == b"\n"
                                readers.append(
                                    (
                                        name,
                                        _line_blocks(
                                            fd,
                                            TAIL_CHUNK,
                                            end=size,
                                            read=read,
                                            max_line=MAX_LINE,
                                            complete_end=complete,
                                        ),
                                    )
                                )
                        except OSError:
                            self.truncated = True
                    # Read one chunk per surface before advancing a busy file.
                    pending: dict[str, str | None] = {name: None for name, _ in readers}

                    while readers:
                        next_readers = []
                        for name, reader in readers:
                            try:
                                block = next(reader)
                            except StopIteration:
                                if byte_limit:
                                    self._stop_at_frontier(pending)
                                    return
                                pending.pop(name)
                                continue
                            except OSError:
                                self.truncated = True
                                continue
                            self.visited.add(name)
                            next_readers.append((name, reader))
                            for raw in block:
                                if len(raw) > MAX_LINE:
                                    continue
                                if (lower or upper) and (
                                    header := _TS_HEADER.match(raw)
                                ):
                                    ts = header[1]
                                    if lower and ts < lower or upper and ts >= upper:
                                        continue
                                try:
                                    line = decode_row(raw)
                                except (ValueError, RecursionError):
                                    continue
                                if not isinstance(line, dict):
                                    continue
                                if since or before:
                                    ts = line.get("ts")
                                    if not isinstance(ts, str) or (
                                        since and ts < since or before and ts >= before
                                    ):
                                        continue
                                if accept is not None and not accept(line):
                                    continue
                                if remaining_rows == 0:
                                    self.truncated = True
                                    self._stop_at_frontier(pending)
                                    return
                                remaining_rows -= 1
                                ts = line.get("ts")
                                if (
                                    isinstance(ts, str)
                                    and (
                                        frontier_before is None or ts < frontier_before
                                    )
                                    and (current := pending[name]) != ts
                                ):
                                    pending[name] = min(current, ts) if current else ts
                                yield line
                        readers = next_readers


@dataclass(frozen=True)
class ScanPosition:
    day: date
    # Exclusive byte endpoints in SURFACES order. None starts a fresh day.
    offsets: tuple[int, ...] | None = None


@dataclass
class PageScan:
    max_bytes: int = MAX_SCAN_BYTES
    max_rows: int = MAX_SCAN_ROWS
    max_files: int = MAX_SCAN_FILES
    truncated: bool = False
    resume: ScanPosition | None = None

    def rows(
        self,
        root: Path | None = None,
        *,
        start: ScanPosition | None = None,
        first_day: date | None = None,
        last_day: date | None = None,
        since: str | None = None,
        before: str | None = None,
        observe: Callable[[dict[str, Any]], None] | None = None,
    ) -> Generator[tuple[ScanPosition, dict[str, Any]]]:
        """Merge reverse file reads one day at a time and checkpoint each file."""
        opened = LogDir.open(root or directory())
        if opened is None:
            return
        bytes_left, rows_left, files_left = (
            self.max_bytes,
            self.max_rows,
            self.max_files,
        )
        byte_limit = False

        def read(fd: int, size: int) -> bytes:
            nonlocal bytes_left, byte_limit
            if size > bytes_left:
                self.truncated = byte_limit = True
                return b""
            data = os.read(fd, size)
            bytes_left -= len(data)
            return data

        def decode_block(
            block: list[tuple[int, bytes]],
        ) -> Iterator[tuple[int, int, dict[str, Any]]]:
            for offset, raw in block:
                if (
                    observe is None
                    and (since or before)
                    and (header := _TS_HEADER.match(raw))
                ):
                    ts = header[1].decode()
                    if (since and ts < since) or (before and ts >= before):
                        continue
                try:
                    line = decode_row(raw)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(line, dict):
                    continue
                if observe is not None:
                    observe(line)
                ts = line.get("ts")
                if isinstance(ts, str) and (
                    (since is None or ts >= since) and (before is None or ts < before)
                ):
                    yield offset, offset + len(raw) + 1, line

        def read_rows(
            slot: int, fd: int, end: int, offsets: list[int]
        ) -> Iterator[tuple[int, dict[str, Any]]]:
            os.lseek(fd, end - 1, os.SEEK_SET)
            complete = read(fd, 1) == b"\n"
            for resume, block in _positioned_blocks(
                fd,
                TAIL_CHUNK,
                end=end,
                read=read,
                max_line=MAX_LINE,
                complete_end=complete,
            ):
                # Observe the whole chunk before choosing its first merge candidate.
                # A call in the chunk can identify gaps from other files.
                candidates = decode_block(block)
                for offset, row_end, line in (
                    list(candidates) if observe is not None else candidates
                ):
                    offsets[slot] = row_end
                    yield offset, line
                offsets[slot] = resume

        def advance(
            slot: int,
            readers: dict[int, Iterator[tuple[int, dict[str, Any]]]],
            heads: dict[int, tuple[int, dict[str, Any]]],
            offsets: list[int],
        ) -> None:
            try:
                heads[slot] = next(readers[slot])
            except StopIteration:
                heads.pop(slot, None)
                if not byte_limit:
                    offsets[slot] = 0
            except OSError:
                heads.pop(slot, None)
                self.truncated = True

        with opened:
            files = (
                (day, surface, name)
                for day, surface, name in reversed(opened.files())
                if (first_day is None or day >= first_day)
                and (last_day is None or day <= last_day)
                and (start is None or day <= start.day)
            )
            for day, daily in itertools.groupby(files, key=lambda item: item[0]):
                names = list(daily)
                if len(names) > files_left:
                    self.truncated = True
                    self.resume = ScanPosition(day)
                    return
                files_left -= len(names)
                offsets = [0] * len(SURFACES)
                with ExitStack() as active:
                    readers: dict[int, Iterator[tuple[int, dict[str, Any]]]] = {}

                    for _, surface, name in names:
                        slot = SURFACES.index(surface)
                        try:
                            fd = opened.read_fd(name)
                            active.callback(os.close, fd)
                            end = os.fstat(fd).st_size
                            if (
                                start is not None
                                and day == start.day
                                and start.offsets is not None
                            ):
                                end = min(end, start.offsets[slot])
                            offsets[slot] = end
                            if end:
                                readers[slot] = read_rows(slot, fd, end, offsets)
                        except OSError:
                            self.truncated = True

                    heads: dict[int, tuple[int, dict[str, Any]]] = {}

                    for slot in readers:
                        advance(slot, readers, heads, offsets)
                        if byte_limit:
                            self.resume = ScanPosition(day, tuple(offsets))
                            return
                    while heads:
                        if rows_left == 0:
                            self.truncated = True
                            self.resume = ScanPosition(day, tuple(offsets))
                            return
                        slot = max(
                            heads, key=lambda key: (event_order(heads[key][1]), key)
                        )
                        offset, line = heads[slot]
                        position = ScanPosition(day, tuple(offsets))
                        offsets[slot] = offset
                        rows_left -= 1
                        yield position, line
                        advance(slot, readers, heads, offsets)
                        if byte_limit:
                            self.resume = ScanPosition(day, tuple(offsets))
                            return


def tail(
    count: int,
    surface: str | None = None,
    name: str | None = None,
    root: Path | None = None,
    chunk: int = TAIL_CHUNK,
) -> list[dict[str, Any]]:
    """The last COUNT events, oldest first, across every surface or one.

    NAME keeps one event, or a family by its prefix: `keeper` matches
    `keeper.request`. Each file is read from its end. An event can be
    appended after a newer one, by another process or a slow writer, so
    COUNT + TAIL_SLACK events are read from each file, merged by time, and
    only then cut to COUNT.
    """
    if count <= 0:
        return []
    flush(1.0)
    opened = LogDir.open(root or directory())
    if opened is None:
        return []
    with opened:
        by_day: dict[date, list[str]] = {}
        for day, found, file in opened.files():
            if surface is None or found == surface:
                by_day.setdefault(day, []).append(file)
        lines: list[dict[str, Any]] = []
        for day in sorted(by_day, reverse=True):
            for file in by_day[day]:
                lines += _file_tail(opened, file, count + TAIL_SLACK, name, chunk)
            if len(lines) >= count:
                break
    lines.sort(key=event_order)
    return lines[-count:]


def render(line: Mapping[str, Any]) -> str:
    """One event as a line of text: time, surface, level, event, then its
    own fields as `key=value`."""
    head = " ".join(
        str(line.get(key, "?")) for key in ("ts", "surface", "level", "event")
    )
    rest = " ".join(
        f"{key}={json.dumps(value, separators=(',', ':'), default=str)}"
        if not isinstance(value, (str, int, float)) or isinstance(value, bool)
        else f"{key}={value}"
        for key, value in line.items()
        if key not in COMMON
    )
    return f"{head} {rest}".rstrip()
