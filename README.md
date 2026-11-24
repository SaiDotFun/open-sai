# Open Sai

Open Sai is a public home for Sai tools and selected development work. The
first project is a trader CLI for Sai's market data and intent-based trading.

## Trader CLI

The `sai` command provides market and account queries, live data,
and trading intents. It uses Sai Keeper's mainnet endpoint by default. Fund the
Sai account through Sai Web before trading; depositing and bridging funds are
outside the CLI.

Install [uv](https://docs.astral.sh/uv/), then install the newest release of
`sai` on your `PATH`:

```bash
uv run https://raw.githubusercontent.com/SaiDotFun/open-sai/main/scripts/install.py
sai --help
```

The [install script](scripts/install.py) finds the newest `sai-cli` release,
downloads its wheel, checks it against the release's `SHA256SUMS`, and runs
`uv tool install` on that file. It keeps the wheel in a cache only you can
write to, and does not defend against other local users who can write to the
directories above `~/.sai`. Until the first release is published, the script says so and
installs from `main`.

To build from source instead, install from Git:

```bash
uv tool install git+https://github.com/SaiDotFun/open-sai
```

To try the CLI without installing it:

```bash
uvx --from git+https://github.com/SaiDotFun/open-sai sai --json market prices
```

Both Git commands build `main`. `sai --version` prints the installed version.

`sai` keeps itself current. About once a day, a command you run in a terminal
starts an update in the background, and a later command tells you once that
it happened. `sai update` does the same right away, and `sai update --check`
only reports. The `sai` path and everything under `~/.sai` stay the same, so
registered agent clients keep working; restart them to load the new version.
To be told about updates without installing them, run
`sai config set update.auto false`. See
[Updates](cli/sai_cli/README.md#updates).

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

`sai skill install` refuses to replace a symlink or a copy that differs
unless you pass `--force`, and never replaces a directory that is not a `sai`
skill. `sai skill source` prints where the packaged skill
lives and which commit it came from.

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
browser session, copy a fresh Sai Web token and run `sai auth import`.
`sai auth status`, `sai auth list`, `sai doctor`, and the MCP tool
`mcp_info` warn when a session has less than three days left.

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

Run `sai account overview` for your active account or `sai account overview
--as ADDR --json` for any EVM account. The MCP tool `account_overview` takes
the same optional address as `addr`.

The overview shows Sai funds and credits, position risk, limit and stop
orders, market orders waiting for the next oracle price, local trade
blockers, and session expiry. Each section reports when it was fetched
and keeps available data when another read fails. See the
[CLI guide](cli/sai_cli/README.md#account-overview) for units and source age.

## Use Sai From Your Agent (MCP)

`sai mcp serve` runs an MCP server that gives agent clients such as Claude
Code the same reads, previews, and trades as the CLI. Install `sai` on your
`PATH` with `uv tool install` first, then register it:

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

Running `install` again reports `unchanged`. An older entry is `replaced`,
and so is one that sets other environment variables or that Codex has
disabled. `uninstall` removes Sai from every scope it is registered in unless
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

Each preview names the signed-in account in `addr`, and each trade must carry the
`previewDigest` from a preview the user confirmed. Before submitting, the
server rebuilds the trade and refuses it with a fresh preview when it no
longer matches:

- Every trade: the signed-in account and the trade's arguments.
- `trade_margin` also binds the margin quote, including the position's
  starting collateral and leverage.
- `trade_partial_close` also binds the close percentage, starting collateral
  and leverage, and the contract inputs used to quote the requested slice,
  including the applicable after-hours cap and collateral/USD oracle price.
- `trade_open` for a market order: also the market price, which may move at
  most the slippage from the confirmed `referencePrice`. A new price needs a
  new confirmation.
- `trade_close`, `trade_tpsl`, and `trade_leverage`: nothing beyond the
  account and arguments.

Ask the agent to call `mcp_info` to see the version, endpoints, active
account, and `trading`, which reports whether trades can submit and why not
(`signed-out`, `expired`, or `other-endpoint`).

With several accounts connected, `auth_list` shows them, `auth_use` switches
the active one (for the CLI too), and preview and trade tools take an
optional `addr` to act as another connected account. A preview names its
account, and a trade refuses if the account changed after the preview. A
trade for an account that is not signed in for this server's endpoint is
refused the same way.

The server reads the same settings as the CLI once, when it starts: the
`--endpoint`, `--ws-endpoint`, and `--lcd-endpoint` flags after `sai mcp
serve`, then `SAI_ENDPOINT`, `SAI_WS_ENDPOINT`, and `SAI_LCD_ENDPOINT`, then
`~/.sai/config.toml`, which `sai config set` edits. See
[Settings](cli/sai_cli/README.md#settings).

## Logs

`sai` and `sai mcp serve` record what they do in `~/.sai/logs`, one JSON
object per line: each command and MCP tool call with its arguments and
duration, each Keeper request and its outcome, and each status change of a
trade intent. The session token is never written.

```bash
sai logs intent KEY              # follow one intent across CLI and MCP
sai logs run ID                   # read a command run or one MCP call
sai logs stats --since 24h        # Keeper latency and error counts
sai logs tail -n 50 --surface mcp
sai logs path
```

Files older than 14 days are deleted. `sai config set logs.enabled false`
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
rehearsal with `--strict` before tagging. The release gate requires a Linux
host that can create a loopback-only network namespace with `unshare` and
`ip`. The script also needs Python 3.11 or later, `uv`, `claude`, and
`codex` on `PATH`, and network access to GitHub and the Python package
index. Use executables that can run with a fresh home directory.

```bash
uv build --wheel --out-dir dist
python3 scripts/release-rehearsal.py --strict dist/sai_cli-0.1.1-py3-none-any.whl
```

The script installs the published v0.1.0 wheel through the release installer
in a temporary home and caches the candidate's dependencies. For the
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
`sai update --to 0.1.0` and reports whether v0.1.0 still blocks the open
before and after reconciling it.

The script stops its command process groups during teardown. On Linux, it
also adopts and stops orphaned helpers that started a separate session.

Run `python3 scripts/release-rehearsal.py --self-test` for the local script
checks. Add `--verbose` to a rehearsal to keep child output in a private
scratch log. The script prints the log path and removes the temporary home.
