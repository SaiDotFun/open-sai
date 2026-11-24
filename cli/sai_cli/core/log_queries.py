"""Read bounded, scrubbed reports from retained logs and Keeper."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from collections import Counter, defaultdict
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import keeper, log, storage
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
    ends: tuple[int, ...] | None = None,
) -> tuple[list[tuple[log.RowPosition, LogLine]], bool]:
    return log.read_prefix(
        root, day, accept, max_bytes=CONTEXT_BYTES, max_rows=CONTEXT_ROWS, ends=ends
    )


def _trail_scan() -> log.PageScan:
    return log.PageScan(
        max_bytes=log.MAX_SCAN_BYTES - 2 * CONTEXT_BYTES,
        max_rows=log.MAX_SCAN_ROWS - 2 * CONTEXT_ROWS,
        max_files=log.MAX_SCAN_FILES - 2 * CONTEXT_FILES,
    )


NO_CALL_PARENT = "-"


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


INTENT_CURSOR_SUFFIX = "~i~"
MAX_INTENT_CURSOR = 96 * 1024
MAX_INTENT_STATE = 2 * 1024 * 1024


def _intent_cursor(
    value: str | None,
) -> tuple[_Cursor, list[log.RowPosition], log.FileEnds | None]:
    base, separator, payload = (value or "").partition(INTENT_CURSOR_SUFFIX)
    if not separator:
        return _Cursor.parse(value), [], None
    cursor = _Cursor.parse(base)
    try:
        if cursor.position is None or len(payload) > MAX_INTENT_CURSOR:
            raise ValueError
        inflater = zlib.decompressobj()
        raw = inflater.decompress(
            base64.b64decode(payload, altchars=b"-_", validate=True),
            MAX_INTENT_STATE + 1,
        )
        if not inflater.eof or inflater.unused_data or len(raw) > MAX_INTENT_STATE:
            raise ValueError
        items, days = json.loads(raw)
        if not isinstance(items, list) or not isinstance(days, list):
            raise ValueError
        positions = []
        for item in items:
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
            positions.append(log.RowPosition(date.fromordinal(day), slot, offset, size))
        ends = {}
        for row in days:
            if (
                not isinstance(row, list)
                or len(row) != len(log.SURFACES) + 1
                or any(
                    type(value) is not int or not 0 <= value < 2**63 for value in row
                )
            ):
                raise ValueError
            day, *offsets = row
            ends[date.fromordinal(day)] = tuple(offsets)
        return cursor, positions, ends
    except (TypeError, ValueError, OverflowError, RecursionError, zlib.error):
        raise SaiError(
            "Pass a log cursor or a UTC timestamp to --before.",
            Code.INVALID_REQUEST,
            exit_code=ExitCode.USAGE,
        ) from None


def _save_intent_cursor(
    report: LogLine, positions: list[log.RowPosition], ends: log.FileEnds
) -> None:
    if base := report.get("nextBefore"):
        raw = json.dumps(
            (
                [
                    (row.day.toordinal(), row.slot, row.offset, row.size)
                    for row in positions
                ],
                [(day.toordinal(), *ends[day]) for day in sorted(ends)],
            ),
            separators=(",", ":"),
        ).encode()
        payload = base64.urlsafe_b64encode(zlib.compress(raw)).decode()
        if len(raw) > MAX_INTENT_STATE or len(payload) > MAX_INTENT_CURSOR:
            del report["nextBefore"]
        else:
            report["nextBefore"] = base + INTENT_CURSOR_SUFFIX + payload


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
    cursor, positions, ends = _intent_cursor(before)
    log.flush(1.0)
    if ends is None:
        ends = log.snapshot_ends(root)
    evidence: dict[tuple[Any, ...], tuple[log.RowPosition, LogLine]] = {}

    def is_anchor(line: LogLine) -> bool:
        return _has_intent(line, key) and _timestamp(line) is not None

    def remember_anchor(position: log.RowPosition, line: LogLine) -> None:
        if is_anchor(line):
            group = _anchor_group(line)
            previous = evidence.get(group)
            if previous is None or log.event_order(line) > log.event_order(previous[1]):
                evidence[group] = (position, line)

    for position, line in log.read_rows_at(root, positions, ends):
        remember_anchor(position, line)

    scan = _trail_scan()
    rows = [
        (position, ts, line)
        for position, line in scan.rows(
            root,
            start=cursor.position,
            last_day=cursor.last_day,
            before=cursor.upper,
            ends=ends,
            observe=remember_anchor,
        )
        if (ts := _timestamp(line)) is not None
    ]
    boundary_days = {cursor.last_day, cursor.position.day if cursor.position else None}
    for day in sorted(
        day for day in boundary_days if day is not None and day < date.max
    ):
        following_day = day + timedelta(days=1)
        following, truncated = _prefix(
            root, following_day, is_anchor, ends.get(following_day, log.NO_FILES)
        )
        for position, line in following:
            remember_anchor(position, line)
        scan.truncated |= truncated
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
    page = _PageReport(report)
    last = None
    stop = None
    unattributed = []
    for position, ts, line in rows:
        if line.get("event") == "log.dropped":
            if _id(line, "runId") not in matched_runs:
                unattributed.append(line)
                continue
            field = "gaps"
        elif scope is not None and (
            (_has_intent(line, key) and _scope(line) == scope)
            or (
                (match := _invocation(line)) in invocations
                and match not in conflicting
                and line.get("intentKey") in (None, key)
                and _address(line) in (None, scope[0])
                and (line.get("endpoint") is None or line.get("endpointId") == scope[1])
                and line.get("endpointId") in (None, scope[1])
            )
        ):
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
    page.finish(scan, cursor, last, stop)
    _save_intent_cursor(report, [position for position, _ in evidence.values()], ends)
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
