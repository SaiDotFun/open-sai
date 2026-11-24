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
because uv cleans that copy out of its cache. Claude Code and Codex change
through their own `mcp add` and `mcp remove` commands; for Cursor, `install`
edits `~/.cursor/mcp.json` and keeps your other servers. Cursor may ask you to
enable `sai` in its MCP settings. Claude Code registers for your user by
default; `--scope project` writes the project's `.mcp.json` instead.

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
- `trade_margin` and `trade_partial_close`: also the position state the
  preview quoted.
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
sai logs tail                    # the last 20 events
sai logs tail -n 50 --surface mcp
sai logs tail --event intent     # how each intent moved through its states
sai logs path                    # where the files are
```

Files older than 14 days are deleted. `sai config set logs.enabled false`
turns logging off. See [Logs](cli/sai_cli/README.md#logs) for the events and
their fields.

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

To release, merge the version bump, then run `just release X.Y.Z` on `main`.
It is a dry run that checks the tree and prints the Git commands; add `--run`
to create and push the tag. The tag starts the
[Release workflow](.github/workflows/release.yml), which runs the tests,
builds the wheel and sdist, writes `SHA256SUMS`, and publishes all three as a
GitHub Release. The install script and `sai update` look for the highest
`sai-cli/v*` release that is not a pre-release.
