# Sai JSONL Log Events

Generated from `cli/sai_cli/core/log_events.py` by `just log-events`.
Writers emit `schema: 2`. Readers also accept older lines without `schema`.
The machine-readable registry is [log-events.json](log-events.json).

## Common Fields

| Presence | Fields |
| --- | --- |
| Required | `schema` (integer), `ts` (string), `level` (string), `event` (string), `surface` (string), `pid` (integer), `version` (string), `runId` (string), `seq` (integer) |
| Optional | `command` (string), `tool` (string), `callId` (string), `truncated` (string[]) |

`ts` is a UTC timestamp with millisecond precision. The integer `seq` orders events
within a process. The field `surface` is `cli`, `mcp`, or `update`.
Events inside a CLI command may carry `command`. Events inside an MCP call carry
`tool` and `callId`, except that a drop notice covers the process as a whole.
When a line exceeds the byte limit, its common required fields survive and
`truncated` lists the remaining field names.

## Parity

The following fields contain volatile values. Preserve field presence and type.
Map IDs consistently across events so a broken correlation still fails parity.

`ts`, `pid`, `runId`, `callId`, `durationMs`, `version`.

## Events

The logger omits fields whose value is null. A failure carries a Sai error
code in `code`. A Keeper lifecycle code, when present, goes in `errorCode`.
The field `txHash` on `intent.submitted` is the request transaction hash when
Keeper includes it in the admission response. It may appear first on `intent.keeper`.
The wait entries reserve fields for the intent wait implementation.

| Event | Emitter | Levels | Required | Optional |
| --- | --- | --- | --- | --- |
| `command.start` | `sai_cli.LoggedGroup.main` | `info` | `argv` (string[]) | None |
| `command.end` | `sai_cli.LoggedGroup.main` | `info`, `warn` | `exitCode` (integer), `durationMs` (integer) | `code` (string), `error` (string), `intentKey` (string) |
| `doctor.result` | `sai_cli.doctor_command` | `info`, `warn` | `overall` (string), `fix` (boolean), `offline` (boolean), `checks` (object[]) | `code` (string) |
| `intent.keeper` | `core.reconcile.reconcile` | `info` | `intentKey` (string), `addr` (string), `endpoint` (string) | `kind` (string), `lifecycleStep` (string), `execActor` (string), `errorCode` (string), `errorMsg` (string), `txHash` (string), `found` (boolean), `code` (string) |
| `intent.reserved` | `core.storage.pending` | `info` | `intentKey` (string), `kind` (string), `status` (string), `addr` (string), `endpoint` (string) | `tradeId` (string) |
| `intent.status` | `core.storage._log_status` | `info` | `intentKey` (string), `kind` (string), `previous` (string), `status` (string), `addr` (string), `endpoint` (string) | `code` (string) |
| `intent.submitted` | `core.intents.submit` | `info` | `intentKey` (string), `kind` (string), `addr` (string), `endpoint` (string) | `txHash` (string) |
| `intent.wait.start` | `core intent wait (lane A4)` | `info` | `intentKey` (string) | `addr` (string), `endpoint` (string), `timeoutMs` (integer) |
| `intent.wait.end` | `core intent wait (lane A4)` | `info`, `warn`, `error` | `intentKey` (string), `status` (string), `durationMs` (integer) | `addr` (string), `endpoint` (string), `txHash` (string), `fillBlock` (integer), `code` (string), `error` (string) |
| `keeper.request` | `core.keeper._log_request` | `info`, `warn` | `operation` (string), `endpoint` (string), `authenticated` (boolean), `durationMs` (integer), `status` (string) | `addr` (string), `code` (string), `error` (string), `httpStatus` (integer), `keeperErrors` (object[]) |
| `log.dropped` | `core.log._write` | `warn` | `count` (integer) | None |
| `mcp.server_start` | `mcp_server.server.serve` | `info` | `keeper` (string), `websocket` (string), `lcd` (string) | None |
| `mcp.tool_call` | `mcp_server.server.SaiMCP.call_tool` | `info` | `tool` (string), `callId` (string), `arguments` (object) | `unknownArguments` (integer) |
| `mcp.tool_result` | `mcp_server.server.SaiMCP.call_tool` | `info`, `warn`, `error` | `tool` (string), `callId` (string), `ok` (boolean), `durationMs` (integer) | `code` (string), `error` (string), `errorType` (string), `account` (string), `status` (string), `intentKey` (string), `previewDigest` (string) |
| `session.active` | `core.storage.use_session` | `info` | `addr` (string) | None |
| `session.expired` | `core.session.check_active` | `warn` | `expiresAt` (string), `code` (string) | `addr` (string) |
| `session.removed` | `core.storage.delete_session` | `info`, `warn` | None | `addr` (string), `connectedAccounts` (integer), `code` (string), `reason` (string) |
| `session.saved` | `core.storage.save_session` | `info` | `addr` (string), `expiresAt` (string), `connectedAccounts` (integer) | `endpoint` (string) |
| `update.check` | `sai_cli.check_update` | `info`, `warn` | `ok` (boolean) | `installed` (string), `latest` (string), `updateAvailable` (boolean), `installKind` (string), `code` (string), `error` (string) |
| `update.result` | `sai_cli.log_update` | `info`, `warn` | `status` (string), `rolledBack` (boolean), `background` (boolean) | `installed` (string), `target` (string), `message` (string), `code` (string) |
| `update.scheduled` | `core.updater.tick` | `info` | None | None |
| `watch.disconnected` | `sai_cli.watch_stream` | `warn` | `code` (string), `errorType` (string), `endpoint` (string) | None |
