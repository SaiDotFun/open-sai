"""Read and join retained JSONL events for the CLI and MCP server."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import keeper, log, storage
from .errors import Code, ExitCode, SaiError

LogLine = dict[str, Any]
KEEPER_FIELDS = (
    "intentKey",
    "kind",
    "lifecycleStep",
    "execActor",
    "evmAddress",
    "txHash",
    "execBlock",
    "execBlockTs",
    "errorCode",
    "errorMsg",
)
SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _id(line: LogLine, field: str) -> str | None:
    value = line.get(field)
    return value if isinstance(value, str) and value else None


def _lookup_intent(endpoint: str, key: str, addr: str) -> dict[str, Any] | None:
    data = keeper.request(endpoint, keeper.INTENTS, {"user": addr, "key": key})
    rows = data.get("userIntents")
    if not isinstance(rows, list):
        raise SaiError("Keeper did not return intent records", Code.INCOMPLETE_DATA)
    for row in rows:
        if not isinstance(row, dict) or row.get("intentKey") != key:
            continue
        address = row.get("evmAddress")
        if not isinstance(address, str) or address.lower() != addr.lower():
            continue
        record = {
            field: row[field]
            for field in KEEPER_FIELDS
            if field in row
            and (row[field] is None or isinstance(row[field], (str, int)))
        }
        for field in ("errorCode", "errorMsg"):
            if isinstance(record.get(field), str):
                record[field] = log.untrusted(record[field])
        return log.clean(record)
    return None


def _intent_address(anchors: list[LogLine], endpoint: str, addr: str | None) -> str:
    if addr is not None:
        if keeper.is_address(addr):
            return addr
        raise SaiError("Pass a 0x-prefixed EVM address", Code.INVALID_REQUEST)
    origin = log.origin(endpoint)
    for line in reversed(anchors):
        if line.get("endpoint", origin) != origin:
            continue
        account = _id(line, "addr") or _id(line, "account")
        if account is not None and keeper.is_address(account):
            return account
    try:
        active = storage.load_session()
    except (ValueError, OSError):
        active = {}
    account = active.get("evmAddress")
    if (
        account is not None
        and keeper.is_address(account)
        and active.get("endpoint") == endpoint
    ):
        return account
    raise SaiError(
        "No account found for the Keeper lookup. Pass an address or use offline mode.",
        Code.INVALID_REQUEST,
    )


def _has_intent(line: LogLine, key: str) -> bool:
    # An earlier audit of this intent does not add new evidence to its trail.
    return line.get("intentKey") == key and line.get("tool") != "logs_intent"


def intent(
    key: str,
    *,
    endpoint: str | None = None,
    addr: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Join direct intent events to CLI runs and individual MCP calls.

    With ENDPOINT, add a read-only Keeper lookup. A failed lookup leaves
    the trail available with a structured error.
    """
    log.flush(1.0)
    anchors = sorted(
        (line for line in log.read_events(root) if _has_intent(line, key)),
        key=log.event_order,
    )
    # MCP run IDs identify server processes. Require a matching call ID
    # for their sibling events. Drop notices carry only the run ID.
    calls: set[tuple[str | None, str]] = set()
    runs: set[str] = set()
    gap_runs: set[str] = set()
    for line in anchors:
        run_id, call_id = _id(line, "runId"), _id(line, "callId")
        if run_id:
            gap_runs.add(run_id)
        if call_id:
            calls.add((run_id, call_id))
        elif run_id and line.get("surface") != "mcp":
            runs.add(run_id)

    events = anchors.copy()
    gaps: list[LogLine] = []
    if anchors:
        for line in log.read_events(root):
            run_id, call_id = _id(line, "runId"), _id(line, "callId")
            if line.get("event") == "log.dropped":
                if run_id in gap_runs:
                    gaps.append(line)
            elif not _has_intent(line, key) and (
                run_id in runs or (run_id, call_id) in calls
            ):
                events.append(line)

    result: dict[str, Any] = {
        "intentKey": key,
        "events": sorted(events, key=log.event_order),
        "gaps": sorted(gaps, key=log.event_order),
        "keeper": None,
    }
    if endpoint is not None:
        try:
            result["keeper"] = _lookup_intent(
                endpoint, key, _intent_address(anchors, endpoint, addr)
            )
        except SaiError as exc:
            error = exc.as_dict()
            error["message"] = log.clean(log.untrusted(exc.message))
            result["keeperError"] = error
    current = result["keeper"] or {}
    logged_hash = next(
        (
            value
            for event in ("intent.submitted", "intent.wait.end", "intent.keeper")
            for line in reversed(anchors)
            if line.get("event") == event and (value := _id(line, "txHash"))
        ),
        None,
    )
    result["txHash"] = logged_hash or _id(current, "txHash")
    return result


def run(identifier: str, *, root: Path | None = None) -> dict[str, Any]:
    """Read a process run ID or one MCP call ID, including relevant gaps."""
    log.flush(1.0)
    events: list[LogLine] = []
    notices: list[LogLine] = []
    gap_runs = {identifier}
    for line in log.read_events(root):
        run_id, call_id = _id(line, "runId"), _id(line, "callId")
        if line.get("event") == "log.dropped":
            notices.append(line)
        elif identifier in (run_id, call_id):
            events.append(line)
            if run_id:
                gap_runs.add(run_id)
    return {
        "id": identifier,
        "events": sorted(events, key=log.event_order),
        "gaps": sorted(
            (line for line in notices if _id(line, "runId") in gap_runs),
            key=log.event_order,
        ),
    }


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


def stats(since: str | None = None, *, root: Path | None = None) -> dict[str, Any]:
    """Summarize recorded Keeper requests and count each event with a code."""
    first_day = None
    cutoff = None
    if since is not None:
        seconds = _seconds(since)
        now = log.now()
        # Clamp a very large lookback to the earliest representable date.
        start = now - timedelta(
            seconds=min(seconds, (now.date().toordinal() - 1) * 86400)
        )
        first_day = start.date()
        cutoff = log.stamp(start)

    log.flush(1.0)
    durations: defaultdict[str, list[int]] = defaultdict(list)
    codes: Counter[str] = Counter()
    gaps: list[LogLine] = []
    for line in log.read_events(root, first_day=first_day):
        ts = _id(line, "ts")
        if cutoff is not None and (ts is None or ts < cutoff):
            continue
        if line.get("event") == "log.dropped":
            gaps.append(line)
            continue
        if code := _id(line, "code"):
            codes[code] += 1
        if line.get("event") == "keeper.request":
            operation, duration = _id(line, "operation"), line.get("durationMs")
            if operation and type(duration) is int and duration >= 0:
                durations[operation].append(duration)
    operations = {}
    for name, samples in sorted(durations.items()):
        samples.sort()
        count = len(samples)
        # Nearest rank, with one-based ranks and integer ceiling division.
        operations[name] = {
            "count": count,
            "p50Ms": samples[(count * 50 + 99) // 100 - 1],
            "p95Ms": samples[(count * 95 + 99) // 100 - 1],
        }
    return {
        "since": cutoff,
        "keeper": operations,
        "errors": dict(sorted(codes.items())),
        "gaps": sorted(gaps, key=log.event_order),
    }
