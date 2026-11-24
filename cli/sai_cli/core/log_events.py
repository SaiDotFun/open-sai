"""Registry for the JSONL events shared by the CLI, MCP server, and Rust port."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA_VERSION = 2
VOLATILE_FIELDS = ("ts", "pid", "runId", "callId", "durationMs", "version")

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
        {"intentKey": "string", "addr": "string", "endpoint": "string"},
        {
            "kind": "string",
            "lifecycleStep": "string",
            "execActor": "string",
            "errorCode": "string",
            "errorMsg": "string",
            "txHash": "string",
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
        {"tradeId": "string"},
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
        {"code": "string"},
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
        {"txHash": "string"},
    ),
    "intent.wait.start": Event(
        "core intent wait (lane A4)",
        ("info",),
        {"intentKey": "string"},
        {"addr": "string", "endpoint": "string", "timeoutMs": "integer"},
    ),
    "intent.wait.end": Event(
        "core intent wait (lane A4)",
        ("info", "warn", "error"),
        {"intentKey": "string", "status": "string", "durationMs": "integer"},
        {
            "addr": "string",
            "endpoint": "string",
            "txHash": "string",
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
            "code": "string",
            "error": "string",
            "httpStatus": "integer",
            "keeperErrors": "object[]",
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
        "Events inside a CLI command may carry `command`. Events inside an MCP call carry",
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
        "The field `txHash` on `intent.submitted` is the request transaction hash when",
        "Keeper includes it in the admission response. It may appear first on `intent.keeper`.",
        "The wait entries reserve fields for the intent wait implementation.",
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
