# Sai Rust Port

The root Cargo workspace contains `sai-core/` for shared behavior and
`sai/` for the clap CLI and stdio MCP server. The Python package under
`cli/sai_cli/` remains the released implementation while Rust gains parity.
The Rust binary exposes `sai --version` and `sai config list|get|set|unset`.
Use `sai --json config list` to inspect settings and their sources. The CLI
writes command events to `$SAI_HOME/logs` when logging is enabled.

## Port Order

This push covers C0 through C3. C2 and C3 use Python tag `sai-cli/v0.1.1`
as their parity reference. C4 and later wait for `sai-cli/v0.1.2`, after
Python ships logging v2 and intent wait. [Port tracker #49](https://github.com/SaiDotFun/open-sai/issues/49).

| Lane | Acceptance criteria |
| --- | --- |
| C0 workspace | Build the CLI scaffold with Cargo and validate it in CI. |
| C1 parity runner | Launch both versions against the same fixtures and track every CLI command and MCP tool. |
| C2 settings, config, and event writer | Match flag, environment, file, and mainnet default precedence. Preserve TOML comments and mode. Write JSONL events so the runner can compare settings behavior. |
| C3 session store and intent ledger | Read v0.1 accounts and schema, preserve active account selection, and maintain compatible locks and pending rows across both implementations. |
| C4 Keeper and reconciliation | Match GraphQL requests, next-oracle outcomes, typed errors, and retry rules. |
| C5 reads and watch | Match decimal values and WebSocket snapshots against loopback fixtures. |
| C6 auth | Match challenge handling, wallet selection, and account checks. |
| C7 trading | Match previews, digests, submit guards, and wait results. A post-dispatch timeout leaves the intent blocking. |
| C8 MCP server | Match the Python tool inventory, schemas, annotations, error codes, and responses on stdio. |
| C9 doctor, logs, skill, and update | Compare local checks, log queries, bundled skill bytes, and install and update behavior. |
| C10 distribution | Ship verified native archives and compatible shim wheels after every command and MCP tool passes parity. |

The [parity runner](https://github.com/SaiDotFun/open-sai/issues/48) gives
each implementation a temporary `SAI_HOME` and replays the same HTTP and
WebSocket fixtures. It compares stdout JSON, stderr, exit codes, file
contents and permissions, SQLite schema and rows, and JSONL events.
Python fixes for [next-oracle fills](https://github.com/SaiDotFun/open-sai/issues/41),
[structured errors](https://github.com/SaiDotFun/open-sai/issues/42), and
[logging v2](https://github.com/SaiDotFun/open-sai/issues/43) define the
corresponding Rust acceptance cases. C2 writes events from the v0.1.1
contract. When Python ships the v2 schema, Rust adopts it and the runner
checks the C2 and C3 cases again.

Use clap derive for command dispatch, Tokio with reqwest and rustls for
HTTP, and tokio-tungstenite for Keeper streams. Use Serde for wire types,
`toml_edit` for config edits, AES-GCM for the existing session envelope,
rusqlite for the ledger, and the official Rust MCP SDK `rmcp` for stdio.
Keep schema v1 compatible with Python during the handoff. Evaluate
`evolve-sqlite` for a later explicit schema change. Emit documented events
through a dedicated JSONL writer so diagnostics stay off MCP stdout.

## Build and Release

Run `just rust-build` for a debug binary and `just rust-test` for workspace
tests. Rust CI runs formatting, Clippy, and tests. Use homebox for development
builds and a CI platform matrix to build and exercise release archives.

Native archives and compatible shim wheels ship on the existing
`sai-cli/v*` tags with SHA-256 checksums. Each shim version fetches and
verifies its exact native version in a private, versioned cache. The final
full Python updater installs the shim through uv and retains a rollback
for the existing uv-owned launcher. Fresh native installs use a standalone
launcher. The installer checks ownership and reports PATH shadowing.
Windows stays on Python until native support passes.

Updates and `sai doctor` keep valid MCP registrations and repair only
recognized stale Sai entries after the launcher passes its health checks.
[MCP registration repair #46](https://github.com/SaiDotFun/open-sai/issues/46).
