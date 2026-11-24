"""Read bounded, scrubbed reports from retained logs and Keeper."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import keeper, log, storage
from .errors import Code, ExitCode, SaiError
from .log_events import COMMON_OPTIONAL, COMMON_REQUIRED, EVENTS

LogLine = dict[str, Any]
Scope = tuple[str, str]
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")
ENDPOINT_ID = re.compile(r"[a-f0-9]{64}")
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
    return ts if ts and TIMESTAMP.fullmatch(ts) else None


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
        address = row.get("evmAddress")
        if isinstance(address, str) and address.lower() == addr.lower():
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
            or (identity and scope[1] != identity)
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


def _scan(
    root: Path | None, before: str | None = None
) -> tuple[list[LogLine], bool, str | None]:
    instant = _before(before)
    cutoff = log.stamp(instant) if instant else None
    scan = log.LogScan()
    log.flush(1.0)
    rows = sorted(
        (
            line
            for line in scan.rows(root, last_day=instant.date() if instant else None)
            if _timestamp(line)
        ),
        key=log.event_order,
    )
    return rows, scan.truncated, cutoff


def _in_window(line: LogLine, cutoff: str | None) -> bool:
    return cutoff is None or (ts := _id(line, "ts")) is not None and ts < cutoff


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
        if "previewDigest" in safe and not ENDPOINT_ID.fullmatch(
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
    tokens = log.known_tokens(check=True)
    bytes_left = MAX_REPORT_BYTES - 8192
    next_before = oldest_scanned
    for rows, (field, line) in enumerate(reversed(entries)):
        safe = _export(line, tokens)
        size = len(json.dumps(safe, allow_nan=False).encode()) + 2
        if rows == MAX_REPORT_ROWS or size > bytes_left:
            scan_truncated = True
            break
        report[field].append(safe)
        if field != "unattributedGaps":
            next_before = _timestamp(line)
        bytes_left -= size
    for field in {field for field, _line in entries}:
        report[field].reverse()
    if scan_truncated:
        report["truncated"] = True
        if next_before:
            report["nextBefore"] = next_before
        report["more"] = (
            "Use --before for earlier timestamps in the scan window. "
            "To read past the scan limit, inspect files from sai logs path."
        )
    return report


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
    identity = log.endpoint_id(endpoint) if endpoint else None
    rows, scan_truncated, cutoff = _scan(root, before)
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
    oldest = next((_id(line, "ts") for line in rows if _in_window(line, cutoff)), None)
    return _limit(report, entries, scan_truncated, oldest)


def run(
    identifier: str, *, before: str | None = None, root: Path | None = None
) -> LogLine:
    """Read a process run ID or one MCP call ID."""
    if TOKEN.fullmatch(identifier) is None:
        raise SaiError("Invalid log run or call ID.", Code.INVALID_REQUEST)
    rows, scan_truncated, cutoff = _scan(root, before)
    events = [
        line
        for line in rows
        if line.get("event") != "log.dropped"
        and identifier in (_id(line, "runId"), _id(line, "callId"))
    ]
    runs = {identifier} | {_id(line, "runId") for line in events} - {None}
    entries = [("events", line) for line in events if _in_window(line, cutoff)]
    entries.extend(
        ("gaps", line)
        for line in rows
        if line.get("event") == "log.dropped"
        and _id(line, "runId") in runs
        and _in_window(line, cutoff)
    )
    entries.sort(key=lambda entry: log.event_order(entry[1]))
    return _limit(
        {"id": identifier, "events": [], "gaps": []},
        entries,
        scan_truncated,
        next((_id(line, "ts") for line in rows if _in_window(line, cutoff)), None),
    )


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
    instant = _before(before)
    upper = log.stamp(instant) if instant else None
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
    scan = log.LogScan()
    durations: defaultdict[str, list[int]] = defaultdict(list)
    codes: Counter[str] = Counter()
    gaps: list[tuple[str, LogLine]] = []
    oldest = None
    for line in scan.rows(
        root, first_day=first_day, last_day=instant.date() if instant else None
    ):
        ts = _timestamp(line)
        if ts is None or (cutoff and ts < cutoff) or (upper and ts >= upper):
            continue
        oldest = min(oldest, ts) if oldest else ts
        if line.get("event") == "log.dropped":
            gaps.append(("gaps", line))
            continue
        if (code := _id(line, "code")) and code in CODES:
            codes[code] += 1
        if line.get("event") == "keeper.request":
            operation, duration = _id(line, "operation"), line.get("durationMs")
            if (
                operation
                and operation in OPERATIONS
                and type(duration) is int
                and duration >= 0
            ):
                durations[operation].append(duration)
    operations = {}
    for name, samples in sorted(durations.items()):
        samples.sort()
        count = len(samples)
        operations[name] = {
            "count": count,
            "p50Ms": samples[(count * 50 + 99) // 100 - 1],
            "p95Ms": samples[(count * 95 + 99) // 100 - 1],
        }
    gaps.sort(key=lambda entry: log.event_order(entry[1]))
    return _limit(
        {
            "since": cutoff,
            "keeper": operations,
            "errors": dict(sorted(codes.items())),
            "gaps": [],
        },
        gaps,
        scan.truncated,
        oldest,
    )
