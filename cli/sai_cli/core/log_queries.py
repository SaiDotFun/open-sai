"""Read bounded, scrubbed reports from retained logs and Keeper."""

from __future__ import annotations

import hashlib
import json
import re
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
START_EVENTS = {"command.start", "mcp.tool_call"}


def _scan(
    root: Path | None,
    before: str | None,
    anchor: Callable[[LogLine], bool],
) -> tuple[list[LogLine], bool, str | None, str | None]:
    instant = _before(before)
    cutoff = log.stamp(instant) if instant else None
    page_day = instant.date() if instant else None
    scan = log.LogScan(
        max_bytes=log.MAX_SCAN_BYTES - 2 * CONTEXT_BYTES,
        max_rows=log.MAX_SCAN_ROWS - 2 * CONTEXT_ROWS,
        max_files=log.MAX_SCAN_FILES - 2 * CONTEXT_FILES,
    )

    log.flush(1.0)
    rows = [
        line
        for line in scan.rows(
            root,
            last_day=page_day,
            accept=lambda line: _in_window(line, cutoff) or anchor(line),
            frontier_before=cutoff,
        )
        if _timestamp(line)
    ]
    frontier = (
        scan.frontier
        if scan.truncated
        else min(
            (line["ts"] for line in rows if _in_window(line, cutoff)),
            default=None,
        )
    )
    truncated = scan.truncated
    if page_day is not None and page_day < date.max:
        # A start on this page can belong to an invocation first scoped
        # just after midnight. The next day's prefix supplies that anchor.
        following = log.LogScan(CONTEXT_BYTES, CONTEXT_ROWS, CONTEXT_FILES)
        rows.extend(
            following.rows(
                root,
                days={page_day + timedelta(days=1)},
                prefix=True,
                accept=anchor,
            )
        )
        scan.visited.update(following.visited)
        truncated |= following.truncated

    starts = {_invocation(line) for line in rows if line.get("event") in START_EVENTS}
    missing = {
        invocation: date.fromisoformat(ts[:10]) - timedelta(days=1)
        for line in rows
        if anchor(line)
        and (invocation := _invocation(line)) is not None
        and invocation not in starts
        and (ts := _timestamp(line))
        and ts[:10] != "0001-01-01"
    }
    if missing:
        # Reserve a bounded read near the end of the preceding day's files.
        # Skip files already scanned by either phase.
        previous = log.LogScan(CONTEXT_BYTES, CONTEXT_ROWS, CONTEXT_FILES)
        rows.extend(
            previous.rows(
                root,
                days=set(missing.values()),
                skip=scan.visited,
                accept=lambda line: (
                    line.get("event") in START_EVENTS and _invocation(line) in missing
                ),
            )
        )
        truncated |= previous.truncated
    return sorted(rows, key=log.event_order), truncated, cutoff, frontier


def _in_window(line: LogLine, cutoff: str | None) -> bool:
    return cutoff is None or (ts := _id(line, "ts")) is not None and ts < cutoff


@dataclass(frozen=True)
class _Cursor:
    upper: str | None
    position: log.ScanPosition | None = None
    timestamp: str | None = None
    parent: str = ""

    @classmethod
    def parse(cls, value: str | None) -> _Cursor:
        if value is None or "~" not in value:
            instant = _before(value)
            return cls(log.stamp(instant) if instant else None)
        try:
            if len(value) > 200:
                raise ValueError
            timestamp, day, parts, upper, parent = value.split("~")
            if (
                _timestamp({"ts": timestamp}) is None
                or (upper and _timestamp({"ts": upper}) is None)
                or (parent and SHA256_HEX.fullmatch(parent) is None)
            ):
                raise ValueError
            offsets = (
                None if parts == "-" else tuple(int(part) for part in parts.split(","))
            )
            if offsets is not None and (
                len(offsets) != len(log.SURFACES)
                or any(not 0 <= offset < 2**63 for offset in offsets)
            ):
                raise ValueError
            return cls(
                upper or None,
                log.ScanPosition(date.fromisoformat(day), offsets),
                timestamp,
                parent,
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

    def encode(self, position: log.ScanPosition, timestamp: str, parent: str) -> str:
        offsets = (
            "-" if position.offsets is None else ",".join(map(str, position.offsets))
        )
        return "~".join(
            (timestamp, position.day.isoformat(), offsets, self.upper or "", parent)
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
    if "endpoint" in safe:
        safe["endpoint"] = log.origin(str(safe["endpoint"]))
    tx_hash, invalid = keeper.request_hash(line.get("txHash"))
    safe.pop("txHash", None)
    if tx_hash:
        safe["txHash"] = tx_hash
    if invalid:
        safe["txHashInvalid"] = True
    return log.clean(safe, tokens)


def _limit(
    report: LogLine,
    entries: list[tuple[str, LogLine]],
    scan_truncated: bool,
    oldest_scanned: str | None,
) -> LogLine:
    page = _PageReport(report)
    last_returned = None
    output_limited = False
    for field, line in reversed(entries):
        if not page.add(field, line):
            output_limited = True
            break
        if field != "unattributedGaps":
            last_returned = _timestamp(line)
    for field in {field for field, _line in entries}:
        report[field].reverse()
    if scan_truncated or output_limited:
        report["truncated"] = True
        next_before = (
            (last_returned or oldest_scanned) if output_limited else oldest_scanned
        )
        if next_before:
            report["nextBefore"] = next_before
        report["more"] = (
            "Use --before for earlier timestamps in the scan window. "
            "To read past the scan limit, inspect files from sai logs path."
        )
    return report


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
                self.report["nextBefore"] = cursor.encode(position, stamp, parent)
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
    rows, scan_truncated, cutoff, frontier = _scan(
        root, before, lambda row: _has_intent(row, key)
    )
    identity = (log.endpoint_id(endpoint, root) or "") if endpoint else None
    anchors = [line for line in rows if _has_intent(line, key)]
    scope, ambiguous = _select_scope(anchors, chosen_addr, identity)
    events: list[LogLine] = []
    if scope is not None:
        direct = [line for line in anchors if _scope(line) == scope]
        invocations = {match for line in direct if (match := _invocation(line))}
        conflicting = {
            match
            for line in anchors
            if _scope(line) not in (None, scope)
            and (match := _invocation(line)) in invocations
        }
        for line in rows:
            if not _in_window(line, cutoff) or line.get("event") == "log.dropped":
                continue
            if _has_intent(line, key) and _scope(line) == scope:
                events.append(line)
                continue
            match = _invocation(line)
            if (
                match in invocations
                and match not in conflicting
                and line.get("intentKey") in (None, key)
                and _address(line) in (None, scope[0])
                and (line.get("endpoint") is None or line.get("endpointId") == scope[1])
                and line.get("endpointId") in (None, scope[1])
            ):
                events.append(line)
    else:
        direct = []

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
            for event in ("intent.submitted", "intent.wait.end", "intent.keeper")
            for line in reversed(direct)
            if line.get("event") == event
            and (tx_hash := keeper.request_hash(line.get("txHash"))[0])
        ),
        current.get("txHash"),
    )
    report["txHash"] = keeper.request_hash(log.clean(request_hash))[0]
    matched_runs = {_id(line, "runId") for line in direct} - {None}
    entries = [("events", line) for line in events]
    unattributed = []
    for line in rows:
        if _in_window(line, cutoff) and line.get("event") == "log.dropped":
            if _id(line, "runId") in matched_runs:
                entries.append(("gaps", line))
            else:
                unattributed.append(line)
    entries.sort(key=lambda entry: log.event_order(entry[1]))
    if unattributed:
        report["unattributedGapCount"] = len(unattributed)
        report["unattributedGaps"] = []
        entries = [("unattributedGaps", line) for line in unattributed[-8:]] + entries
        scan_truncated |= len(unattributed) > 8
    return _limit(report, entries, scan_truncated, frontier)


def run(
    identifier: str, *, before: str | None = None, root: Path | None = None
) -> LogLine:
    """Read a process run ID or one MCP call ID."""
    if TOKEN.fullmatch(identifier) is None:
        raise SaiError("Invalid log run or call ID.", Code.INVALID_REQUEST)

    cursor = _Cursor.parse(before)
    last_day = cursor.last_day
    parent = cursor.parent

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
    if cursor.position is None and last_day is not None and last_day < date.max:
        context = log.LogScan(CONTEXT_BYTES, CONTEXT_ROWS, CONTEXT_FILES)
        for line in context.rows(
            root,
            days={last_day + timedelta(days=1)},
            prefix=True,
            accept=lambda line: _id(line, "callId") == identifier,
        ):
            remember_call_run(line)

    scan = log.PageScan(
        max_bytes=log.MAX_SCAN_BYTES - 2 * CONTEXT_BYTES,
        max_rows=log.MAX_SCAN_ROWS - 2 * CONTEXT_ROWS,
        max_files=log.MAX_SCAN_FILES - 2 * CONTEXT_FILES,
    )
    entries = []
    last = None
    for position, line in scan.rows(
        root,
        start=cursor.position,
        last_day=last_day,
        before=cursor.upper,
        observe=remember_call_run,
    ):
        if (ts := _timestamp(line)) is None:
            continue
        last = ts
        if line.get("event") == "log.dropped" or identifier in (
            _id(line, "runId"),
            _id(line, "callId"),
        ):
            entries.append((position, ts, line))

    report = _PageReport({"id": identifier, "events": [], "gaps": []})
    displayed = None
    for position, ts, line in entries:
        if line.get("event") == "log.dropped":
            run_id = _id(line, "runId")
            if run_id != identifier and not (
                parent
                and run_id
                and TOKEN.fullmatch(run_id)
                and parent_key(run_id) == parent
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
