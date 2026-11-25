# Open Sai

Open Sai is a public home for Sai tools and selected development work. The
first project is a trader CLI for Sai's market data and intent-based trading.

## Trader CLI

The `sai` command provides market and account queries, live data,
and trading intents. It uses Sai Keeper's mainnet endpoint by default. Fund the
Sai account through Sai Web before trading; depositing and bridging funds are
outside the CLI.

On Linux or macOS (x86-64 and ARM64), use Python 3.10 or later to run the
standalone installer. It verifies the release archive against `SHA256SUMS`,
checks that Sai and its MCP server start, and installs the launcher at
`~/.local/bin/sai`. Add `~/.local/bin` to your `PATH` to run the command. The
installed binary runs on its own. Until a native release is available, use the
pinned Python installer below.

```bash
curl -fsSL https://raw.githubusercontent.com/SaiDotFun/open-sai/main/scripts/install.py | python3 - --native
sai --help
```

If you installed Sai from a release wheel with [uv](https://docs.astral.sh/uv/),
find the full Python bridge version in the release notes. Install it explicitly
before running the native update. The bridge is published after the first
native release, whose version is higher, so an unpinned update can skip the
bridge.

```bash
sai update --to BRIDGE_VERSION
sai update
```

Replace `BRIDGE_VERSION` with the version in the release notes. The second
command selects the native release by its tag, verifies the matching shim,
and keeps your uv launcher.

On Windows, use the latest full Python release listed in the release notes.
Replace `PYTHON_VERSION` with that release version. The installer verifies the
wheel against `SHA256SUMS` and caches it under `$SAI_HOME/cache/wheels`.

```powershell
uv run https://raw.githubusercontent.com/SaiDotFun/open-sai/main/scripts/install.py --version PYTHON_VERSION
```

For maintainers, ship the next regular Python release, then publish the
native archives and matching shim. Cut the bridge afterward as the last full
Python release in the existing series. Choose its version at cut time and set
it in `pyproject.toml` for the ordinary Python release build. It must be newer
than the earlier full Python releases and lower than the first native
release. To rehearse a proposed version before bumping the package metadata,
run `uv run python scripts/build-bridge.py --version BRIDGE_VERSION --out-dir dist`.

If uv installed Sai from Git, run `uv tool uninstall sai-cli` and use the
pinned installer above with the published full-Python bridge version. Then
run `sai update`. The bridge keeps a verified Python wheel for offline
recovery. Your Sai profile stays in place when you replace the uv tool.

If the uv switch is interrupted, run the saved recovery binary with the same
uv settings:

```bash
"$(uv tool dir)/.sai-cli-recover" update --recover
```

To build from source instead, install from Git:

```bash
uv tool install git+https://github.com/SaiDotFun/open-sai
```

To try the CLI without installing it:

```bash
PYTHON_VERSION=X.Y.Z
uvx --from "https://github.com/SaiDotFun/open-sai/releases/download/sai-cli/v${PYTHON_VERSION}/sai_cli-${PYTHON_VERSION}-py3-none-any.whl" sai --json market prices
```

The Git install builds `main`. Run `sai --version` to check your installed version.

On supported installs, an interactive command can start a background update
once a day. Run `sai update` to update now or `sai update --check` to see
the installed and latest versions. To get notices without installing updates,
run `sai config set update.auto false`. Restart agent clients to load the new
version. See the [update guide](cli/sai_cli/README.md#updates).

The package is `sai-cli` and is not on PyPI yet, so `uv tool install sai`
installs an unrelated PyPI package.

Agents: run `sai skill show` (or `sai --skill`) to print the agent skill that
ships with the installed version, and `sai skill show --page NAME` for one of
its pages, such as `cli`, `mcp`, or `trading-safety`. To load the skill ahead
of time, copy it to where agents look for skills:

```bash
sai skill install                               # ~/.agents/skills/sai
sai skill install --dir ~/.claude/skills/sai    # another agent's directory
npx skills add SaiDotFun/open-sai --skill sai   # without the CLI installed
```

`sai skill install` requires `--force` to replace a symlink or a modified
copy of the skill. When replacing a symlink, it leaves the target intact.
`sai skill source` prints the packaged path and source commit.

To work from a checkout, run `uv sync`, then `uv run sai --help`.

For command examples, setup details, and recovery behavior, read the
[CLI guide](cli/sai_cli/README.md) and the
[agent skill](skills/sai/SKILL.md). A trade accepted by Keeper is
still pending until its outcome is observed. Use `--dry-run` to inspect a local
trade request without submitting it.

### Connect to Sai

Trading needs a Sai session. There are two ways to get one:

- **From Sai Web.** On Mainnet, open the wallet menu and select
  **Connect Sai CLI / MCP** beside **Send to phone**. The button appears for
  the selected, connected wallet with an active trading session. Check the
  session expiry shown in local time and click **Copy session token**. Run the
  `sai auth import` command shown there, which specifies the Mainnet endpoint.
  Paste the token at the hidden prompt or press Enter to read the clipboard.
  You can also tell your agent to run the command without a terminal so it
  reads the clipboard directly.
- **With a Foundry keystore.** Run `sai auth login NAME`,
  where `NAME` is a keystore in `~/.foundry/keystores`. The CLI fetches
  Keeper's sign-in challenge, checks that it is a Sai trading-session
  request for that account, and only then has the key sign it. Keeper
  returns a session that lets `sai` trade for the account. `cast wallet
  sign` asks for the keystore password, unless the `ud` tool is on your
  `PATH` with a wallet of the same name, which signs without a prompt.

The Sai Web dialog pins Mainnet for the import command only. Check the
imported session with `sai --endpoint https://sai-keeper.nibiru.fi/query auth status`.
Later CLI commands and any MCP server using the session must target the same
Keeper endpoint. To save Mainnet as your CLI default, run
`sai config set keeper.endpoint https://sai-keeper.nibiru.fi/query`.
Update any endpoint overrides and restart running MCP servers to pick up the change.

You can connect several accounts. Each sign-in adds an account and makes it
active. `sai auth list` shows them, `sai auth use ADDR` switches the active
one, `sai --as ADDR ...` runs one command as another, and `sai auth logout
[ADDR]` removes one.

Run `sai auth renew [ADDR]` to sign a fresh session with the key saved by
`sai auth login NAME`. Renewal keeps the active account selected. For a
browser session, copy a fresh Sai Web token for the same account. In v0.1.2,
run `sai auth import` with the original Keeper endpoint.
`sai auth status`, `sai auth list`, `sai doctor`, and the MCP tool
`mcp_info` warn when a session has less than three days left.

On `main`, run `renewalCommand` when the session report includes it. Browser
renewal uses `auth import --addr` to check the account. Read any
`renewalGuidance` separately. If the command references
`SAI_RECONCILE_ENDPOINT`, supply the original Keeper URL privately in the
same shell.
See [Renewing Sessions](cli/sai_cli/README.md#renewing-sessions).

After connecting, commands run without prompts until the session expires. Know
what the token is:

- It is the same session token Sai Web uses in your browser.
- It lasts as long as Keeper issues it: 30 days in production.
- Anyone with the token can trade with your Sai Balance and request
  one-click withdrawals of idle funds. Those withdrawals can only go to
  the wallet that owns the session. Keep the token out of clipboard sync,
  chats, and shared notes.
- It cannot be revoked yet. Signing out of Sai Web does not sign out the CLI,
  and `sai auth logout` only deletes the local copy.

The CLI stores sessions in `~/.sai/trader/sessions.json` with mode 0600,
encrypted with a key built into the CLI, like the Cosmos SDK `test` keyring.
That keeps the token out of plaintext files and grep, but anyone who can read
your files can decrypt it. There is no session passphrase.

### Read an Account Overview

Run `sai account overview` for your active account. To read a public EVM
address without a session, pass `--as ADDR`, for example
`sai --json account overview --as 0x0000000000000000000000000000000000000001`.
The MCP tool `account_overview` takes the same address as `addr`.

The overview shows Sai funds and credits, position risk, limit and stop
orders, market orders waiting for the next oracle price, local trade
blockers, and session expiry. Each section reports when it was fetched
and keeps available data when another read fails. Check the section's
`error` even when the command exits successfully. See the
[CLI guide](cli/sai_cli/README.md#account-overview) for units and source age.

## Use Sai From Your Agent (MCP)

`sai mcp serve` runs an MCP server that gives agent clients such as Claude
Code the same reads, previews, and trades as the CLI. Install `sai` on your
`PATH`, then register it:

```bash
sai mcp install claude          # or codex, cursor, or several at once
sai mcp install --all           # every client found on this machine
sai mcp list                    # where Sai is registered
sai mcp uninstall codex
```

`install` registers the `sai` on your `PATH`, normally `~/.local/bin/sai`,
which keeps its path across updates. It refuses a `sai` run through `uvx`,
because uv cleans that copy out of its cache. Ordinary Claude Code installs
use its MCP commands. Codex and Cursor installs edit the Sai entry in their
config files. Cursor may ask you to enable Sai in its MCP settings. Claude
Code installs use user scope by default. To register in the project's
`.mcp.json`, pass `--scope project`.

`sai doctor` and successful updates can repair stale Codex and Cursor
launchers. For Claude Code, they report stale entries and leave them for you
to repair. Run the command they print, such as
`sai mcp install --client claude --scope user --repair-existing sai`.
It edits the existing registration under its current name and scope.

Running `install` again reports `unchanged`. Install replaces a `sai` entry
that uses an older command, sets other environment variables, or is disabled
in Codex. Install reports a working legacy entry under its existing name and
scope. If the legacy entry needs repair, run `sai doctor`.

`uninstall` removes Sai from every scope it is registered in unless
you pass `--scope`. When Codex loads a different `sai` entry from the current project's
`.codex/config.toml`, sai reports it but never edits it; change it there
yourself. `--dry-run` shows the commands without running them. `install`
asks which clients to register only on a terminal; an agent that names no
client gets the list of detected clients and nothing changes. Restart or
reconnect a client after a change.

From a checkout, give the server command after `--`:

```bash
sai mcp install claude -- uv run --directory /path/to/open-sai sai mcp serve
```

The server lists every tool, trades included, whether or not you are signed in.
Trading follows sign-in on each call. Sign in with `sai auth login NAME` or `sai
auth import` in a terminal, and the agent can submit trades; sign out with `sai
auth logout`, and every `trade_*` call returns `SIGNED_OUT` without touching
Keeper. An expired session returns `SESSION_EXPIRED`. The server reads the
stored session without a prompt, never asks for the token, and never returns
it.

An MCP preview for a connected account names that account in `addr`.
While signed out, `preview_open` without an `addr` returns estimates with
`confirmationStatus: "informational"` and omits `previewDigest`. After
sign-in, preview again for the connected account before asking for confirmation.
Pass that preview's `previewDigest` as `preview_digest` in the trade call.
Before submitting, the server rebuilds the trade and checks the confirmed
terms listed below.
If the trade returns `PREVIEW_MISMATCH` or `PRICE_MOVED`, it has submitted
nothing. Use the returned `preview` for confirmation if it has a
`previewDigest`. Otherwise, check the reported cause and request a fresh
preview before asking for confirmation.

- Every trade digest binds the signed-in account, the trade's arguments,
  the verified chain ID, and the Perp contract address.
- `trade_margin` also binds the margin quote, including the position's
  starting collateral and leverage.
- `trade_partial_close` also binds the close percentage, starting collateral
  and leverage, and the contract inputs used to quote the requested slice,
  including the applicable after-hours cap and collateral/USD oracle price.
- `trade_open` for a market order also takes the preview's `referencePrice`
  as `reference_price`. A move beyond the allowed slippage requires a fresh
  preview and confirmation.

On `main`, Sai refuses trades on positions owned by a different account.
A preview with `positionOwnerVerified: false` has no confirmation digest.

Ask the agent to call `mcp_info` to see the version, endpoints, and active
account. Its `trading` field reports the active account's session readiness.
Each trade checks the account it targets. If that trade fails a session
check, use its error code to diagnose the requested account.

With several accounts connected, `auth_list` shows them, `auth_use` switches
the active one (for the CLI too), and preview and trade tools take an
optional `addr` to act as another connected account. A change of account
requires a new preview and confirmation. A trade also requires a valid
session for that account on this server's Keeper endpoint.

The server reads the same settings as the CLI once, when it starts: the
`--endpoint`, `--ws-endpoint`, and `--lcd-endpoint` flags after `sai mcp
serve`, then `SAI_ENDPOINT`, `SAI_WS_ENDPOINT`, and `SAI_LCD_ENDPOINT`, then
`~/.sai/config.toml`, which `sai config set` edits. See
[Settings](cli/sai_cli/README.md#settings).

## Logs

`sai` and `sai mcp serve` write JSON lines to `$SAI_HOME/logs`, which
defaults to `~/.sai/logs`. Logs record commands, MCP calls, Keeper requests,
and intent status changes. Sai redacts session tokens and sensitive values.

```bash
sai logs intent KEY              # follow one intent across CLI and MCP
sai logs run ID                   # read a process run or one MCP call
sai logs stats --since 24h        # Keeper latency and error counts
sai logs tail -n 50 --surface mcp
sai logs path
```

Sai keeps 14 UTC days of logs by default, counting today. `sai config set logs.enabled false`
turns logging off. See the [log query guide](cli/sai_cli/README.md#logs)
and the [event schema](docs/log-events.md).

## Development

Run `just install`, `just test`, and `just check`.

### The Agent Skill

The `sai` agent skill is edited in
[Unique-Divine/jiyuu](https://github.com/Unique-Divine/jiyuu/tree/main/ai-skills/sai)
and copied into `skills/sai/`. Do not edit `skills/sai/` here; the next sync
replaces the whole directory. To pull the latest:

```bash
just sync-skill                  # dry run: files that change, commit message
just sync-skill --run --commit   # write skills/sai and commit it
just sync-skill --from ~/jiyuu --ref my-branch --run   # a local checkout
```

The sync records the jiyuu commit in `skills/sai/.source.json`, and its commit
message lists the jiyuu commits pulled since the last committed sync. Running
`--run` first and `--run --commit` later commits the files already written. The package ships
the directory through the symlink `cli/sai_cli/skills`. `setup.py` walks it
and lists every file, hidden directories included, and `just build-check`
fails if the wheel or sdist misses a file of it or holds a link instead.

### Releases

Each component of this repository releases under its own tag prefix. The CLI
uses `sai-cli/vX.Y.Z`, where `X.Y.Z` is the version in `pyproject.toml`, and
`sai-cli/vX.Y.Z-rc.N` for a pre-release.

To release, merge the version bump and complete the
[upgrade and downgrade rehearsal](#rehearse-an-upgrade-and-downgrade), then
run `just release X.Y.Z` on `main`.
It is a dry run that checks the tree and prints the Git commands; add `--run`
to create and push the tag. The tag starts the
[Release workflow](.github/workflows/release.yml), which runs the tests,
builds the wheel and sdist, writes `SHA256SUMS`, and publishes all three as a
GitHub Release. The install script and `sai update` look for the highest
`sai-cli/v*` release that is not a pre-release.

### Rehearse an Upgrade and Downgrade

After the version bump lands on `main`, build the candidate and run the
rehearsal with `--strict` before tagging. Replace `X.Y.Z` with the version in
`pyproject.toml`. The release gate requires a Linux
host that can create a loopback-only network namespace with `unshare` and
`ip`. The script also needs Python 3.11 or later, `uv`, `claude`, and
`codex` on `PATH`, and network access to GitHub and the Python package
index. Use executables that can run with a fresh home directory.

```bash
uv build --wheel --out-dir dist
python3 scripts/release-rehearsal.py --strict dist/sai_cli-X.Y.Z-py3-none-any.whl
```

The script installs the latest published `sai-cli` wheel in a temporary home
and caches the candidate's dependencies. To rehearse an older published
baseline, pass `--from VERSION`. Run the script once per baseline. For the
rehearsal, it sets uv offline and pins the endpoints to loopback. The
`--strict` flag stops the run before installing anything if the namespace
is unavailable. For a local practice run, omit `--strict` to allow the
dead-proxy fallback, which only covers proxy-aware traffic. A synthetic
session and loopback Keeper leave an open unresolved. The script serves both wheels
and their checksums to `sai update`. It checks that the session, config,
and Claude and Codex MCP registrations work through the same launcher,
that the migrated ledger blocks a retry, and that `sai doctor` passes once
Keeper reports a terminal outcome.
It restores the pending ledger checkpoint before running
`sai update --to` with the baseline version and reports whether it blocks the open
before and after reconciling it.

The script stops its command process groups during teardown. On Linux, it
also adopts and stops orphaned helpers that started a separate session.

Run `python3 scripts/release-rehearsal.py --self-test` for the local script
checks. Add `--verbose` to a rehearsal to keep child output in a private
scratch log. The script prints the log path and removes the temporary home.
