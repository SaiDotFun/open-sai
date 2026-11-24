"""Registry for the JSONL events shared by the CLI, MCP server, and Rust port."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA_VERSION = 2
VOLATILE_FIELDS = (
    "ts",
    "pid",
    "runId",
    "callId",
    "durationMs",
    "version",
    "commandId",
    "endpointId",
)

COMMON_REQUIRED = {
    "schema": "integer",
    "ts": "string",
    "level": "string",
    "event": "string",
    "surface": "string",
    "pid": "integer",
    "version": "string",
    "runId": "string",
    "seq": "integer",
}
COMMON_OPTIONAL = {
    "command": "string",
    "tool": "string",
    "callId": "string",
    "commandId": "string",
    "truncated": "string[]",
}


@dataclass(frozen=True)
class Event:
    emitter: str
    levels: tuple[str, ...]
    required: dict[str, str] = field(default_factory=dict)
    optional: dict[str, str] = field(default_factory=dict)


EVENTS = {
    "command.start": Event("sai_cli.LoggedGroup.main", ("info",), {"argv": "string[]"}),
    "command.end": Event(
        "sai_cli.LoggedGroup.main",
        ("info", "warn"),
        {"exitCode": "integer", "durationMs": "integer"},
        {"code": "string", "error": "string", "intentKey": "string"},
    ),
    "doctor.result": Event(
        "sai_cli.doctor_command",
        ("info", "warn"),
        {
            "overall": "string",
            "fix": "boolean",
            "offline": "boolean",
            "checks": "object[]",
        },
        {"code": "string"},
    ),
    "intent.keeper": Event(
        "core.reconcile.reconcile",
        ("info",),
        {
            "intentKey": "string",
            "addr": "string",
            "endpoint": "string",
        },
        {
            "endpointId": "string",
            "kind": "string",
            "lifecycleStep": "string",
            "execActor": "string",
            "errorCode": "string",
            "txHash": "string",
            "txHashInvalid": "boolean",
            "found": "boolean",
            "code": "string",
        },
    ),
    "intent.reserved": Event(
        "core.storage.pending",
        ("info",),
        {
            "intentKey": "string",
            "kind": "string",
            "status": "string",
            "addr": "string",
            "endpoint": "string",
        },
        {"tradeId": "string", "endpointId": "string"},
    ),
    "intent.status": Event(
        "core.storage._log_status",
        ("info",),
        {
            "intentKey": "string",
            "kind": "string",
            "previous": "string",
            "status": "string",
            "addr": "string",
            "endpoint": "string",
        },
        {"code": "string", "endpointId": "string"},
    ),
    "intent.submitted": Event(
        "core.intents.submit",
        ("info",),
        {
            "intentKey": "string",
            "kind": "string",
            "addr": "string",
            "endpoint": "string",
        },
        {"txHash": "string", "txHashInvalid": "boolean", "endpointId": "string"},
    ),
    "intent.status_write_failed": Event(
        "core.intents._record_submission_status",
        ("warn",),
        {
            "intentKey": "string",
            "addr": "string",
            "endpoint": "string",
            "status": "string",
            "errorType": "string",
            "code": "string",
        },
        {"endpointId": "string"},
    ),
    "intent.wait.start": Event(
        "core.wait.for_intent_async",
        ("info",),
        {"intentKey": "string"},
        {
            "addr": "string",
            "endpoint": "string",
            "endpointId": "string",
            "timeoutMs": "integer",
        },
    ),
    "intent.wait.end": Event(
        "core.wait.for_intent_async",
        ("info", "warn", "error"),
        {"intentKey": "string", "status": "string", "durationMs": "integer"},
        {
            "addr": "string",
            "endpoint": "string",
            "endpointId": "string",
            "txHash": "string",
            "txHashInvalid": "boolean",
            "fillBlock": "integer",
            "code": "string",
            "error": "string",
        },
    ),
    "keeper.request": Event(
        "core.keeper._log_request",
        ("info", "warn"),
        {
            "operation": "string",
            "endpoint": "string",
            "authenticated": "boolean",
            "durationMs": "integer",
            "status": "string",
        },
        {
            "addr": "string",
            "intentKey": "string",
            "endpointId": "string",
            "code": "string",
            "error": "string",
            "httpStatus": "integer",
            "keeperErrors": "object[]",
            "keeperErrorCount": "integer",
        },
    ),
    "log.dropped": Event("core.log._write", ("warn",), {"count": "integer"}),
    "mcp.server_start": Event(
        "mcp_server.server.serve",
        ("info",),
        {"keeper": "string", "websocket": "string", "lcd": "string"},
    ),
    "mcp.tool_call": Event(
        "mcp_server.server.SaiMCP.call_tool",
        ("info",),
        {"tool": "string", "callId": "string", "arguments": "object"},
        {"unknownArguments": "integer"},
    ),
    "mcp.tool_result": Event(
        "mcp_server.server.SaiMCP.call_tool",
        ("info", "warn", "error"),
        {
            "tool": "string",
            "callId": "string",
            "ok": "boolean",
            "durationMs": "integer",
        },
        {
            "code": "string",
            "error": "string",
            "errorType": "string",
            "account": "string",
            "status": "string",
            "intentKey": "string",
            "previewDigest": "string",
        },
    ),
    "session.active": Event("core.storage.use_session", ("info",), {"addr": "string"}),
    "session.expired": Event(
        "core.session.check_active",
        ("warn",),
        {"expiresAt": "string", "code": "string"},
        {"addr": "string"},
    ),
    "session.removed": Event(
        "core.storage.delete_session",
        ("info", "warn"),
        optional={
            "addr": "string",
            "connectedAccounts": "integer",
            "code": "string",
            "reason": "string",
        },
    ),
    "session.saved": Event(
        "core.storage.save_session",
        ("info",),
        {"addr": "string", "expiresAt": "string", "connectedAccounts": "integer"},
        {"endpoint": "string"},
    ),
    "update.check": Event(
        "sai_cli.check_update",
        ("info", "warn"),
        {"ok": "boolean"},
        {
            "installed": "string",
            "latest": "string",
            "updateAvailable": "boolean",
            "installKind": "string",
            "code": "string",
            "error": "string",
        },
    ),
    "update.mcp_repair": Event(
        "core.updater.repair_after_update",
        ("info", "warn"),
        {"background": "boolean", "clients": "object[]", "notes": "string[]"},
    ),
    "update.result": Event(
        "sai_cli.log_update",
        ("info", "warn"),
        {"status": "string", "rolledBack": "boolean", "background": "boolean"},
        {
            "installed": "string",
            "target": "string",
            "message": "string",
            "code": "string",
        },
    ),
    "update.scheduled": Event("core.updater.tick", ("info",)),
    "watch.disconnected": Event(
        "sai_cli.watch_stream",
        ("warn",),
        {"code": "string", "errorType": "string", "endpoint": "string"},
    ),
}


def _matches_type(value: object, kind: str) -> bool:
    if kind.endswith("[]"):
        return isinstance(value, list) and all(
            _matches_type(item, kind[:-2]) for item in value
        )
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    return isinstance(value, dict) and kind == "object"


def valid_event(line: Mapping[str, object]) -> bool:
    """Check a registered event, including context and writer fields."""
    name = line.get("event")
    spec = EVENTS.get(name) if isinstance(name, str) else None
    if spec is None or line.get("level") not in spec.levels:
        return False
    required = COMMON_REQUIRED | ({} if "truncated" in line else spec.required)
    fields = COMMON_OPTIONAL | spec.optional | spec.required | required
    return required.keys() <= line.keys() and all(
        key in fields and _matches_type(value, fields[key])
        for key, value in line.items()
    )


def render_json() -> str:
    return (
        json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "volatileFields": VOLATILE_FIELDS,
                "common": {"required": COMMON_REQUIRED, "optional": COMMON_OPTIONAL},
                "events": {name: asdict(spec) for name, spec in EVENTS.items()},
            },
            indent=2,
        )
        + "\n"
    )


def _fields(fields: dict[str, str]) -> str:
    return ", ".join(f"`{name}` ({kind})" for name, kind in fields.items()) or "None"


def render_markdown() -> str:
    lines = [
        "# Sai JSONL Log Events",
        "",
        "Generated from `cli/sai_cli/core/log_events.py` by `just log-events`.",
        f"Writers emit `schema: {SCHEMA_VERSION}`. Readers also accept older lines without `schema`.",
        "The machine-readable registry is [log-events.json](log-events.json).",
        "",
        "## Common Fields",
        "",
        "| Presence | Fields |",
        "| --- | --- |",
        f"| Required | {_fields(COMMON_REQUIRED)} |",
        f"| Optional | {_fields(COMMON_OPTIONAL)} |",
        "",
        "`ts` is a UTC timestamp with millisecond precision. The integer `seq` orders events",
        "within a process. The field `surface` is `cli`, `mcp`, or `update`.",
        "CLI invocations carry a distinct `commandId` within the process `runId`.",
        "Events inside an MCP call carry",
        "`tool` and `callId`, except that a drop notice covers the process as a whole.",
        "When a line exceeds the byte limit, its common required fields survive and",
        "`truncated` lists the remaining field names.",
        "",
        "## Parity",
        "",
        "The following fields contain volatile values. Preserve field presence and type.",
        "Map IDs consistently across events so a broken correlation still fails parity.",
        "",
        ", ".join(f"`{name}`" for name in VOLATILE_FIELDS) + ".",
        "",
        "## Events",
        "",
        "The logger omits fields whose value is null. A failure carries a Sai error",
        "code in `code`. A Keeper lifecycle code, when present, goes in `errorCode`.",
        "MCP tool results carry the symbolic `code`. Numeric `exitCode` belongs to the CLI process.",
        "The field `endpointId` is a per-home keyed digest of the request URL, including path and",
        "query. The field `endpoint` shows the origin. Earlier unkeyed identities cannot establish",
        "an intent's endpoint scope.",
        "A request hash accepts 64 hexadecimal digits with an optional `0x` prefix.",
        "The writer converts the hash to lowercase and preserves the prefix if present.",
        "A malformed request hash is omitted and sets `txHashInvalid`.",
        "The field `txHash` on `intent.submitted` is the request transaction hash when",
        "Keeper includes it in the admission response. It may appear first on `intent.keeper`.",
        "When polling starts, `intent.wait.start` records the intent key and timeout.",
        "The `intent.wait.end` event records the outcome in `status`, the elapsed",
        "milliseconds in `durationMs`, and a Sai `code` when assigned.",
        "In v0.1.2 the wait writer does not populate `txHash`, `txHashInvalid`,",
        "`fillBlock`, or `error` on that event. Read the wait receipt for",
        "transaction and fill details.",
        "",
        "| Event | Emitter | Levels | Required | Optional |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, spec in EVENTS.items():
        levels = ", ".join(f"`{level}`" for level in spec.levels)
        lines.append(
            f"| `{name}` | `{spec.emitter}` | {levels} | {_fields(spec.required)} | "
            f"{_fields(spec.optional)} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    docs = Path("docs")
    docs.mkdir(exist_ok=True)
    (docs / "log-events.json").write_text(render_json(), encoding="utf-8", newline="\n")
    (docs / "log-events.md").write_text(
        render_markdown(), encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    main()
