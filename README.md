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
Python installer below.

```bash
curl -fsSL https://raw.githubusercontent.com/SaiDotFun/open-sai/main/scripts/install.py | python3 - --native
sai --help
```

On a host that can run native Sai, a [uv](https://docs.astral.sh/uv/) install
from the Sai 0.1.3 release wheel can update directly.

```bash
sai update
```

If you installed an earlier release wheel, first update to the published
full Python release `0.1.3`, then run the native update. The update verifies
the shim and keeps your uv launcher.

```bash
sai update --to 0.1.3
sai update
```

Use the managed Python installer on Windows or when you want the full Python
package on another platform. It verifies the release download against
`SHA256SUMS` and records the release for `sai update`. Add `--version X.Y.Z`
to pin a stable release. If an older Windows `sai update` asks you to keep
your current Python version, use this installer to update first.

```powershell
uv run https://raw.githubusercontent.com/SaiDotFun/open-sai/main/scripts/install.py --full-python
```

On a native release, the standard `sai-cli` wheel is the native shim. To
inspect the full Python packages, download `sai-cli-X.Y.Z-full-python.zip`
and `SHA256SUMS` from the release assets. Compute the ZIP's SHA-256 and
compare it with the ZIP's entry in `SHA256SUMS`.

- Linux: `sha256sum sai-cli-X.Y.Z-full-python.zip`
- macOS: `shasum -a 256 sai-cli-X.Y.Z-full-python.zip`
- Windows PowerShell: `Get-FileHash -Algorithm SHA256 sai-cli-X.Y.Z-full-python.zip`

After the hashes match, extract the ZIP to inspect the wheel and sdist.

```console
uv run --no-project python -m zipfile -e sai-cli-X.Y.Z-full-python.zip full-python
```

If uv installed Sai from Git and you want to move to the native release, first
switch to the published 0.1.3 wheel. The installer keeps a verified Python wheel
for offline recovery. Your Sai profile stays in place when you replace the uv
tool.

```bash
uv tool uninstall sai-cli
uv run https://raw.githubusercontent.com/SaiDotFun/open-sai/main/scripts/install.py --version 0.1.3
sai update
```

If the uv switch is interrupted, run the saved recovery binary with the same
uv settings:

```bash
"$(uv tool dir)/.sai-cli-recover" update --recover
```

To build from source instead, install from Git:

```bash
uv tool install git+https://github.com/SaiDotFun/open-sai
```

To try the CLI without installing it, pass the release wheel to `uvx`.
For full Python on a native release, use the wheel you extracted above.

```bash
VERSION=X.Y.Z
uvx --from "https://github.com/SaiDotFun/open-sai/releases/download/sai-cli/v${VERSION}/sai_cli-${VERSION}-py3-none-any.whl" sai --json market prices
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

For the default native release, merge a version bump that aligns the Python
package, Cargo workspace, and shim on the same release version. In the shim,
use `X.Y.ZrcN` for release candidates. Run the
[Release workflow](.github/workflows/release.yml) manually on the candidate
commit with the planned tag. The workflow checks the package versions, runs
the Python tests and the four native smoke jobs, and uploads the
`sai-release-candidate` Actions artifact. Complete the rehearsal below for
the planned tag before publishing.

Run `just release X.Y.Z` on `main` to check the tree and preview the Git
commands. Add `--run` to create and push the tag. The tag workflow checks that
the tagged commit is on main's first-parent history. It publishes four native
archives, the shim wheel and sdist, a ZIP containing the full Python wheel and
sdist, and one `SHA256SUMS` file in one immutable release. After publication,
it checks the tag and every file against GitHub's signed release attestation.
Sai does not sign the binaries separately.

Before tagging, enable immutable releases in the repository settings and set
the Actions secret `SAI_RELEASE_SETTINGS_TOKEN` to a fine-grained token scoped
to this repository with Administration read access. The workflow checks the
setting with that token immediately before publication and stops if GitHub
cannot confirm it is enabled. The usual Actions token publishes the release.

Native Linux builds and smoke tests use glibc 2.35 on x86_64 and arm64.
Native macOS builds target macOS 11 on Intel and Apple silicon. The hosted
smoke tests run on macOS 15, the tested support floor.

The shim keeps the `sai-cli` package name and takes the standard wheel and
sdist filenames on native releases. The Python bridge downloads the wheel
by that filename and keeps the uv-managed `sai` launcher. The full Python
build keeps both original filenames inside `sai-cli-X.Y.Z-full-python.zip`.
Use the [managed Python installer](#trader-cli) with `--full-python --version X.Y.Z`
to install that release and keep it eligible for updates.

### Release Python Only

Use this mode for a full Python release before the native distribution is
ready. Bump the Python package version and run the manual Release workflow
with the planned tag and the `python-only` option enabled. Its candidate
artifact contains the full Python wheel, sdist, and `SHA256SUMS`.

Preview the tag on `main` with `just release X.Y.Z --python-only`. Add
`--run` when the rehearsal is complete. The command records
`Sai-Release-Mode: python-only` in the annotated `sai-cli/vX.Y.Z` tag.
The workflow uses the Python version and omits the native build. The Python
files keep their usual download URLs and publish together in one immutable
release.

### Rehearse a Release Candidate

Use `sai-cli/vX.Y.Z-rc.N` as the planned tag for the manual run. After it
passes, download its candidate artifact and verify the checksums. On a
native candidate, extract the full Python packages into a separate directory
with `unzip sai-cli-X.Y.Z-rc.N-full-python.zip -d full-python`. Use
`./full-python/` in the wheel path below. The full Python wheel should
report `X.Y.ZrcN`.

```bash
sha256sum --check --strict SHA256SUMS
SAI_HOME="$(mktemp -d)" uv run --isolated --no-project \
  --with ./sai_cli-X.Y.ZrcN-py3-none-any.whl sai --version
```

On `main`, run `just release X.Y.Z-rc.N` to check the package versions and
preview the tag commands.

The Release workflow also stages both release modes on pull requests that
change the release inputs. The native candidate is `sai-release-candidate`.
The Python-only candidate is `sai-python-release-candidate`.

### Rehearse an Upgrade and Downgrade

For a stable release, build the candidate on `main` and run the rehearsal
with `--strict` before tagging. The rehearsal exercises stable release
discovery and takes a wheel with the `X.Y.Z` version from `pyproject.toml`.
The release gate requires a Linux host that can create a loopback-only
network namespace with `unshare` and
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

### Rehearse the Native Cutover and Rollback

Run this rehearsal manually on Linux before publishing a native candidate.
It uses real Claude Code, Codex, and Cursor installations. The hosted
Native Distribution jobs provision uv and Sai and run the automated archive
smoke tests against Python 0.1.1. They do not provision the three clients
needed for the populated 0.1.3 profile rehearsal.

Use Python 3.11 or later and put `uv`, `claude`, `codex`, and
`cursor-agent` on `PATH`. Codex must support `codex app-server`. The host
must support the loopback-only network namespace described above.

To reproduce the reported native 0.2.4 candidate, export its source revision
into a scratch directory and set the test version in its package metadata.
The commands below keep the version changes out of the checkout. Use the
revision under review when rehearsing a different candidate.

```bash
cutover="$PWD/dist/cutover"
native_src="$cutover/native-src"
native_ref=ccffb46ab2b5a7084cc6ce41cc6a1be6c81900b6
target="$(rustc -vV | sed -n 's/^host: //p')"
mkdir -p "$native_src"
git archive "$native_ref" | tar -x -C "$native_src"
python3 - "$native_src" <<'PYTHON'
from pathlib import Path
import sys

source = Path(sys.argv[1])
for filename in ("Cargo.toml", "shim/pyproject.toml"):
    path = source / filename
    text = path.read_text()
    old = 'version = "0.1.2"'
    assert text.count(old) == 1
    path.write_text(text.replace(old, 'version = "0.2.4"'))

lock = source / "Cargo.lock"
text = lock.read_text()
for package in ("sai", "sai-core"):
    old = f'name = "{package}"\nversion = "0.1.2"'
    assert text.count(old) == 1
    text = text.replace(old, f'name = "{package}"\nversion = "0.2.4"')
lock.write_text(text)
PYTHON
cargo build --locked --bin sai --manifest-path "$native_src/Cargo.toml" \
  --target-dir "$native_src/target"
strip --strip-debug "$native_src/target/debug/sai"
python3 "$native_src/scripts/native-dist.py" archive \
  "$native_src/target/debug/sai" "$cutover/native" --target "$target"
python3 "$native_src/scripts/native-dist.py" rehearsal-wheel "$cutover/native"
sha256sum "$cutover/native/sai-0.2.4-$target.tar.gz"
```

The loopback shim must match the archive's version. Record the source
revision and archive digest with the rehearsal output. Download the baseline
wheel and build the full Python bridge from the checkout under review.
Prepare uv's dependency cache as a separate step with network access.
Use the same Python interpreter and cache for the rehearsal.

```bash
gh release download sai-cli/v0.1.3 --repo SaiDotFun/open-sai \
  --pattern '*.whl' --pattern SHA256SUMS --dir "$cutover/published"
python3 scripts/build-bridge.py --version 0.1.4 --out-dir "$cutover/bridge"
export UV_CACHE_DIR="$cutover/uv-cache"
for wheel in "$cutover/published/"*.whl "$cutover/bridge/"*.whl; do
  uv --no-config pip install --python "$(command -v python3)" \
    --default-index https://pypi.org/simple --only-binary :all: \
    --target "$cutover/prefetch" "$wheel"
done
```

Run the rehearsal with that cache. All installs, CLI commands, and MCP
checks run in the loopback-only namespace with uv offline. The rehearsal
exits if the namespace is unavailable or the offline install fails.
When `UV_CACHE_DIR` is unset, it uses the host's default uv cache.

```bash
python3 scripts/release-rehearsal.py --verbose \
  --published-assets "$cutover/published" \
  --bridge-wheel "$cutover/bridge/sai_cli-0.1.4-py3-none-any.whl" \
  --native-archive "$cutover/native/sai-0.2.4-$target.tar.gz" \
  "$cutover/native/sai_cli-0.2.4-py3-none-any.whl"
```

The script installs the published wheel with uv, updates to the bridge
and then to the native shim, and rolls back using the native updater.
The same uv launcher and temporary profile are used throughout.

The rehearsal checks the selected account, saved config, pending open,
retained logs, and MCP registrations. It starts Sai through Claude Code,
Codex, and Cursor, and checks stale registrations during cutover and
rollback. The loopback Keeper drops the first acknowledgement and reports the
open as pending. A retry must reference that intent and leave the submission
count at one. The script reports failed checks and exits nonzero.
