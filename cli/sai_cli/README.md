# Sai CLI

Install `sai` as the [root README](../../README.md) shows. From a checkout, run
`uv sync`, then `uv run sai --help`.
The CLI connects to mainnet Keeper by default.

## Settings

`$SAI_HOME/config.toml` (default `~/.sai/config.toml`) holds settings for both
the CLI and `sai mcp serve`. Each setting comes from the first of these that sets
it: a flag, an environment variable, the file, the mainnet default.

| Key                  | Flag             | Variable            | Default                 |
| -------------------- | ---------------- | ------------------- | ----------------------- |
| `keeper.endpoint`    | `--endpoint`     | `SAI_ENDPOINT`      | mainnet Keeper GraphQL  |
| `keeper.ws_endpoint` | `--ws-endpoint`  | `SAI_WS_ENDPOINT`   | derived from the above  |
| `lcd.endpoint`       | `--lcd-endpoint` | `SAI_LCD_ENDPOINT`  | `https://lcd.nibiru.fi` |
| `update.auto`        |                  | `SAI_UPDATE_AUTO`   | `true`                  |
| `logs.enabled`       |                  | `SAI_LOG`           | `true`                  |
| `logs.keep_days`     |                  | `SAI_LOG_KEEP_DAYS` | `14`                    |

```bash
sai config list                      # every key, its value, and its source
sai config get keeper.endpoint       # the exact value
sai config set keeper.endpoint URL
sai config unset keeper.endpoint
```

`config list --json` returns `{"path", "settings": {KEY: {"value",
"source", "env", "default"}}}`, where `source` is `flag`, `env`, `file`,
`default`, `derived`, or `invalid` (a `logs.*` value sai could not read,
which turns logging off). `list` drops credentials from URLs; `get` prints the
exact value. An unknown key or invalid value exits 2 and lists the valid keys.

The first `set`, or an `unset` that removes a value, writes the whole file
with every key commented out and explained, at mode 0600 in a 0700
directory. Unsetting a key that is already unset writes nothing. Later edits keep your
comments, and a lock file makes concurrent edits run one at a time. The
CLI reads a section written as an inline table, dotted keys, or only
subtables, but `set` and `unset` refuse to edit it (exit 2) until you rewrite
it as a `[section]` table. Unsetting a key that is already unset leaves the
file alone. Keys the CLI does not know produce a warning and are
ignored; a known section that is not a table is an error.

A `keeper.ws_endpoint` from a lower layer than `keeper.endpoint` is ignored,
and the CLI derives the WebSocket from the Keeper endpoint instead. So
`sai --endpoint URL` never pairs `URL` with the WebSocket of the Keeper in the
file. The per-command `--ws-endpoint` and `--lcd-endpoint` options still
work and override the setting for that command.

## Account Overview

```bash
sai account overview
sai account overview --as 0xYOUR_ADDR --json
```

The command uses the active account on the configured Keeper endpoint.
Pass `--as ADDR` to read another EVM account, including one you have never
connected. The MCP tool `account_overview` takes an optional `addr` and
returns the same fields as the CLI's JSON output.

- **Sai funds.** Backed deposits, credits, and unsettled Teller funds that
  are available to trade are separate USD amounts. An omitted balance is
  unknown. If Keeper returns no Sai funds rows, the section reports an error.
- **Positions.** Size is collateral in USD multiplied by leverage. USDC
  collateral is valued at par. Gross unrealized PnL is before fees and uses
  the contract's range of -100% to +900% of collateral. Legacy stNIBI PnL
  uses the collateral price at the same pinned block. The total requires
  agreement with the contract's live positions, a USD PnL value for every
  position, and no fill that Keeper reports as awaiting indexing. The
  positions section shows the membership check's block and age. The total
  is unavailable when that block is more than 60 seconds old at the check.
- **Risk.** The liquidation threshold includes borrowing fees from the
  contract. Distances compare the same-block oracle mark and trigger levels
  in the pair's quote token. A zero or negative distance means the price
  reached or crossed the trigger. For a non-USD quote, displayed USD prices
  use the oracle's quote-token conversion. If the contract threshold or
  matching trade state is unavailable, any Keeper liquidation figure is
  labeled as an estimate and has no distance. If only the oracle mark is
  unavailable or stale under the market's contract settings, the threshold
  can still be shown. A stale mark at or past the liquidation threshold
  carries a warning because a catalyst can liquidate at that price.
  A pending after-hours leverage adjustment withholds
  gross PnL and TP/SL distances and retains liquidation distance when the
  mark is usable.
- **Orders.** Pending limit and stop orders list their trigger prices.
  Queued market orders come from Keeper's `pending_exec` records. For a
  queued open, the trade ID is reserved. Collateral on a queued order, when
  present, is in base units.
- **Local blockers.** An unknown or pending intent blocks all trades for the account.
  An accepted intent for a position blocks requests on that position.
  An accepted open blocks another open by default until its outcome is settled.
- **Session.** The overview shows the expiry of a stored session for the
  selected account and warns in its last three days.

Each section's `fetchedAt` records when the client read it, in UTC. Source
timestamps and `sourceAgeSeconds` describe the data at fetch time. A
position's indexed trade record, contract threshold, and oracle mark carry
separate source times. Oracle metadata shows the publish block separately
from the LCD read block. The estimate from Keeper has an unknown source time.
The LCD supplies missing block times after its chain matches Keeper's. A
custom Keeper requires an explicit LCD for that chain. An independently
overridden WebSocket is labeled as a balance source on an unverified chain.
Local intent age starts at reservation. Session age starts at token issuance
when the stored token provides that claim.

A failed read produces a section `error` with a code and message. Other
sections still return their data. An eight-second LCD deadline keeps already
loaded rows when further chain reads stall. The overview flags a paginated
list when it cannot confirm the list on a second scan.

## Updates

```bash
sai update                # install the latest release
sai update --check        # report the installed and latest versions only
sai update --to 0.2.0     # install that release; the only way to go back
sai update --source       # build the release from Git instead of its wheel
```

`sai update` works in this order, so a release that cannot start never
replaces a working `sai`:

1. Download the release wheel and `SHA256SUMS` and check the hash.
2. Install the wheel into a throwaway environment, run its `sai --version`,
   and import the CLI and the MCP server there.
3. Copy the tool's environment beside it, to roll back to. As a spare, keep
   a verified wheel of the current version: the one in
   `$SAI_HOME/cache/wheels` if its hash still matches, or the current release
   downloaded and checked again. With neither, stop and leave the install
   alone.
4. Install the wheel with `uv tool install --force`.
5. Run the installed `sai --version` and the imports again. If that fails, or
   on Ctrl-C, swap the copy back, which needs no network and no package
   index. Only if that fails is the spare wheel reinstalled.

It exits 0 when sai is updated or already current and 1 on a failure. uv and
the checks run in a neutral directory with an isolated Python, so nothing is
read from the directory you are in.

Only a `uv tool install` of a release, or of this repository's Git source, is
updated. A checkout, a `uvx` run, a fork, or any other install gets a note and
exit code 3; `sai doctor` warns about those in its `install` check. sai never
moves to an older version by itself. `sai update` also stops with exit code 3,
before changing anything, when uv's current tool or executable directory is
not where this `sai` is installed, or when the `sai` on your `PATH` now
belongs to another install.

The update keeps the path to `~/.local/bin/sai` stable. After verifying the
install, it checks MCP registrations and can repair stale Codex and Cursor
launchers. For stale Claude Code registrations, it prints an explicit repair
command. Background updates include that command in the next update notice
for any stale user registration they find. A running `sai mcp serve` keeps
the old code until its client restarts. The `mcp_info` tool reports
`restartNeeded`.

With `update.auto` on, which is the default, sai also updates by itself. A
command run in a terminal starts `sai update --background` detached, at most
once a day, and does not wait for it. A later command prints one line on
stderr when the update finished or failed. With `update.auto` off, the
background run only checks, and the notice says a release is available.
Nothing is started or printed under `--json`, when `CI` is set, without a
terminal, or by `sai mcp`, `sai update`, `sai doctor`, `sai config`,
`sai logs`, and `sai skill`. So `sai config set update.auto false` never
starts an update, and a background run reads the setting again right before
it installs. On Windows the background run only notifies.

`$SAI_HOME/update.json` holds the last check and the last update result, and
`update.lock` guards it. `update-cache.lock` is held for a whole install, so
two tools that share one `SAI_HOME` do not prune each other's wheels; the
cache is pruned only after a new version is verified. `$SAI_HOME`, `cache`,
and `cache/wheels` must belong to you and not be writable by others; sai
tightens its own and refuses anyone else's. `sai doctor` reads it for its `version` check, which
warns when sai is behind or the last update failed. Two files sit beside the
tool's environment in uv's tool directory, shared by every `SAI_HOME` that
uses the same `sai`: `.sai-cli-update.lock`, held for a whole install, and
`.sai-cli-release.json`, which records the release, the wheel's SHA-256, and
a hash of the installed files. An install from a local wheel counts as a
release install only while that record matches what is installed.

These checks guard against a bad release, a failed network, two commands
racing, and mistakes such as uv pointed at another directory. They are not a
defense against someone who can already write to uv's tool directory or to
`$SAI_HOME`; that person can replace `sai` outright.

## Connecting

The CLI trades with a Keeper session token, the same HS256 JWT Sai Web keeps
in the browser. Keeper issues it for 30 days in production. It authorizes
every trade kind on the account's deposited funds, and Keeper cannot revoke it
yet: signing out of Sai Web clears only the browser copy, not the CLI's.

On Sai Web Mainnet, open the wallet menu and select **Connect Sai CLI / MCP**
beside **Send to phone**. The button appears for the selected, connected
wallet with an active trading session. Check the expiry, click **Copy session
token**, and run the `sai auth import` command shown in the dialog. The command
specifies the Mainnet endpoint.

- With a terminal, paste the token at the hidden prompt or press Enter
  without typing to read the clipboard.
- Without a terminal, as when an agent runs it, it reads the clipboard with the
  first command that works: `pbpaste`, `wl-paste`, `xclip`, or `powershell.exe
  Get-Clipboard` (WSL and Windows). If none works, as over SSH, it exits
  non-zero. Run the dialog's import command in your own terminal.
- It never reads the token from arguments, environment variables, or piped
  stdin. Like `auth login`, it prints `addr`, `expiresAt`, and `endpoint`.
- After storing the token, it reads the clipboard again and clears it only if
  it still holds the token. Anything copied in the meantime stays.

The dialog pins Mainnet for import only. Check the session and, if you want
Mainnet as your saved default, set the Keeper endpoint.

```bash
sai --endpoint https://sai-keeper.nibiru.fi/query auth status
sai config set keeper.endpoint https://sai-keeper.nibiru.fi/query
```

Later CLI commands and any MCP server using the session must target the same
Keeper endpoint. An explicit `--endpoint` or `SAI_ENDPOINT` overrides the
saved setting. Update any conflicting overrides and restart running MCP
servers to pick up the change.

`auth login` signs Keeper's challenge with a local key. Before anything
signs, the CLI checks that the typed data has primary type
`UserTradingSession`, domain `Sai` version `1`, your addr, and the issued
challenge nonce.

Run `sai auth renew [ADDR]` to get a fresh session using the key saved by
`sai auth login NAME`. With no address, it renews the active account. An
explicit address renews that account and keeps the active selection. If a
keystore session predates the saved key name, run `sai auth login NAME` once.
For a session copied from Sai Web, copy a fresh token and run `sai auth
import`.

Status, list, doctor, the overview, and the MCP tool `mcp_info` report a
warning when a session has less than three days left. Interactive commands
can also print a one-line notice to stderr. JSON output carries the warning
in the reported session data. Use the Keeper endpoint named in the renewal
or import hint. If the hint asks you to reuse your settings, keep the original
Keeper configuration when running the command.

`auth login NAME` picks the key named `NAME`:

- If the `ud` tool is on `PATH` and `ud evm wallet show NAME` reports a
  wallet whose Cast keystore is `matching`, the CLI signs with `ud evm
  wallet run NAME -- wallet sign --data --from-file PATH`, which needs no
  prompt. ud holds the password for the keystore of the same name. If ud
  fails to sign, for example because the keystore has another password, the
  CLI signs with `cast` instead when run in a terminal, and `cast` asks for
  the password.
- Otherwise it uses the Foundry keystore `~/.foundry/keystores/NAME`. It
  reads the addr from the keystore file (or `cast wallet address --account
  NAME` if the file has none) and runs `cast wallet sign --account NAME
  --data --from-file PATH` on your terminal, where `cast` prompts for the
  keystore password.
- If ud and the keystore hold different accounts under `NAME`, it stops
  before signing and names both. `--addr` with the keystore's addr signs in
  as that account. ud cannot sign with its own until the keystore is renamed
  and `ud evm wallet sync NAME` has run.
- If `ud` is installed but its lookup fails, the CLI cannot tell whether ud
  holds another account under `NAME`, so it stops. `--addr` with the
  keystore's addr signs in as that account.
- With neither, it stops and names where it looked.

`--addr ADDR` is optional. The CLI refuses to sign when ud or the keystore
reports a different addr. For a keystore it reads the file's plaintext
`address` field, which needs no password, and falls back to `cast wallet
address` only when the file has none. That field is not bound to the
encrypted key: if someone edited it, the check passes, Keeper rejects the
signature, and nothing is stored.

### Several Accounts

Each `auth login` or `auth import` connects an account and makes it active.
Signing in with another account keeps the ones already connected; signing in
again with the same addr refreshes its session.

- `auth list` shows each connected account, its expiry, and which is active.
- `auth use ADDR` makes another connected account active.
- `--as ADDR`, before the command, runs one command as another connected
  account: `sai --as 0x... trade close 3`.
- `auth logout [ADDR]` removes one account, by default the active one.
  The others stay connected. After removing the active account, none is
  active until `auth use`.

`auth logout` makes no network call, and since Keeper cannot revoke tokens
yet, a copy of the token elsewhere, such as in Sai Web, stays valid until it
expires.

Each session is bound to its account and Keeper endpoint and expires with
the token. The CLI writes `$SAI_HOME/trader/sessions.json` with mode 0600,
encrypted by AES-GCM with a fixed key built into the CLI, like the Cosmos SDK
`test` keyring. Nothing prompts, so agents can run authenticated commands.
This only keeps the token out of plaintext; anyone who can read the file and
this source can decrypt it. Older versions could encrypt the session with a
passphrase. The CLI refuses those files and asks you to run `sai auth import`
or `sai auth login NAME` again. A single-account `session.json` from before
multiple accounts still loads and moves into `sessions.json` at the next
sign-in or switch.


## MCP Server

`sai mcp serve` runs the stdio MCP server for agent clients. Register it with
`sai mcp install claude`, `codex`, or `cursor`, or `--all` for every detected
client; `sai mcp list` and `sai mcp uninstall` show and remove registrations.
A bare `sai mcp` prints help. The
server writes only MCP messages to stdout and logs to stderr; run on a
terminal, it prints one stderr line saying an agent client should launch it.
`--endpoint`, `--ws-endpoint`, and `--lcd-endpoint` pick the network as the
top layer of [Settings](#settings), falling back to the same `sai` global
flags, then `SAI_ENDPOINT`, `SAI_WS_ENDPOINT`, and `SAI_LCD_ENDPOINT`, then
the config file, then mainnet. They resolve once, at start.

`install` registers the unresolved `sai` on `PATH` (normally the
`~/.local/bin/sai` link that survives updates) as `<sai> mcp serve`, with no
environment except `SAI_HOME` when it is set. It refuses a `sai` inside the uv
cache with code `UNSTABLE_PATH`, and warns when the `sai` on `PATH` is not the
install that is running. A server command after `--` replaces the default, for
a dev checkout. Claude Code and Codex change through `claude mcp add|remove`
and `codex mcp add|remove`; Cursor through an atomic edit of
`~/.cursor/mcp.json` that keeps other servers and the file mode. Reads take
only the `sai` entry's command, args, `SAI_HOME`, and the names of any other
environment settings, and never run `claude mcp get`, which starts the
server. The only environment value output shows is `SAI_HOME`, a path; other
settings appear by name in `staleEnv`, and an entry with any of them, or a
disabled Codex entry, is not current and gets replaced. `list` shows every
scope's entry under `registrations` and marks the one the client uses as
`effective` (Claude Code: local, then project, then user; Cursor: project,
then user). `uninstall` removes every scope's entry unless `--scope` names
one. Output shows a registered command in full only when it is the one
`install` would register; any other entry shows only its program name and
`args: "<hidden>"`, since its args may hold a token or credential URL. A
trusted Codex project's `.codex/config.toml` can define its own `sai` entry
that overrides the user's. Codex decides which project configs load, so sai
runs `codex mcp get sai --json` twice, from `$CODEX_HOME` for the user entry
and from the working directory for the entry Codex uses there. When they
differ (compared by a hash of everything Codex reports for the server, env values,
URL, and tool filters included), `list`
reports a `project` entry as effective, and `install` and `uninstall` fail
for Codex rather than edit project files. After each change sai reads the
client again: the entry must match, or be gone, and for Codex the answer from
the working directory must agree too. Otherwise the client fails with "a
project .codex/config.toml entry is still active", which catches a project
entry identical to the user one. An entry with a Codex tool filter
(`enabled_tools` or `disabled_tools`) lists it under `extraSettings` and is not
current. `reconnectNeeded` is true whenever a client config was written, even
if the check afterwards failed. The Cursor edit re-reads the file just before the rename
and merges again once if something else changed it. A plain file has no
compare-and-swap, so a write in the instant between that re-read and the
rename can still be lost. Results report `added`, `replaced`, `unchanged`, `removed`,
`absent`, `failed`, or `planned` (with `--dry-run`) per client, and the
command exits 1 if any client failed. Without a terminal, `install` with no
client lists clients and changes nothing, and `uninstall` with no client is a
usage error.

The server lists every tool regardless of sign-in. Each `trade_*` call checks
the session of the account it acts as (the active one, or its `addr`)
before it rebuilds the preview: no session returns
`SIGNED_OUT`, an expired one `SESSION_EXPIRED`, and one for another endpoint
or an unreadable file `SESSION_UNAVAILABLE`. Each message tells the agent to
have the user run `sai auth login NAME` or `sai auth import`. A signed-in trade
still needs the `previewDigest` of the preview the user confirmed.

The partial-close digest covers the close percentage, starting collateral and
leverage, and the contract inputs used to quote the requested slice. The
margin digest covers the full quote, including starting collateral, stored
leverage, and the inputs that determine effective leverage. Before submitting,
both tools re-read the position and the effective-leverage inputs, including
the applicable after-hours cap, at the same block. Submission requires an LCD
that reports the query height and serves height-pinned queries. If a trade call
finds a change, it returns `PREVIEW_MISMATCH` and includes the latest preview
when it can still quote the position. The direct leverage tool confirms the
requested target leverage.

## Commands

Commands include `market list`, `market prices`, `account positions ADDR`,
`account balances ADDR`, `account history ADDR`, `trade open`, `trade
close`, `trade partial-close`, `trade tpsl`, `intent wait KEY`,
and live `watch` streams. Run any command with `--help` for details. Trading
requires a funded account; funding is performed separately. Trade commands
support `--dry-run` to inspect the local semantic request.
For a live market order, the CLI refreshes the price before admission and
reports that reference price after acceptance. A dry run marks the price as
requiring refresh and may include an optional supplied reference. A partial
close sizes the collateral reduction from the Perp contract's position.
It quotes the requested slice in fields `quote.closedNotionalUsdc` and
`quote.closedNotionalUsd` using the effective leverage, collateral/USD
oracle price, and Perp's rounding. A missing sizing input sets
`quote.closedNotionalIsEstimate` to true. If the collateral price is
unavailable, the USD field is null.
The CLI rechecks the position before submission. Partial close accepts
`--lcd-endpoint` like `trade margin`. Market schedule and other Keeper
admission checks remain authoritative.

`trade leverage TRADE_ID --leverage VALUE` sets a target leverage.
`trade margin TRADE_ID --direction add|remove --amount USDC` reads the
position, exchange balance, and applicable after-hours cap from Perp. It uses
the effective leverage to quote a target and refuses if it cannot verify
the direction. When the after-hours guard is pending,
`quote.marginChangeIsEstimate` is true because the guard can deduct fees
before the leverage change. Keeper submits the target leverage. Perp uses
effective leverage at execution to determine the direction and amount.
`--dry-run` reads live state and prints the quote without submitting an
intent. Use `--lcd-endpoint` to select the matching network's
Nibiru REST service; the CLI checks that its chain matches Keeper.
Margin changes and partial closes briefly re-read Keeper and the contract if
their position data disagree. If the mismatch persists, the command stops
before submitting and asks you to retry after Keeper synchronizes.

The CLI persists non-secret request identities under `$SAI_HOME/trader` or
`~/.sai/trader`. A network error after submission leaves the outcome unknown,
and an unknown intent blocks new trades on the account. Reconcile it with
`sai intent wait KEY` or inspect the latest record with
`sai intent status ADDR --key KEY`. If Keeper has no record of the key five
minutes after submission, past Keeper's two-minute admission expiry, the CLI
marks it `absent` and trading resumes. A connection that fails before the
request is sent marks the intent `not_sent`, which does not block. Keeper
admission does not establish execution or finality.

### Wait for an Intent

```bash
sai --json intent wait INTENT_KEY --timeout 30
```

The command uses the account recorded for the key on the configured Keeper
endpoint, then the active account on that endpoint. Pass `--addr ADDR` to
look up an intent from another machine or account. Add `--wait` to a live
trade command to submit once and wait up to 30 seconds for that key. With
MCP, call `intent_wait` after the trade returns its key. The tool caps each
call at 10 seconds.

The result contains an `outcome` and a `receipt`. An unresolved timeout
returns `unknown` with code `OUTCOME_UNKNOWN` and exits 5 in the CLI. Call
wait again with the same key and account. If reconciliation releases the
local block after the admission grace period, the wait returns `absent`
and exits 1. A delayed admission can still appear.
If the ledger already has a final outcome and Keeper is unavailable, the
wait returns `terminal` and exits 1 with the execution details unavailable.

A confirmed failure exits 1, and an admission rejection exits 4. The
outcomes `filled`, `placed`, and `succeeded` exit 0. A limit or stop
order can return `placed` before its trigger. The receipt's `action`
identifies the intent kind and order type when available.
Keeper can confirm execution while the matching history is still unavailable.
In that case the wait returns `succeeded` for a market intent.
If matched history records liquidation or another effect that differs from
the request, the CLI exits 1. The receipt reports the observed
`execution.tradeChangeType` and the outcome is `liquidated` or `unexpected`.
An after-hours risk adjustment alongside the recorded action appears in
`execution.extraEffects`.

The receipt's `requestTxHash` identifies the transaction that submitted
the request. For a next-oracle fill, `execution.block` is the later block
that filled it. When history is available, `execution.historyId` identifies
the matching row. `positionNow` shows Keeper's latest state for that trade,
and `positionFeesUsd` gives cumulative fees. The fill can include an oracle
quote and the escrowed collateral in base units from the matching market
order. Actual fill price and size remain null when Keeper exposes only the
current position.

### Doctor

`sai doctor` checks the install, the version, config, MCP registrations,
`$SAI_HOME/trader`, `$SAI_HOME/logs`, the session, the intent ledger, and
the Keeper HTTP, WebSocket, and LCD endpoints. It creates
`$SAI_HOME/trader`, removes group and other access from it and the files sai
keeps there (the connected accounts, the ledger and its journals, and the
shell history) and from the logs directory and its log files, migrates the
intent ledger, and reconciles
unknown and pending intents for the signed-in account the way `sai intent status` does
(a Keeper read; it never places or cancels a trade). It never deletes or
rewrites the session, never writes through a symlink in `$SAI_HOME/trader`,
and never prints the session token. Without fixes it reads the ledger from a
temporary copy, so it writes nothing at all.

Doctor can repair stale Codex and Cursor launchers. It prints a repair command
for each stale Claude Code launcher, such as
`sai mcp install --client claude --scope user --repair-existing sai`.
Use the name and scope in the command doctor prints. Run project and local
repairs from the directory doctor checked. The command preserves the
registration's environment and compatible server arguments. If the command
includes `--reset-args`, it uses default server arguments.

Each check prints pass, fixed, warn, fail, or skipped, with what changed or
the command that fixes it. `sai --json doctor` prints a structured report on
success. If a check fails, doctor exits 1 and the JSON error message lists the
failed checks and their fixes. `--no-fix` reports without changing anything,
log lines included, and `--offline` skips the network checks, which
otherwise run in parallel and stop after 7 seconds in total. The
endpoints come from [Settings](#settings); `sai doctor --ws-endpoint` and
`--lcd-endpoint` override them for one run.

### Intent and History JSON

`intent status` returns Keeper intent rows in `userIntents` and any matching
trade history in `matchedTradeHistory`. Sai can add `outcome`,
`localStatus`, and `hint`. `account history` returns trade-history rows in
`perp.tradeHistory`. The CLI and MCP use the same Keeper fields.

| Rows | Keeper Fields |
| --- | --- |
| Intent | `intentKey`, `kind`, `lifecycleStep`, `execActor`, `evmAddress`, `nibiAddress`, `txHash`, `execBlock`, `execBlockTs`, `errorCode`, `errorMsg` |
| Trade history | `id`, `intentKey`, `tradeChangeType`, `txHash`, `evmTxHash`, `block.block`, `block.block_ts`, `trade.id`, `trade.trader` |

Sai omits null, unrecognized, and invalid row fields and lowercases valid
transaction hashes. A malformed hash or timestamp adds the corresponding
flag with value `true`. The flags are `txHashInvalid`,
`evmTxHashInvalid`, `execBlockTsInvalid`, and `block.block_tsInvalid`.

Use `outcome` and `localStatus` when present. If both are absent, read the
latest row's `lifecycleStep` and `execActor` together. A server `exec_pass`
confirms the intent succeeded. Matching trade history can arrive later.
[Keeper lifecycle and actor mapping](https://github.com/SaiDotFun/open-sai/blob/main/cli/sai_cli/core/intent-outcomes.md#keeper-intent-outcomes)

On an intent error, `errorCode` keeps recognized codes such as
`wallet_rejected` and `rpc_submission_failed`. Sai uses `KEEPER_ERROR`
when the code is missing or unrecognized. The `errorMsg` field says
`Keeper reported an intent error.`.

## Errors and Exit Codes

Put `--json` before the command to read errors on stdout. A failed command
writes one JSON object with `error.code`, `error.message`, and
`error.retryable`. The error includes `intentKey` when it refers to a
recorded intent and `hint` when a command can help. Failed doctor checks and
MCP registration commands include their structured reports in
`error.details`. The registration report includes `changed` per client and
the aggregate `reconnectNeeded` flag. If a stream such as `watch` has already
emitted JSON lines, the error object is its final line. Once `sai mcp serve`
starts the stdio transport, stdout carries MCP protocol traffic. Without
`--json`, error text goes to stderr.

| Exit | Meaning | What to Do |
| --- | --- | --- |
| 0 | Success | Read the result. |
| 1 | Other failure | Read `error.code` and the message before deciding what to do. |
| 2 | Command usage error | Check the command's `--help`. |
| 3 | Unsupported install or environment | Follow the error's installation or terminal instructions. |
| 4 | Request rejected or never sent | The request executed nothing. Review the error before retrying. |
| 5 | Outcome unknown | Keep the intent key and reconcile it. Do not resubmit. |
| 6 | Signed out or session expired | Sign in with `sai auth login NAME` or `sai auth import`. |

`ADMISSION_REJECTED` and `NOT_SENT` have exit 4 and `retryable: true`.
`OUTCOME_UNKNOWN` has exit 5 and `retryable: false`. The reconciliation
and sign-in hints pin the original Keeper endpoint. For URLs that may
contain credentials, including custom gateway paths, set
`SAI_RECONCILE_ENDPOINT` to the original URL before running the hint.
Exit 6 covers `SIGNED_OUT` and
`SESSION_EXPIRED`. `SESSION_UNAVAILABLE` has exit 1 and describes a local
session the command could not use, for example one for another Keeper
endpoint.

For Keeper withdrawal failures, `intent status` and MCP `intent_status`
report these values in `userIntents[].errorCode`.

| Keeper Intent Code | Meaning | What to Do |
| --- | --- | --- |
| `withdraw_evm_unconfigured` | Keeper lacks the configuration for EVM withdrawals. | Wait for Keeper to be configured before retrying. |
| `invalid_withdraw` | Keeper rejected the withdrawal request as invalid. | Review and correct the withdrawal request before retrying. |

The CLI, MCP tools, and failure logs share codes. MCP tool errors have
top-level `error` and `code` fields. The `command.end` event includes the
CLI process's numeric `exitCode`. The `mcp.tool_result` event carries the
symbolic `code` on failures. Each MCP call returns within the same process.

## Agent Skill

The CLI ships the `sai` agent skill as a directory: `SKILL.md`, which routes
to child pages `cli`, `mcp`, and `trading-safety`, and protocol references
under `references/`.

- `sai skill show` prints `SKILL.md`. `sai --skill` is the same command.
  `--page NAME` prints one child page; `NAME` is a path without `.md`, such
  as `references/contracts`, or a file name only one page has, such as
  `vaults`. Output is raw Markdown, under `--json` too.
- `sai skill source` prints the packaged path, the page names, the CLI and
  skill repositories, the jiyuu commit the skill was synced from, and the
  install commands. `sai --json skill source` prints the same as JSON.
- `sai skill install [--dir DIR] [--force]` copies the directory into
  `~/.agents/skills/sai`, or into `DIR` itself. It reports `installed`,
  `unchanged`, or `replaced`.

`install` deletes nothing without `--force`. It fills a `DIR` that is missing
or has no entries at all, and otherwise:

| `DIR` | Without `--force` | With `--force` |
| --- | --- | --- |
| Same files as the packaged skill | `unchanged` | `unchanged` |
| A symlink, which another tool manages | refused | The link is replaced by a copy. Its target is untouched. |
| A `sai` skill whose files differ | refused, naming the files | The directory is replaced. |
| A `sai` skill that holds a symlink or special file | refused | refused |
| Anything else, such as a folder of links to other skills | refused | refused |

A directory counts as a `sai` skill only when its own `SKILL.md` is a regular
file that starts with `name: sai`, so a mistyped `--dir` cannot delete other
files or links. `install` writes the new copy beside `DIR`, moves the old
directory or link aside, moves the new one into place, and only then deletes
the old one; if anything stops the move, Ctrl-C included, the old one is put
back. It refuses to replace
`DIR` while the current directory is inside it.

## Logs

The CLI and the MCP server append what they do to `$SAI_HOME/logs` (default
`~/.sai/logs`), one JSON object per line. Each surface has one file per UTC
day: `cli-YYYY-MM-DD.jsonl`, `mcp-YYYY-MM-DD.jsonl` for `sai mcp serve`, and
`update-YYYY-MM-DD.jsonl` for `sai update`. The directory has mode 0700 and
the files 0600.

```bash
sai logs path
sai logs tail -n 50 --surface mcp
sai logs tail --event intent
sai logs intent KEY
sai logs intent KEY --offline
sai logs run ID
sai logs stats --since 24h
sai --json logs intent KEY
```

The `intent` command joins a key's events for one EVM account and Keeper
endpoint to other events from the same CLI invocation or MCP call. A REPL
process gives each command its own `commandId`. The `run` command accepts
a process `runId` or a single MCP `callId`. Both order events by timestamp
and sequence. They report `log.dropped` notices separately because those
notices cover whole processes. An intent report also includes an
`unattributedGapCount` and up to eight `unattributedGaps` where a notice
cannot be tied to the key. Lost events can include the original match.

The `intent` command reads Keeper's current record and returns the request
transaction hash when available. The configured Keeper endpoint selects the
trail. The endpoint identity uses a local key to hash the full request URL,
including its path and query. The printed `endpoint` field shows the origin.
Use the root `--as` flag to select an account. The command's `--addr` flag
takes precedence when both are present. Use `--offline` to skip Keeper.
If the local trail has several endpoint
identities, pass a root `--endpoint` flag to select one offline. The report
warns when the scope is ambiguous or an older line lacks enough information
to assign it. The read leaves the local intent ledger untouched.

The read-only MCP tool `logs_intent` accepts `offline`, `addr`, and
`before`. It handles up to two log queries at a time and returns a busy
error when both slots are occupied. Log reports omit stored arguments and
free-text diagnostics. Keeper failures return a recognized code and a
short Sai message. Request hashes use lowercase hex and retain an existing
`0x` prefix. A malformed hash is omitted and sets `txHashInvalid` in the
corresponding record or event.

The `stats` command uses the retained scan window by default. Pass a positive
whole number followed by `s`, `m`, `h`, `d`, or `w` to limit the lookback.
The p50 and p95 use the nearest rank for each Keeper operation. Error totals
count each coded event, so a failure can count at more than one level.

Queries scan up to 128 files, 16 MiB, and 20,000 rows. Stats seek by UTC day
and skip out-of-window rows before charging the row budget. The scan
alternates surfaces within a day. Intent reports reserve part of the scan
budget for finding command starts and anchors across midnight. A report contains
up to 128 event or gap rows and uses a 96 KiB response budget. When
`truncated` is true, use `nextBefore` with `--before` or the MCP `before`
argument for earlier timestamps within the scan window. Use the files
listed by `sai logs path` when the scan limit prevents reaching older rows
or when events share the cutoff timestamp.

Use `sai --json logs ...` for machine-readable output. The `intent` and
`run` reports return arrays `events` and `gaps`. The intent report also
returns `intentKey`, `keeper`, and `txHash`. A failed Keeper read adds
`keeperError` alongside the local trail. The `stats` report maps operations
to `count`, `p50Ms`, and `p95Ms` under `keeper`, with counts by code under
`errors`. It also returns `gaps` and the UTC cutoff `since`, if set.
The `tail` command prints one JSON object per line.

A `keeper.request` has `status` `ok`, `error`, `rejected` (Keeper refused
admission), or `not_sent` (the connection failed first). Its `operation` is
`markets`, `prices`, `positions`, `history`, `intents`, `submit`, and so on.
To follow one trade, filter on its `intentKey`: `intent.reserved` (status
`unknown`), then `keeper.request` with operation `submit`, then
`intent.status` as it becomes `accepted`, `pending`, `rejected`, `not_sent`,
`terminal`, or `absent`, with `intent.keeper` lines for each time
`intent status` asked Keeper. An intent in `pending` blocks new trades for
the account. [Keeper intent outcome mapping](core/intent-outcomes.md)

See the [log event schema](../../docs/log-events.md) for event names and
field types. The `run` query and statistics also read older logs.

Sai logs allowlisted fields such as the account, intent key, and request
transaction hash. It omits session tokens, signatures, clipboard text, and
Keeper request and response payloads. The logger also scrubs every string.

- A URL anywhere, including in `argv` and `endpoint`, is cut to its scheme,
  host, and port, so an API key in a path or query is not written. In
  running text a URL runs to the next space; in a command-line token or a
  tool argument it runs to the end of the value.
- Session tokens this process has seen are replaced when the event is
  made, so signing out afterwards cannot leave one in a line that was
  still waiting to be written. The background writer rereads the session
  store and replaces its tokens again. Anything shaped like a JWT, and hex
  strings of 100 digits or more, such as a signature, are replaced too.
- For GraphQL errors, `keeper.request` records the total count and up to
  three validated codes. Unrecognized codes appear as `KEEPER_ERROR`.
- Other untrusted text from Keeper or a tool is cut to 300 characters.
  Sai removes hex strings of 64 digits or more and cuts the text at the
  first URL.
- `mcp.tool_call` logs only the parameters the tool defines.
  `unknownArguments` counts the rest, and a call to a tool the server does
  not have is logged as tool `unknown` with no arguments.
  The `logs_intent` tool omits its arguments from the log.
- Other strings are cut at 2000 characters, lists and objects at 64 items,
  and one event at 512 values and about 12,000 characters. A list or object
  passed where a tool takes a single value is logged as its type and length.

Known limit: if another process stores a session token and removes it again
before this process's writer reaches a line that quotes it, only the token's
shape can catch it. Sai session tokens are JWTs, which are always removed by
shape. A token of some other form in that window would be written.

Logging never slows a command or a trade. Events go on an in-memory queue of
at most 2048 events and 4 MB, and one background thread scrubs and writes
them, so a slow disk cannot delay an MCP reply or the step between reserving
an intent and submitting it. If the queue fills, sai drops events and later
writes one `log.dropped` line with the count. At exit sai waits up to 0.3
seconds for the queue to empty and drops the rest.

sai writes only inside a real `$SAI_HOME/logs` directory that you own. It
does not follow a symlink at that path, or on Windows a junction or other
reparse point, and it does not append to a symlink,
a FIFO, a file with other hard links, or a file someone else owns. It
removes group and other access from the directory and from a log file before
appending. A logs directory that cannot be written never fails a command;
the lines are lost. `sai doctor` reports a symlink or a file at the logs
path.

Files older than `logs.keep_days` days, counting today, are deleted once per
UTC day. A long-running MCP server checks again when the day changes. Only
files named `cli-`, `mcp-`, or `update-YYYY-MM-DD.jsonl` are deleted; other
files in the directory are left alone.

`sai config set logs.enabled false` or `SAI_LOG=0` turns logging off;
nothing is then written or created. A logs setting sai cannot read, such as
`SAI_LOG_KEEP_DAYS=two` or a `logs` entry that is not a table, also turns
logging off and prints a warning; it never stops a command or `sai mcp
serve`. `sai config list` shows its source
as `invalid`. `sai logs`, `sai doctor --no-fix`, `--help`, `--version`, and
`--skill` never write log lines.

Each line is appended with a single write, so concurrent `sai` processes and
MCP servers never interleave within a line. A line goes in the file for the
UTC day in its `ts`. `sai logs tail` reads each file from its end, so it
stays fast on a large file, and orders events by `ts` across files.

Run `sai` with no arguments for an interactive shell. `help` or `help COMMAND`
prints usage, Ctrl-C cancels the running command, and Ctrl-D or `exit` leaves.
In a terminal, the shell keeps command history in `$SAI_HOME/trader/history`.

`utils/repl_skin.py` is adapted from CLI-Anything
(https://github.com/HKUDS/CLI-Anything) under the Apache License 2.0. See the
repository license.
