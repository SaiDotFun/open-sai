"""Read bounded, scrubbed reports from retained logs and Keeper."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zlib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import keeper, log, paths, storage
from .errors import Code, ExitCode, SaiError
from .log_events import COMMON_OPTIONAL, COMMON_REQUIRED, EVENTS

LogLine = dict[str, Any]
Scope = tuple[str, str]
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")
PROCESS_RUN_ID = re.compile(r"[a-f0-9]{32}")
ENDPOINT_ID = re.compile(r"h1_[a-f0-9]{64}")
SHA256_HEX = re.compile(r"[a-f0-9]{64}")
MCP_STATUSES = frozenset(
    {"accepted", "rejected", "not_sent", "unknown", "terminal", "absent"}
)
TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z"
)
SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
MAX_REPORT_ROWS = 128
MAX_REPORT_BYTES = 96 * 1024
CODES = frozenset(Code)
OPERATIONS = frozenset(keeper.OPERATIONS.values()) | {"custom"}
FREE_TEXT = {
    "argv",
    "arguments",
    "error",
    "errorMsg",
    "keeperErrors",
    "checks",
    "message",
    "reason",
}
INTENT_EVIDENCE = {
    "intent.reserved",
    "intent.status",
    "intent.submitted",
    "intent.keeper",
    "intent.wait.end",
    "intent.status_write_failed",
}
TX_EVENTS = ("intent.submitted", "intent.wait.end", "intent.keeper")


def _id(line: LogLine, field: str) -> str | None:
    value = line.get(field)
    return value if isinstance(value, str) and value else None


def _timestamp(line: LogLine) -> str | None:
    ts = _id(line, "ts")
    if ts and TIMESTAMP.fullmatch(ts):
        try:
            datetime.fromisoformat(ts)
            return ts
        except ValueError:
            pass
    return None


def _address(line: LogLine) -> str | None:
    address = _id(line, "addr") or _id(line, "account")
    return address.lower() if address and keeper.is_address(address) else None


def _scope(line: LogLine) -> Scope | None:
    account, identity = _address(line), _id(line, "endpointId")
    if account and identity and ENDPOINT_ID.fullmatch(identity):
        return account, identity
    return None


def _invocation(line: LogLine) -> tuple[str, str, str] | None:
    run_id = _id(line, "runId")
    call_id = _id(line, "callId")
    command_id = _id(line, "commandId")
    if run_id and call_id:
        return run_id, "call", call_id
    if run_id and command_id and line.get("surface") != "mcp":
        return run_id, "command", command_id
    return None


def _lookup_intent(endpoint: str, key: str, addr: str) -> LogLine | None:
    data = keeper.request(
        endpoint, keeper.INTENTS, {"user": addr, "key": key}, trace_intent=False
    )
    rows = data.get("userIntents")
    if not isinstance(rows, list):
        raise SaiError("Keeper did not return intent records", Code.INCOMPLETE_DATA)
    for row in rows:
        if not isinstance(row, dict) or row.get("intentKey") != key:
            continue
        nibi = row.get("nibiAddress")
        if keeper.canonical_account(row.get("evmAddress")) == addr.lower() and (
            nibi is None or keeper.canonical_account(nibi) == addr.lower()
        ):
            return keeper.safe_intent_record(row)
    return None


def _has_intent(line: LogLine, key: str) -> bool:
    return line.get("intentKey") == key and line.get("tool") != "logs_intent"


def _select_scope(
    anchors: list[LogLine], addr: str | None, identity: str | None
) -> tuple[Scope | None, bool]:
    candidates: set[Scope] = set()
    evidence: set[Scope] = set()
    for line in anchors:
        scope = _scope(line)
        if (
            not scope
            or (addr and scope[0] != addr)
            or (identity is not None and scope[1] != identity)
        ):
            continue
        candidates.add(scope)
        if line.get("event") in INTENT_EVIDENCE and line.get("found") is not False:
            evidence.add(scope)
    scopes = evidence or candidates
    if len(scopes) == 1:
        return next(iter(scopes)), False
    return None, bool(scopes)


def _active_address(endpoint: str) -> str | None:
    try:
        active = storage.load_session()
    except (ValueError, OSError):
        return None
    if active.get("endpoint") == endpoint:
        return _address({"addr": active.get("evmAddress")})
    return None


def _before(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        instant = datetime.fromisoformat(value)
        if instant.utcoffset() == timedelta(0):
            return instant
    except ValueError:
        pass
    raise SaiError(
        "Pass a UTC timestamp to --before.",
        Code.INVALID_REQUEST,
        exit_code=ExitCode.USAGE,
    )


CONTEXT_BYTES = 256 * 1024
CONTEXT_ROWS = 256
CONTEXT_FILES = len(log.SURFACES)


def _prefix(
    root: Path | None,
    day: date,
    accept: Callable[[LogLine], bool],
    snapshot: log.FileSnapshot | None = None,
    *,
    budget: log.ScanBudget | None = None,
) -> tuple[list[tuple[log.RowPosition, LogLine]], bool]:
    return log.read_prefix(
        root,
        day,
        accept,
        max_bytes=CONTEXT_BYTES,
        max_rows=CONTEXT_ROWS,
        snapshot=snapshot,
        budget=budget,
    )


def _trail_scan() -> log.PageScan:
    return log.PageScan(
        max_bytes=log.MAX_SCAN_BYTES - 2 * CONTEXT_BYTES,
        max_rows=log.MAX_SCAN_ROWS - 2 * CONTEXT_ROWS,
        max_files=log.MAX_SCAN_FILES - 2 * CONTEXT_FILES,
    )


NO_CALL_PARENT = "-"
# Intent cursors use the otherwise unused parent field after anchor discovery.
INTENT_ATTRIBUTION_COMPLETE = "-"


def _parse_position(day: str, parts: str) -> log.ScanPosition:
    offsets = None if parts == "-" else tuple(int(part) for part in parts.split(","))
    if offsets is not None and (
        len(offsets) != len(log.SURFACES)
        or any(not 0 <= offset < 2**63 for offset in offsets)
    ):
        raise ValueError
    return log.ScanPosition(date.fromisoformat(day), offsets)


def _encode_position(position: log.ScanPosition) -> tuple[str, str]:
    offsets = "-" if position.offsets is None else ",".join(map(str, position.offsets))
    return position.day.isoformat(), offsets


@dataclass(frozen=True)
class _Cursor:
    upper: str | None
    position: log.ScanPosition | None = None
    timestamp: str | None = None
    parent: str = ""
    replay: log.ScanPosition | None = None

    @classmethod
    def parse(cls, value: str | None) -> _Cursor:
        if value is None or "~" not in value:
            instant = _before(value)
            return cls(log.stamp(instant) if instant else None)
        try:
            if len(value) > 280:
                raise ValueError
            timestamp, day, parts, upper, parent, *replay = value.split("~")
            if (
                _timestamp({"ts": timestamp}) is None
                or (upper and _timestamp({"ts": upper}) is None)
                or (
                    parent not in ("", NO_CALL_PARENT)
                    and SHA256_HEX.fullmatch(parent) is None
                )
                or (replay and (len(replay) != 2 or parent))
            ):
                raise ValueError
            return cls(
                upper or None,
                _parse_position(day, parts),
                timestamp,
                parent,
                _parse_position(replay[0], replay[1]) if replay else None,
            )
        except ValueError:
            raise SaiError(
                "Pass a log cursor or a UTC timestamp to --before.",
                Code.INVALID_REQUEST,
                exit_code=ExitCode.USAGE,
            ) from None

    @property
    def last_day(self) -> date | None:
        # Only an explicit timestamp filter bounds which rows can be returned.
        # The position cursor's timestamp describes the last consumed row,
        # and an unread row can have a later timestamp.
        return date.fromisoformat(self.upper[:10]) if self.upper else None

    def encode(
        self,
        position: log.ScanPosition,
        timestamp: str,
        parent: str,
        replay: log.ScanPosition | None,
    ) -> str:
        fields = (timestamp, *_encode_position(position), self.upper or "", parent)
        return "~".join(
            (*fields, *_encode_position(replay)) if replay is not None else fields
        )


CURSOR_STATE_SUFFIX = "~s2~"
CURSOR_STATE_NAME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})_([0-9a-f]{64})")
MAX_CURSOR_STATES = 128
MAX_CURSOR_CACHE_BYTES = 16 * 1024 * 1024
MAX_CURSOR_FILE_BYTES = 1024 * 1024
MAX_CURSOR_STATE_BYTES = 8 * 1024 * 1024
LinkPositions = dict[str, set[log.RowPosition]]
LINK_TAG = re.compile(r"[0-9a-f]{16}")


def _cursor_directories(
    root: Path | None, *, create: bool = False
) -> Iterator[log.LogDir]:
    root = root or log.directory()
    if opened := log.LogDir.open(root / ".intent-cursors", create=create):
        yield opened

    # A log directory may be readable and stored on a read-only mount.
    location = (str(Path.home()) + "\0" + str(root.absolute())).encode(
        errors="surrogatepass"
    )
    fallback = (
        Path(tempfile.gettempdir())
        / f"sai-intent-cursors-{hashlib.sha256(location).hexdigest()}"
    )
    try:
        if create:
            paths.private_directory(fallback)
    except OSError:
        return
    if opened := log.LogDir.open(fallback):
        yield opened


def _read_cursor_state(root: Path | None, name: str) -> bytes:
    match = CURSOR_STATE_NAME.fullmatch(name)
    if match is None:
        raise ValueError
    for opened in _cursor_directories(root):
        with opened:
            try:
                with os.fdopen(opened.read_fd(name), "rb", buffering=0) as stream:
                    data = stream.read(MAX_CURSOR_FILE_BYTES + 1)
                if (
                    len(data) > MAX_CURSOR_FILE_BYTES
                    or hashlib.sha256(data).hexdigest() != match[2]
                ):
                    continue
                inflater = zlib.decompressobj()
                raw = inflater.decompress(data, MAX_CURSOR_STATE_BYTES + 1)
                if (
                    inflater.eof
                    and not inflater.unused_data
                    and len(raw) <= MAX_CURSOR_STATE_BYTES
                ):
                    return raw
            except (OSError, zlib.error):
                continue
    raise SaiError(
        "Log cursor state is unavailable. Start a new intent log query.",
        Code.INVALID_REQUEST,
        exit_code=ExitCode.USAGE,
    )


def _prune_cursor_states(opened: log.LogDir, keep: str, oldest: str) -> None:
    candidates = []
    for name in opened.names():
        if (
            not CURSOR_STATE_NAME.fullmatch(name)
            or (info := opened.file_info(name)) is None
        ):
            continue
        if info.st_size > MAX_CURSOR_FILE_BYTES or (
            name != keep and name[:10] < oldest
        ):
            opened.unlink(name)
            continue
        candidates.append((name == keep, info.st_mtime_ns, name, info.st_size))

    retained = used_bytes = 0
    for _, _, name, size in sorted(candidates, reverse=True):
        if retained == MAX_CURSOR_STATES or used_bytes + size > MAX_CURSOR_CACHE_BYTES:
            opened.unlink(name)
        else:
            retained += 1
            used_bytes += size


def _write_cursor_state(
    root: Path | None, raw: bytes, snapshot: log.FileSnapshot
) -> str:
    if len(raw) > MAX_CURSOR_STATE_BYTES:
        raise SaiError(
            "Intent log continuation exceeds the cache limit.", Code.INTERNAL_ERROR
        )
    data = zlib.compress(raw)
    if len(data) > MAX_CURSOR_FILE_BYTES:
        raise SaiError(
            "Intent log continuation exceeds the cache limit.", Code.INTERNAL_ERROR
        )
    name = f"{max(snapshot)[0].isoformat()}_{hashlib.sha256(data).hexdigest()}"
    for opened in _cursor_directories(root, create=True):
        with opened:
            try:
                opened.publish_private(name, data)
            except OSError:
                continue
            # A snapshot cannot refer to a file newer than the day in its name.
            retained = log.snapshot_files(root)
            oldest = min(retained)[0].isoformat() if retained else ""
            _prune_cursor_states(opened, name, oldest)
            return name
    raise SaiError("Could not save the intent log continuation.", Code.INTERNAL_ERROR)


def _intent_cursor(
    value: str | None,
    root: Path | None,
) -> tuple[_Cursor, list[log.RowPosition], log.FileSnapshot | None, LinkPositions]:
    base, separator, payload = (value or "").partition(CURSOR_STATE_SUFFIX)
    if not separator:
        if "~s~" in base or "~i~" in base:
            raise log.stale_snapshot()
        cursor = _Cursor.parse(value)
        if cursor.position is not None:
            raise log.stale_snapshot()
        return cursor, [], None, {}
    cursor = _Cursor.parse(base)
    try:
        if cursor.position is None:
            raise ValueError
        items, files, tagged = json.loads(_read_cursor_state(root, payload))
        if not all(isinstance(value, list) for value in (items, files, tagged)):
            raise TypeError

        def position(item: Any) -> log.RowPosition:
            if (
                not isinstance(item, list)
                or len(item) != 4
                or any(type(value) is not int for value in item)
            ):
                raise ValueError
            day, slot, offset, size = item
            if not (
                0 <= slot < len(log.SURFACES)
                and 1 <= size <= log.MAX_LINE + 1
                and 0 <= offset < 2**63 - size
            ):
                raise ValueError
            return log.RowPosition(date.fromordinal(day), slot, offset, size)

        positions = [position(item) for item in items]
        snapshot: log.FileSnapshot = {}
        for row in files:
            if (
                not isinstance(row, list)
                or len(row) != 6
                or any(type(value) is not int for value in row)
            ):
                raise ValueError
            day, slot, size, device, inode, modified = row
            if not (0 <= slot < len(log.SURFACES) and 0 < size < 2**63):
                raise ValueError
            snapshot[date.fromordinal(day), slot] = log.FileExtent(
                size, device, inode, modified
            )
        links: LinkPositions = defaultdict(set)
        for entry in tagged:
            if (
                not isinstance(entry, list)
                or len(entry) != 5
                or not isinstance(entry[0], str)
                or LINK_TAG.fullmatch(entry[0]) is None
            ):
                raise ValueError
            links[entry[0]].add(position(entry[1:]))
        return cursor, positions, snapshot, links
    except (TypeError, ValueError, OverflowError, RecursionError, zlib.error):
        raise SaiError(
            "Pass a log cursor or a UTC timestamp to --before.",
            Code.INVALID_REQUEST,
            exit_code=ExitCode.USAGE,
        ) from None


def _save_intent_cursor(
    report: LogLine,
    positions: list[log.RowPosition],
    snapshot: log.FileSnapshot,
    links: LinkPositions,
    root: Path | None,
) -> None:
    if base := report.get("nextBefore"):

        def fields(row: log.RowPosition) -> tuple[int, int, int, int]:
            return row.day.toordinal(), row.slot, row.offset, row.size

        raw = json.dumps(
            (
                list(map(fields, positions)),
                [
                    (
                        day.toordinal(),
                        slot,
                        extent.size,
                        extent.device,
                        extent.inode,
                        extent.modified,
                    )
                    for (day, slot), extent in sorted(snapshot.items())
                ],
                [
                    (tag, *fields(row))
                    for tag, refs in links.items()
                    for row in sorted(refs)
                ],
            ),
            separators=(",", ":"),
        ).encode()
        report["nextBefore"] = (
            base + CURSOR_STATE_SUFFIX + _write_cursor_state(root, raw, snapshot)
        )


def _anchor_group(line: LogLine) -> tuple[Any, ...]:
    """Rows with these facts in common provide the same attribution."""
    event = _id(line, "event")
    scope = _scope(line)
    return (
        scope,
        _invocation(line),
        _id(line, "runId"),
        event in INTENT_EVIDENCE and line.get("found") is not False,
        scope is None
        and (line.get("endpoint") is not None or event in INTENT_EVIDENCE),
        event
        if event in TX_EVENTS and keeper.request_hash(line.get("txHash"))[0]
        else None,
    )


def _link_tag(kind: str, value: str | tuple[str, str, str]) -> str:
    # The cursor indexes candidate locations. The retained row must still
    # establish the relationship before the report can use it.
    return hashlib.blake2s(
        json.dumps((kind, value)).encode(), digest_size=8
    ).hexdigest()


def _export(line: LogLine, tokens: tuple[str, ...]) -> LogLine:
    name = _id(line, "event")
    spec = EVENTS.get(name) if name else None
    fields = COMMON_REQUIRED.keys() | COMMON_OPTIONAL.keys()
    if spec:
        fields |= spec.required.keys() | spec.optional.keys()
    safe: LogLine = {
        field: value
        for field, value in line.items()
        if field in fields
        and field not in FREE_TEXT
        and isinstance(value, (str, int, bool))
    }
    if name == "keeper.request" and "keeperErrors" in line:
        # The shared decoder has already projected these error entries.
        safe["keeperErrors"] = line["keeperErrors"]
    for field, allowed in (
        ("kind", keeper.INTENT_KINDS),
        ("lifecycleStep", keeper.INTENT_STEPS),
        ("execActor", keeper.INTENT_ACTORS),
        ("errorCode", keeper.INTENT_ERROR_CODES | {Code.KEEPER_ERROR}),
        ("code", CODES),
    ):
        if field in safe and keeper.known_text(safe[field], allowed) is None:
            del safe[field]
    if safe.get("code") and ("error" in line or "errorMsg" in line):
        safe["error"] = log.untrusted("Sai recorded a failure. See the error code.")
    for field in ("addr", "account"):
        if field in safe and not keeper.is_address(safe[field]):
            del safe[field]
    if "operation" in safe and safe["operation"] not in OPERATIONS:
        del safe["operation"]
    if name == "mcp.tool_result":
        if "status" in safe and safe["status"] not in MCP_STATUSES:
            del safe["status"]
        if "previewDigest" in safe and not SHA256_HEX.fullmatch(
            str(safe["previewDigest"])
        ):
            del safe["previewDigest"]
    for name in ("intentKey", "runId", "callId", "commandId"):
        if name in safe and not TOKEN.fullmatch(str(safe[name])):
            del safe[name]
    tx_hash, invalid = keeper.request_hash(line.get("txHash"))
    safe.pop("txHash", None)
    if tx_hash:
        safe["txHash"] = tx_hash
    if invalid:
        safe["txHashInvalid"] = True
    return log.clean(safe, tokens)


class _PageReport:
    def __init__(self, report: LogLine) -> None:
        self.report = report
        self.tokens = log.known_tokens(check=True)
        self.bytes_left = MAX_REPORT_BYTES - 8192
        self.rows_left = MAX_REPORT_ROWS
        self.fields: set[str] = set()

    def add(self, field: str, line: LogLine) -> bool:
        safe = _export(line, self.tokens)
        size = len(json.dumps(safe, allow_nan=False).encode()) + 2
        if self.rows_left == 0 or size > self.bytes_left:
            return False
        self.report[field].append(safe)
        self.fields.add(field)
        self.rows_left -= 1
        self.bytes_left -= size
        return True

    def finish(
        self,
        scan: log.PageScan,
        cursor: _Cursor,
        last: str | None,
        stop: log.ScanPosition | None,
        parent: str = "",
        *,
        replay: log.ScanPosition | None = None,
    ) -> LogLine:
        for field in self.fields:
            self.report[field].sort(key=log.event_order)
        if scan.truncated or stop is not None:
            self.report["truncated"] = True
            self.report["more"] = "Use --before to continue reading the retained logs."
            if (position := stop or scan.resume) is not None:
                # A timestamp-only query can exhaust its byte budget while
                # skipping newer rows. The continuation still has a position.
                stamp = last or cursor.timestamp or cursor.upper
                if stamp is None:
                    stamp = f"{position.day.isoformat()}T00:00:00.000Z"
                self.report["nextBefore"] = cursor.encode(
                    position, stamp, parent, replay
                )
        return self.report


def intent(
    key: str,
    *,
    endpoint: str | None = None,
    addr: str | None = None,
    offline: bool = False,
    before: str | None = None,
    root: Path | None = None,
) -> LogLine:
    """Join the evidence for one account and endpoint, with optional Keeper state."""
    if keeper.INTENT_KEY.fullmatch(key) is None:
        raise SaiError("Invalid intent key.", Code.INVALID_REQUEST)
    if addr is not None and not keeper.is_address(addr):
        raise SaiError("Pass a 0x-prefixed EVM address.", Code.INVALID_REQUEST)
    chosen_addr = addr.lower() if addr else None
    cursor, positions, snapshot, links = _intent_cursor(before, root)
    log.flush(1.0)
    if snapshot is None:
        snapshot = log.snapshot_files(root)
    elif start := cursor.position:
        required = {
            (day, slot)
            for day, slot in snapshot
            if (cursor.last_day is None or day <= cursor.last_day)
            and (
                day < start.day
                or (day == start.day and (start.offsets is None or start.offsets[slot]))
            )
        }
        required.update((ref.day, ref.slot) for ref in positions)
        for refs in links.values():
            required.update((ref.day, ref.slot) for ref in refs)
        log.check_snapshot(root, snapshot, required)
    evidence: dict[tuple[Any, ...], tuple[log.RowPosition, LogLine]] = {}

    def is_anchor(line: LogLine) -> bool:
        return _has_intent(line, key) and _timestamp(line) is not None

    def remember_anchor(position: log.RowPosition, line: LogLine) -> None:
        if is_anchor(line):
            group = _anchor_group(line)
            previous = evidence.get(group)
            if previous is None or log.event_order(line) > log.event_order(previous[1]):
                evidence[group] = (position, line)

    budget = log.ScanBudget()
    scan = log.PageScan()
    previous, scan.truncated = log.read_rows_at(root, positions, snapshot, budget)
    checked = set(positions)
    rows = []
    if not scan.truncated:
        for position, line in previous:
            remember_anchor(position, line)
        if (
            cursor.position is None
            and (day := cursor.last_day) is not None
            and day < date.max
        ):
            following_day = day + timedelta(days=1)
            following, truncated = _prefix(
                root,
                following_day,
                is_anchor,
                snapshot,
                budget=budget,
            )
            for position, line in following:
                remember_anchor(position, line)
            scan.truncated |= truncated
        pending = (
            set().union(*links.values()) - checked
            if links and cursor.replay is None
            else set()
        )
        # Leave part of the same budget for links the selected rows need.
        reserved = log.ScanBudget(
            min(sum(ref.size for ref in pending), budget.bytes_left // 2),
            min(len(pending), budget.rows_left // 2),
            min(len({(ref.day, ref.slot) for ref in pending}), budget.files_left // 2),
        )
        rows = [
            (position, ts, line)
            for position, line in scan.rows(
                root,
                start=cursor.position,
                last_day=cursor.last_day,
                before=cursor.upper,
                snapshot=snapshot,
                budget=budget,
                reserve=reserved,
                observe=remember_anchor,
                context=is_anchor,
            )
            if (ts := _timestamp(line)) is not None
        ]
        if not rows and scan.resume == cursor.position:
            scan.resume = None
    complete_scan = not scan.truncated
    anchors = sorted((line for _, line in evidence.values()), key=log.event_order)
    identity = (log.endpoint_id(endpoint, root) or "") if endpoint else None
    scope, ambiguous = _select_scope(anchors, chosen_addr, identity)
    direct = [line for line in anchors if scope is not None and _scope(line) == scope]
    invocations = {match for line in direct if (match := _invocation(line))}
    conflicting = {
        match
        for line in anchors
        if _scope(line) not in (None, scope)
        and (match := _invocation(line)) in invocations
    }
    report: LogLine = {
        "intentKey": key if scope else None,
        "events": [],
        "gaps": [],
        "keeper": None,
        "txHash": None,
    }
    if ambiguous or any(
        _scope(line) is None
        and (line.get("endpoint") is not None or line.get("event") in INTENT_EVIDENCE)
        for line in anchors
    ):
        report["scopeWarning"] = (
            "Intent events have ambiguous account or endpoint attribution."
            if ambiguous
            else "Older events without sufficient attribution were omitted."
        )
    if endpoint is not None and not offline:
        account = scope[0] if scope else chosen_addr or _active_address(endpoint)
        if ambiguous or account is None:
            report["keeperError"] = {
                "code": Code.INVALID_REQUEST,
                "message": (
                    "Several accounts match this key. Pass an address for the Keeper lookup."
                    if ambiguous
                    else "Pass an address for the Keeper lookup or use offline mode."
                ),
            }
        else:
            try:
                report["keeper"] = _lookup_intent(endpoint, key, account)
            except SaiError as exc:
                report["keeperError"] = {
                    "code": exc.code,
                    "message": log.clean(log.untrusted("Keeper intent lookup failed.")),
                }

    current = report["keeper"] or {}
    if current:
        report["intentKey"] = current.get("intentKey", report["intentKey"])
    request_hash = next(
        (
            tx_hash
            for event in TX_EVENTS
            for line in reversed(direct)
            if line.get("event") == event
            and (tx_hash := keeper.request_hash(line.get("txHash"))[0])
        ),
        current.get("txHash"),
    )
    report["txHash"] = keeper.request_hash(log.clean(request_hash))[0]
    matched_runs = {_id(line, "runId") for line in direct} - {None}

    def resolve_link(tag: str) -> bool:
        pending = links.get(tag, set()) - checked
        previous, truncated = log.read_rows_at(root, list(pending), snapshot, budget)
        if truncated:
            return False
        checked.update(pending)
        for position, anchor in previous:
            if not is_anchor(anchor):
                continue
            remember_anchor(position, anchor)
            anchor_scope = _scope(anchor)
            if anchor_scope == scope:
                if run_id := _id(anchor, "runId"):
                    matched_runs.add(run_id)
                if match := _invocation(anchor):
                    invocations.add(match)
            elif anchor_scope is not None and (match := _invocation(anchor)):
                conflicting.add(match)
        return True

    page = _PageReport(report)
    last = None
    stop = None
    replay = cursor.replay
    parent = cursor.parent
    can_defer = scan.resume is not None and parent != INTENT_ATTRIBUTION_COMPLETE
    unattributed = []
    for position, ts, line in rows if replay is None else ():
        if line.get("event") == "log.dropped":
            run_id = _id(line, "runId")
            if (
                scope is not None
                and run_id
                and run_id not in matched_runs
                and not resolve_link(_link_tag("run", run_id))
            ):
                stop = position
                break
            if run_id not in matched_runs:
                if run_id and can_defer:
                    replay = position
                    break
                unattributed.append(line)
                continue
            field = "gaps"
        elif scope is not None and _has_intent(line, key) and _scope(line) == scope:
            field = "events"
        elif (
            (match := _invocation(line)) is not None
            and line.get("intentKey") in (None, key)
            and (
                scope is None
                or (
                    _address(line) in (None, scope[0])
                    and (
                        line.get("endpoint") is None
                        or line.get("endpointId") == scope[1]
                    )
                    and line.get("endpointId") in (None, scope[1])
                )
            )
        ):
            if scope is not None and not resolve_link(_link_tag("invocation", match)):
                stop = position
                break
            if scope is None or match not in invocations:
                if can_defer:
                    replay = position
                    break
                continue
            if match in conflicting:
                continue
            field = "events"
        else:
            continue
        if not page.add(field, line):
            stop = position
            break
        last = ts
    if unattributed:
        report["unattributedGapCount"] = len(unattributed)
        report["unattributedGaps"] = []
        scan.truncated |= len(unattributed) > 8
        scan.truncated |= any(
            not page.add("unattributedGaps", line) for line in unattributed[:8]
        )
    if stop is not None and stop == cursor.position and last is None:
        # The first unread row needs more context than this page can verify.
        scan.truncated = True
        scan.resume = stop = None
    keep_all_links = replay is not None
    if replay is not None and complete_scan:
        # The search has reached the end of the snapshot. Resume output at
        # the first withheld row with the anchors found across all windows.
        stop, replay, parent = replay, None, INTENT_ATTRIBUTION_COMPLETE
    page.finish(scan, cursor, last, stop, parent, replay=replay)
    if frontier := stop or scan.resume:
        # Scope evidence can be deeper than the next page's scan. Links to
        # unread anchors are only needed when the scan already saw related rows.
        remaining_rows = rows
        if complete_scan and stop is not None and not keep_all_links:
            first_unread = next(
                index for index, (position, _, _) in enumerate(rows) if position == stop
            )
            remaining_rows = rows[first_unread:]
        related_invocations = {
            match
            for _, _, line in remaining_rows
            if (not _has_intent(line, key) or _scope(line) is None)
            and (match := _invocation(line)) is not None
        }
        related_runs = {
            _id(line, "runId")
            for _, _, line in remaining_rows
            if line.get("event") == "log.dropped"
        }
        if complete_scan and not keep_all_links:
            # The entire unread range was visible. Drop links it cannot use.
            needed = {_link_tag("invocation", match) for match in related_invocations}
            needed.update(_link_tag("run", run) for run in related_runs if run)
            links = {tag: refs for tag, refs in links.items() if tag in needed}
        summaries: dict[tuple[Any, ...], tuple[log.RowPosition, LogLine]] = {}
        for group, (position, line) in evidence.items():
            authority = (group[0], *group[3:])
            previous = summaries.get(authority)
            if previous is None or log.event_order(line) > log.event_order(previous[1]):
                summaries[authority] = (position, line)
            unread = position.day < frontier.day or (
                position.day == frontier.day
                and (
                    frontier.offsets is None
                    or position.offset < frontier.offsets[position.slot]
                )
            )
            if (match := _invocation(line)) and (
                keep_all_links
                or (not unread and (not complete_scan or match in related_invocations))
                or (not complete_scan and match in related_invocations)
            ):
                links.setdefault(_link_tag("invocation", match), set()).add(position)
            if (run_id := _id(line, "runId")) and (
                keep_all_links
                or (not unread and (not complete_scan or run_id in related_runs))
                or (not complete_scan and run_id in related_runs)
            ):
                links.setdefault(_link_tag("run", run_id), set()).add(position)
        _save_intent_cursor(
            report,
            [position for position, _ in summaries.values()],
            snapshot,
            links,
            root,
        )
    if report.get("truncated") and "nextBefore" not in report:
        report["more"] = "Inspect the retained files from sai logs path."
    return report


def run(
    identifier: str, *, before: str | None = None, root: Path | None = None
) -> LogLine:
    """Read a process run ID or one MCP call ID."""
    if TOKEN.fullmatch(identifier) is None:
        raise SaiError("Invalid log run or call ID.", Code.INVALID_REQUEST)

    cursor = _Cursor.parse(before)
    last_day = cursor.last_day
    # Process runs use the full UUID hex. MCP calls use a 12-character prefix.
    parent = NO_CALL_PARENT if PROCESS_RUN_ID.fullmatch(identifier) else cursor.parent

    def parent_key(run_id: str) -> str:
        return hashlib.sha256(f"{identifier}~{run_id}".encode()).hexdigest()

    def remember_call_run(line: LogLine) -> None:
        nonlocal parent
        # A newer call event can identify the parent of an earlier gap.
        # The event itself does not need a slot in this page's row budget.
        if (
            _id(line, "callId") == identifier
            and line.get("event") != "log.dropped"
            and (run_id := _id(line, "runId"))
            and TOKEN.fullmatch(run_id)
            and _timestamp(line)
        ):
            parent = parent_key(run_id)

    log.flush(1.0)
    if (
        not parent
        and cursor.position is None
        and last_day is not None
        and last_day < date.max
    ):
        following, _ = _prefix(
            root,
            last_day + timedelta(days=1),
            lambda row: _id(row, "callId") == identifier,
        )
        for _, line in following:
            remember_call_run(line)

    scan = _trail_scan()
    entries = []
    last = None
    for position, line in scan.rows(
        root,
        start=cursor.position,
        last_day=last_day,
        before=cursor.upper,
        observe=(lambda _, line: remember_call_run(line)) if not parent else None,
    ):
        if (ts := _timestamp(line)) is None:
            continue
        last = ts
        if cursor.replay is None and (
            line.get("event") == "log.dropped"
            or identifier in (_id(line, "runId"), _id(line, "callId"))
        ):
            entries.append((position, ts, line))

    report = _PageReport({"id": identifier, "events": [], "gaps": []})
    if cursor.replay is not None:
        if not parent and scan.resume is not None:
            return report.finish(scan, cursor, last, None, replay=cursor.replay)
        # Replay from the first withheld notice once attribution is settled.
        return report.finish(
            scan, cursor, last, cursor.replay, parent or NO_CALL_PARENT
        )

    displayed = None
    for position, ts, line in entries:
        if line.get("event") == "log.dropped":
            run_id = _id(line, "runId")
            if run_id != identifier:
                if not run_id or TOKEN.fullmatch(run_id) is None:
                    continue
                if not parent and scan.resume is not None:
                    # The scan can still find the call. Publish only entries
                    # before this notice, and keep its position for replay.
                    return report.finish(scan, cursor, last, None, replay=position)
                if (
                    not parent
                    or parent == NO_CALL_PARENT
                    or parent_key(run_id) != parent
                ):
                    continue
            field = "gaps"
        else:
            field = "events"
        if not report.add(field, line):
            return report.finish(scan, cursor, displayed, position, parent)
        displayed = ts
    return report.finish(scan, cursor, last, None, parent)


def _seconds(duration: str) -> int:
    match = re.fullmatch(r"([0-9]+)([smhdw])", duration)
    if match:
        try:
            amount = int(match[1])
            if amount and amount <= timedelta.max.days * 86400 // SECONDS[match[2]]:
                return amount * SECONDS[match[2]]
        except ValueError:
            pass
    raise SaiError(
        "Use a positive whole duration followed by s, m, h, d, or w, such as 24h.",
        Code.INVALID_REQUEST,
        exit_code=ExitCode.USAGE,
    )


def stats(
    since: str | None = None, *, before: str | None = None, root: Path | None = None
) -> LogLine:
    """Summarize the timed samples and coded failures within the scan budget."""
    cursor = _Cursor.parse(before)
    first_day = None
    cutoff = None
    if since is not None:
        seconds = _seconds(since)
        now = log.now()
        start = now - timedelta(
            seconds=min(seconds, (now.date().toordinal() - 1) * 86400)
        )
        first_day, cutoff = start.date(), log.stamp(start)

    log.flush(1.0)
    scan = log.PageScan()
    page = _PageReport({"since": cutoff, "gaps": []})
    durations: defaultdict[str, list[int]] = defaultdict(list)
    codes: Counter[str] = Counter()
    last = None
    stop = None
    with closing(
        scan.rows(
            root,
            start=cursor.position,
            first_day=first_day,
            last_day=cursor.last_day,
            since=cutoff,
            before=cursor.upper,
        )
    ) as rows:
        for position, line in rows:
            ts = _timestamp(line)
            if ts is None:
                continue
            event = line.get("event")
            if event == "log.dropped":
                if not page.add("gaps", line):
                    stop = position
                    break
            else:
                if (code := _id(line, "code")) and code in CODES:
                    codes[code] += 1
                if event == "keeper.request":
                    operation, duration = _id(line, "operation"), line.get("durationMs")
                    if (
                        operation is not None
                        and operation in OPERATIONS
                        and type(duration) is int
                        and duration >= 0
                    ):
                        durations[operation].append(duration)
            last = ts
    operations = {}
    for name, samples in sorted(durations.items()):
        samples.sort()
        count = len(samples)
        operations[name] = {
            "count": count,
            "p50Ms": samples[(count * 50 + 99) // 100 - 1],
            "p95Ms": samples[(count * 95 + 99) // 100 - 1],
        }
    report = page.finish(scan, cursor, last, stop)
    report["keeper"] = operations
    report["errors"] = dict(sorted(codes.items()))
    return report
