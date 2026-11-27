# Sai CLI Parity

Run from the repository root after `uv sync --locked`.

```sh
just parity
SAI_PARITY_BIN=/absolute/path/to/sai just parity
SAI_PARITY_BIN=/absolute/path/to/sai uv run python tests/parity/parity.py --case 'config-*'
```

Run on Linux with user and network namespaces, `unshare` and `setpriv`
from util-linux, `ip` from iproute2, and `libseccomp.so.2`. The driver brings up loopback in a
fresh network namespace and launches both CLIs without network administration
privileges. Unroutable IPv4 or IPv6 attempts fail the case, even if the CLI
catches the socket error. A seccomp filter also blocks Unix and virtual-machine
sockets that could reach a host service. The driver stops at startup if it cannot establish
the isolated network or find a native `uv` executable that runs with a
temporary home.

Ledger comparison requires SQLite 3.26 or later for `table_xinfo`.
Pytest skips ledger and namespace parity tests below that version.

The default pytest run includes the comparison unit tests. CI selects the
namespace-dependent candidate tests with
`uv run pytest -m parity_namespace tests/parity` after provisioning the
loopback prerequisites.

The default run compares Python with Python. Set `SAI_PARITY_BIN` to an
executable path or a command on `PATH` to compare a port. Set
`SAI_PARITY_PYTHON` to the `sai` executable installed from a pinned Python
checkout to override the reference. Arguments belong in `cases.toml`. The
runner prints a Markdown report to stdout and exits 1 if any selected case
differs or fails its expectations.

The intent wait and trade `--wait` replays use Python commit
`bdc11da6c9938aa0371835d921f27d45daa77c36`, the main merge of PR #70. Set the
reference launcher separately from the C4 parity reference. The cancel tests
compare the Keeper route, LCD checks, submission, and wait requests. They
also check that a filled target produces zero mutations for both submit
modes. The dry-run case compares each CLI with and without `--wait`.

The TP/SL and leverage wait cases use Python main at
`20493c984e3398756365ea1a2b8786a7a0115a11`. They check the request and
execution blocks, admission and chain failure codes, and intent-key recovery
after Ctrl-C. The deadline cases exercise the pin's 30-second budget and
Rust's 60-second budget. They also check that Rust reports a leverage
execution first visible after 33 seconds. Current Python trade `--wait`
also uses 60 seconds and tests the late result with a simulated clock.

```sh
SAI_PARITY_WAIT_PYTHON=/absolute/path/to/pinned/.venv/bin/sai \
SAI_PARITY_POSITION_WAIT_PYTHON=/absolute/path/to/position-wait-pin/.venv/bin/sai \
SAI_PARITY_BIN=/absolute/path/to/rust/sai \
uv run pytest tests/parity/test_intent_wait.py tests/parity/test_trade_wait.py
```

`PASS` means the selected invocation or MCP tool call matched. `LISTED`
means the MCP tool's schema and metadata matched in `tools/list`.
`DEFERRED` marks a CLI command awaiting a fixture or a native implementation.
`ACCEPTED` means each implementation met its recorded expectations for an
intentional difference.

## Release Cutover Results

The required Rust job runs the full manifest and the focused comparisons
against the same `sai-cli/v0.1.2` executable. Run the manifest locally with
`parity.py --reference /absolute/path/to/released/sai --cutover` and set
`SAI_PARITY_BIN` to the native executable.

Each `CUTOVER_ROW` in the CI log carries a stable ID, a result, and the
observed difference paths. Hashes bind the normalized values without exposing
them in the job log. The full manifest must contain every case ID in
[`release-case-ids.txt`](release-case-ids.txt). `case:` IDs use names in
`cases.toml`, `cli:` IDs
use deferred command names, and `mcp:` IDs use tool names. The MCP result
includes tool metadata and requires a matching `tools/call` transcript.

[`manifest-xfails.toml`](manifest-xfails.toml) records the owner and hash of
each reviewed difference signature. An exact match reports `XFAIL`. A changed
signature, unexpected pass, or unused entry fails the required job. Owners
remove their entries in the same change that repairs the corresponding gaps.
The `#211` titles give the reason each release exception matches or differs
from Python main at `a48e7775`, using the same fixtures and strict comparison.
A title that says the row matches main is a candidate for removal when the
release reference advances and the row passes against that reference.

[`python-main-gaps.toml`](python-main-gaps.toml) records Python CLI changes
that landed after the release reference. The runner checks these signatures
when it compares current Python against a candidate and reports
`CURRENT_PYTHON_ROW` results. A changed signature or unexpected pass fails the
comparison. Port the behavior and remove its entry in the same change.

CI records the C3 storage replays against v0.1.2 and a separate C2/C3 replay
against v0.1.1. The nine C2 config cases share their exact log-schema
signatures across both releases. Each v0.1.1 config result appears as a
`C2_V011_ROW`.

## Auth Security Parity

The namespace matrix compares rejected import operands and malformed stored
expiry in text and JSON against the native auth implementation from
[PR #106](https://github.com/SaiDotFun/open-sai/pull/106). Those four cases
disable logging. The auth regression tests check output and log redaction
directly, including Python MCP stdio responses.

The `auth-use-missing` case keeps its release exception because Rust uses
the fixed diagnostic from Python main. Python v0.1.2 echoed the unregistered
account operand in the error.

For `auth use` and `auth logout`, the focused log tests compare each command
start, session event, and command end against the release. Rust records only
the command words in `argv`, following Python main's auth privacy behavior.
Python v0.1.2 records the address operand and flags.

Rust applies the process umask to mode `0644` when creating
`trader/sessions.lock`. Python applies it to mode `0666`, so a permissive
umask can give Python's new lock group or other write permissions. Both
CLIs preserve the mode of an existing lock.

## Native Distribution

The Linux installer rehearsal installs the published Python v0.1.3 wheel
with uv, then runs `sai update` against the candidate shim and archive over
loopback. The published updater checks for the `sai VERSION` banner during
its trial. Rust keeps that banner for the handoff, and the manifest records
the resulting difference from Python's `sai, version VERSION` banner.

The Native Distribution workflow installs the built shim wheel with uv on
each native target and serves the matching archive over loopback. Build the
loopback-only wheel with `python scripts/native-dist.py rehearsal-wheel DIR`.
Set `SAI_NATIVE_WHEEL` and `SAI_NATIVE_ARCHIVE` to the rehearsal wheel and archive paths to run
`uv run --locked pytest tests/parity/test_native_distribution.py`. If uv
is exposed through a version manager, set `UV` to the uv executable itself.
Set `SAI_NATIVE_RELEASE_WHEEL` to the official-source wheel to check that it
ignores the loopback override. The CI bundle contains the official-source wheel.

The Linux x86_64 job also tests upgrade and offline rollback against the
published Python release `sai-cli/v0.1.1` at commit
`9ee5a356fd41a22697483e467652d367035909bd`. To run that case locally, set
`SAI_NATIVE_BASELINE` to a directory with the published wheel and
`SHA256SUMS`, `scripts/install.py` from the pinned commit as `install.py`,
and the commit SHA in a file named `commit`.

Native MCP `auth_status` uses "Requested account" for disconnected addresses
so an accidental URL in `addr` cannot reach the client transcript. The parity
case rewrites that address in the pinned Python reference only.

## Compare MCP Stdio

Check out Python commit `1c671b231b0fc968a44ec8dd520e67ea28e25b7f` in a
separate directory. Use CPython 3.12, which preserves the tool description
indentation recorded in the Rust inventory.

```sh
uv sync --project /absolute/path/to/python-checkout --python 3.12 --locked
SAI_MCP_PARITY_PYTHON=/absolute/path/to/python-checkout/.venv/bin/sai \
SAI_PARITY_BIN=/absolute/path/to/rust/sai \
  uv run python tests/parity/parity.py --case 'mcp-*-*'
```

The transcript checks server initialization, ordered tool metadata,
strict argument errors, and the JSON string in each successful response's
`content` and `structuredContent`. It also replaces a fixture session
between calls to check the trade refusal before argument validation.
Auth cases check expiry, strict arguments, and active account changes
from both MCP and a separate CLI process. They also check credential
redaction and verify that an invalid account summary leaves selection intact.
The wait transcript verifies exact Keeper request counts and the ledger for
filled and still-pending intents.

Run the overview and log-query transcripts against the published
`sai-cli/v0.1.3` reference. The overview transcript checks clock values
before masking them for the byte comparison. The log-query transcript
checks the retained events and counts Keeper requests in each mode.

```sh
SAI_MCP_PARITY_V013_PYTHON=/absolute/path/to/python-v013/.venv/bin/sai \
SAI_PARITY_BIN=/absolute/path/to/rust/sai \
  unshare -Urn sh -c 'ip link set lo up && exec "$@"' sh \
  uv run --locked pytest tests/parity/test_mcp_stdio.py -k v013
```

Native validation replies report the validation category and schema-defined
field names. They omit submitted values and unknown field names. Until the
Python follow-up lands, the transcript projects that expected difference
only on the Python side of declared validation failures. It compares the
native reply against the projected text exactly.

## Add a Case

Each `[[case]]` table has a unique `name` and a list of CLI arguments in
`args`. The `expect` key defaults to exit code 0. Set `mode` to `text`,
`jsonl`, or `mcp` when stdout uses one of those formats. The default is
`json`.

- The `seed` key selects starting files. The `setup` key can run one CLI
  invocation before the command under test. Both processes start with
  separate temporary `HOME` and `SAI_HOME` directories at mode `0700`.
- Use placeholders such as `{addr}`, `{intent}`, and `{keeper}` in
  arguments. Both implementations use the same loopback HTTP and WebSocket
  servers. Child environments omit inherited proxies and set `NO_PROXY`
  for loopback.
- The `wire` list specifies the required requests, including counts.
  The stub checks the full GraphQL document against `keeper-requests.json`
  before returning a fixture. HTTP queries and WebSocket subscriptions
  preserve operation names, arguments, and variables. Whitespace and field
  order may vary. LCD requests also check their arguments. Doctor runs its
  network checks in parallel, so its wire list is compared without regard
  to order.
- Set `interrupt_after` to send SIGINT after that many JSONL snapshots.
  The runner checks the final output, exit status, and flushed events.
- The `state` table pins persisted values on each side. A key takes the
  form `file#object.key.0.field`, with zero-based list indexes. Use
  `file#@mode` to assert a mode, or `"<absent>"` to assert that a file or
  field is missing.
- The `output` table pins normalized stdout fields using dotted paths.
  `mcp_calls` sends specific tool calls after initialization. If a case sets
  `retry_expect`, the runner invokes the same command again in the same
  home and checks both outputs under `first` and `retry`.
- Keep the coverage inventory current. The runner checks that each leaf
  command in Python's Click command tree has a case or a `[deferred]` entry.
  The MCP table comes from the actual stdio `tools/list` response.

## Python References in Rust CI

The Rust CI job sets `SAI_PARITY_PYTHON` to the `sai` executable built from
the Python tag `sai-cli/v0.1.2`, commit `8557a7e13dceab0accc369191182c8e01423c91b`.
To compare every selected manifest case against that same executable, run
`uv run --locked python tests/parity/parity.py --reference "$SAI_PARITY_PYTHON"`
with `SAI_PARITY_BIN` set to the Rust binary. The explicit reference applies
to the CLI cases and the MCP transcripts.

The focused Rust CI comparison uses `--release-xfails`. The file
`release-xfails.toml` maps exact failing pytest IDs to the open PR that owns
each gap and the signature of its normalized field differences. The cases
still run, and an unexpected pass fails CI so the fixing
PR removes its marks in the same change. Keep a scenario's explicit pytest
ID when changing its fixture. If a first fix exposes a second known gap,
retain the scenario and update its expected failure with the next owner.
Run without the option to see the raw failures.

## Auth Subprocess Cases

Run `tests/parity/test_auth.py` with `SAI_PARITY_BIN` set to the Rust binary.
Set `SAI_PARITY_AUTH_PYTHON` to the Python `sai` executable from a checkout
of `main` to compare auth, including renewal, expiry recovery, and origin-only
output. The Rust CI job pins the reference to a commit on `main`. The test runs
use private temporary homes, fixture clipboard and signing commands, and
loopback Keeper.

The `legacy` seed contains a config file and an encrypted synthetic session
written by the Python source at tag `sai-cli/v0.1.0`. The concurrent-close
case overlaps two processes using one ledger. The stub holds the first
admission until the contender exits, and the runner checks that the ledger
contains one `unknown` row tied to the actual request before releasing the
reply. The unsubmitted-intent case starts with a committed reservation and
replays recovery when Keeper has no record. This case models the state left
by an exit before submission.

## Position Trade Reference

The margin and partial-close cases use Python commit
`275df9c60af376ba7d884863fb00b011cd07ae02`, including the ownership check
from [PR 148](https://github.com/SaiDotFun/open-sai/pull/148) and route
binding from [PR 150](https://github.com/SaiDotFun/open-sai/pull/150).
The cases compare canonical ownership, the requested index, quote decimals,
and preview digests. Rust checks the Keeper trader and index before contacting
the LCD. Foreign-owner and stale-digest cases require zero reservations and
submissions.

The standalone runner projects out Python-only estimates when comparing to
Rust. It checks Python's estimate reads and Rust's quote reads separately.
The stale-index case records Python's retries and Rust's immediate refusal.
The wrong-chain cases account for the indexed reads Rust logs before the LCD
network check. They still compare the rest of each log.

```sh
SAI_PARITY_PYTHON=/path/to/pinned/.venv/bin/sai \
  SAI_PARITY_BIN=/path/to/rust/sai \
  uv run python tests/parity/parity.py --case 'trade-margin*' --case 'trade-partial-close*'
```

## Installer and Python Bridge Cases

Run `uv run pytest tests/parity/test_installer_bridge.py`. The archive and
ownership refusal cases run without build artifacts. Set the following paths
to run the release replay:

| Variable | Local artifact |
| --- | --- |
| `SAI_C10B_RUST` | Rust binary built for the runner with the same version as the shim. |
| `SAI_C10B_SHIM` | Shim wheel or a directory containing it. Build it from PR #116 or its merged source. For loopback downloads, substitute the fixture release source from `tests/parity/native_release_source.py` before building. |
| `SAI_C10B_BRIDGE` | Full Python wheel or a directory containing it. Build it from the branch under test at a lower version than the shim. |
| `SAI_C10B_PRE_BRIDGE` | Directory with the Python wheel and `install.py` from the pinned pre-bridge commit `8557a7e13dceab0accc369191182c8e01423c91b`, and that commit SHA in a file named `commit`. |
| `SAI_C10B_MUTANT` | A build of the same Rust source that fails `mcp serve` only when invoked from an installed standalone version directory or the persistent uv tool environment. |

The fixture builder derives rehearsal versions from `pyproject.toml`. Both
workflows build the wheels locally and run the compatibility checks with
real uv launchers. Native MCP startup is an
expected failure when a build from before PR #120 reports a missing command.

## Auth Argument Privacy Reference

Set `SAI_PARITY_AUTH_PRIVACY_PYTHON` to the `.venv/bin/sai` from Python
[PR #152](https://github.com/SaiDotFun/open-sai/pull/152) at commit
`3725b66446da215f3e84e65ad9fce36679b80c20`. The CLI tests check the
`auth use` diagnostic and the `command.start` arguments against that build.

## C11 Python Reference

The C11 parity fixes use Python tag `sai-cli/v0.1.2` at commit
`8557a7e13dceab0accc369191182c8e01423c91b`. Install that checkout with
`uv sync --locked` and point `SAI_PARITY_PYTHON` at its `.venv/bin/sai`.
The focused CI step and the full release manifest use the same v0.1.2
launcher.

```sh
SAI_PARITY_PYTHON=/absolute/path/to/v0.1.2/.venv/bin/sai \
SAI_PARITY_BIN=/absolute/path/to/rust/sai \
  uv run --locked pytest tests/parity/test_intent_status.py \
    -k 'c11 or watch_reports_rejected_upgrade'
```

## Released Intent Status Cases

Run `tests/parity/test_intent_status_v012.py` with `SAI_PARITY_BIN` set to
the Rust binary and `SAI_PARITY_PYTHON` set to the Python executable from
tag `sai-cli/v0.1.2`, commit `8557a7e13dceab0accc369191182c8e01423c91b`.
The focused release CI step runs this file and the intent status replays
against the same executable.

The `intent-status` manifest case checks that a valid transaction hash
matches exactly one history row. The focused tests also cover the original
malformed hash from #212. Python v0.1.2 matches it by string equality. Rust
requires a valid hash as evidence and keeps an unresolved intent pending.

## Comparison Rules

The C7d estimate cases use the Python v0.1.2 commit
`8557a7e13dceab0accc369191182c8e01423c91b`. Set
`SAI_PARITY_ESTIMATES_PYTHON` to its `sai` launcher and run
`uv run pytest tests/parity/test_preview_estimates.py` with `SAI_PARITY_BIN`.
These cases compare dry-run stdout bytes after replacing the live
`observedAt` timestamps. The Rust confirmation tests compare canonical bytes
and SHA-256 digests generated by the same Python commit.

The runner compares exit codes, stderr categories, and stdout. JSON and JSONL
object keys may appear in any order. Market, account, and watch cases also
compare stdout and stderr byte for byte. Array entries and JSONL events retain
their order. For the concurrent-close case, each process's events retain
their own order, and the two streams are matched by the process outcome.
For MCP, each request must receive one response with the matching ID.
The driver rejects any extra lines on stdout and compares the tool list
in wire order. It checks that both content fields contain the same JSON
string before comparing the decoded document. Server provenance has
implementation-specific values and must have a valid absolute path and shape.

The `SAI_HOME` snapshot compares relative paths, file types, modes, and
contents. It parses TOML values and checks that edits preserve the existing
config text outside the edited value, including its inline comment. It
authenticates and decrypts session envelopes. It reads SQLite's parsed column
types, defaults, nullability, primary keys, foreign keys, table options, and
index metadata from a copy with its journal files. The snapshot also compares
`CHECK` expressions in the stored table definitions, the user version, and
typed rows. Formatting of the CREATE statements, row order, and physical
page layout may vary.
The runner parses `update.json` as JSON and compares other files byte for
byte. The report names differing paths and fields.

The comparison permits these values to vary, with field presence and shape
checked on both sides.

| Location | Values |
| --- | --- |
| Text and structured output | The run's absolute temporary home path |
| Stdout from `--version`, `skill source`, `update --check`, `doctor`, and MCP initialization | Running or installed CLI versions at the documented fields |
| `skill source` | Absolute package directory containing the bundled skill files. |
| Doctor checks | `generatedAt` and check durations, including durations embedded in network-check summaries |
| Doctor provenance | Source checkout path, commit, and dirty flag |
| `update.json` | `checkedAt` |
| Encrypted session envelopes | The nonce and ciphertext, after verifying the decrypted payload |
| Intent ledger and outputs | Generated `intentKey` values and their ledger creation timestamps. The latter must be stored as REAL Unix seconds within the invocation window. |
| Log file grouping | Dated files for each surface are merged in UTC date order. Each event requires a UTC timestamp in `YYYY-MM-DDTHH:MM:SS.sssZ` format that belongs to the file's date and falls within the invocation window. |
| Log events | Fields listed under `volatileFields` in `docs/log-events.json` and generated `intentKey` values. The `doctor.result` and `update.check` events use the same rules as their stdout reports. |
| `logs/.endpoint-key` | The randomly generated contents. The file must contain 32 bytes, and its mode is compared. |

Generated IDs use the same placeholder wherever the original ID reappears
within a run. An event with the wrong call ID or an output with a different
intent key can therefore differ even when both IDs have valid shapes.
Each field listed under `volatileFields` needs a normalization rule in
`compare.py` that checks the value's format. Intent keys use the same
mapping in logs, the ledger, and command output.

## Account Overview Reference

The `account-overview-*` and `account-bare` cases use Python main at
`20493c984e3398756365ea1a2b8786a7a0115a11`.
The reference includes the cancellable LCD reads from PR #119 and the
address-pinned renewal guidance from PRs #135 and #156. Rust CI installs
it on Python 3.13.
The three-position case compares gross PnL against the compensated
`sum()` in Python 3.12 and later. Both implementations use the same
summation on Python 3.11, the package's minimum supported interpreter.
Set `SAI_PARITY_OVERVIEW_PYTHON` to its `sai` executable for local runs.
The cases validate height-pinned requests and compare section errors,
source timestamps, risk values, and persisted state. Fetch timestamps and
ages are checked against the invocation and then normalized.

## C9b Python Reference

Commit `bdc11da6c9938aa0371835d921f27d45daa77c36` is the Python
reference for logs, skill management, and the REPL. It contains the logging
v2 schema from PR #69 and the paging fixes from PR #93. In CI,
`test_local_commands.py` uses the installed Python in the current checkout.
To reproduce the pinned comparison, install Python from that commit and set
`SAI_C9B_PARITY_PYTHON` to its `sai` launcher. The C4 tests select the v0.1.2 launcher via
`SAI_PARITY_PYTHON` in CI.

The dense intent attribution test also matches Python `sai-cli/v0.1.2`
at commit `8557a7e`. With 1,025 distinct anchors, Rust carries a compact
file range in its cursor and still returns a row linked to an anchor on
an earlier page.

Accepted C9b divergences from the pinned Python reference

- Rust names the rejected option or argument without echoing its value.
  `skill show` errors still list the valid pages. Python v0.1.2 quotes
  rejected values in diagnostics.
- Rust allowlists command names, flags, and known-safe values in fresh
  and retained `argv`. It replaces other values and free-form error log
  text with `[redacted]`. It shows only the origin of a structured endpoint
  when it parses. Python v0.1.2 retains arbitrary argument and error text.
- Rust `logs tail` strips credentials, paths, queries, and fragments from
  URLs throughout retained rows, including arguments and nested fields.
  The pinned Python reference prints the retained values. Current Python
  strips them after PR #157.
- Rust `logs intent` resumes at the first unread file positions when a
  page ends and retains attribution from both the initial scan and anchors
  discovered in later scans.
  The pinned Python reference can omit events that share the last displayed
  timestamp. Current Python keeps the remaining events after PR #157.
  Rust also returns attributed command starts on their retained page when
  the scan budget ends before that file. The pinned Python reference may
  include those starts on an earlier page. UTC timestamps passed directly
  to `--before` remain exclusive.

## Shared-Home Trade Replays

The shared-state cases pin Python to commit
`973f3f59f38dd89d0c2e03e674157934a00316ae`. This pin includes the
position-confirmation changes in #150. Use a checkout at that commit with
its project environment installed. The Rust CI job runs this file separately
from the Keeper and auth parity files.

```sh
uv sync --project "$SAI_PYTHON_REFERENCE" --locked
cargo build --locked -p sai
cargo test --locked -p sai-core --test storage --no-run
```

Set `SAI_PYTHON_REFERENCE` to the pinned checkout, `SAI_PARITY_BIN` to the
absolute path of the compiled Rust CLI, and `SAI_PARITY_RESERVE_BIN` to
the storage test executable printed by the last Cargo command. Run
`uv run --locked python tests/parity/network.py -m parity_namespace tests/parity/test_trade_shared_state.py -rs`.
The network launcher requires Linux user namespaces, `libseccomp`, and the
system commands listed at the top of this file. Direct pytest selection also
checks that only loopback is available before launching any CLI or MCP process.

Each case starts with a private `SAI_HOME` and uses fixture credentials
against loopback Keeper and LCD servers. The crash cases stop the real
storage writer at a pipe barrier after SQLite commits. The other runtime
opens the same ledger, attempts a reservation, and reconciles the
original key. The loopback server must see zero mutations. The grace
period case advances the fixture row's timestamp to exercise recovery
without waiting five minutes.

The trade cases count every mutation arrival by intent key, including
repeated arrivals with the same key. The tests hold or drop the admission
reply to exercise contention, transport timeouts, and reconciliation. The
wait cases replay pending and final results and check the receipt against
the original request and the indexed execution.

Native trade, wait, and MCP skips link to their tracking issue or PR. CI
also builds the PR base on `main` and checks its CLI and MCP capabilities
before skipping. For the same check locally, set `SAI_PARITY_MAIN_BIN` to
the executable built from `main`. A missing shipped adapter or an MCP
stub that changes the ledger or touches Keeper's mutation fails the case.

## Bare Command Help

`test_bare_command_parity.py` uses the checkout's Python launcher. The
reference is Python main at `cb8c8f92c458c22df9662efea99ee5ecd9d53fd0`.
The manifest covers every leaf reported by Python's `required_commands`
helper. The test checks that bare text matches the same executable's
`--help`, with stderr and exit 2, and that both executables return the same
JSON usage error.

Run `uv run --locked pytest tests/parity/test_bare_command_parity.py` with
`SAI_PARITY_BIN` set to the Rust binary. The cases include the bare REPL,
logs and skill groups, and margin and partial-close commands.

## Doctor and MCP Registration Reference

The C9a cases compare against Python main at
`3feb83a1ab3e46b4f618f497a4820d6179d19eba`. Install that checkout with
`uv sync --locked`, then point `SAI_C9A_PYTHON` at its
`.venv/bin/sai` executable. Run the driver with
`--case 'doctor-*' --case 'mcp-list*' --case 'mcp-install-*'` and set
`SAI_PARITY_BIN` to the Rust binary. With `SAI_C9A_PYTHON` unset, the
driver uses the Python entry point from its own environment.

Doctor cases that query `node_info` use the checkout's Python entry point.
The LCD comparison contract comes from Python main at
`59a694fc3bebcf18954de864d9aa8e009a8ac558`. The native CI gate runs
`--case doctor-network --case 'doctor-lcd-*'`. These cases cover matching
chains, a missing Keeper chain ID, a reflected LCD path, an unreachable
LCD, and malformed chain IDs. The driver compares the CLI streams byte
for byte after masking the temporary home path, report timestamp, probe
and check durations, and each implementation's package version.

The invalid-chain cases pin exit 1 in both modes when `node_info` returns
a nonempty malformed chain ID. Earlier Rust reported a warning and exited
0 in this fixture. Current Rust treats it as a failed LCD check, matching
Python.

The logging-enabled mismatch case checks the LCD warning event and scans
the raw logs for the fixture path. It allows the existing difference in
the Keeper probe event, where Python includes `command` and `commandId`
and Rust omits both fields.

The `mcp-uninstall-*` cases use the Python `sai-cli/v0.1.2` reference at
`8557a7e13dceab0accc369191182c8e01423c91b`. Point `SAI_MCP_UNINSTALL_PYTHON`
at that checkout's `.venv/bin/sai` and run the driver with
`--case 'mcp-uninstall-*'`.

The `client_seed` key supplies private Claude Code, Codex, and Cursor
configs. The runner uses fixture programs for client subprocesses and
compares their arguments, file modes, config values, Codex comments, and
which files changed. It also checks that stdout and stderr omit the
fixture credential.

In `doctor-mcp-verification-failure`, Rust asks Codex to check the proposed
launcher using read-only overrides. When Codex rejects it, Rust reports the
failure and leaves the original config in place. The pinned Python v0.1.2
reference leaves the replacement on disk.

In `mcp-uninstall-codex-query-failure`, both CLIs report the failed Codex
query. Rust restores the original configuration bytes and mode, so the
result reports no change or reconnect. The Python reference leaves the
entry deleted.

The `mcp-uninstall-*-read-only` cases use mode-0444 Codex and Cursor files
in writable directories. Python replaces the file with the Sai entry removed
and preserves its mode. Rust returns `INTERNAL_ERROR` and leaves the bytes and
mode intact. The cases also check that the unrelated server remains.

The `lcd-origin-only` cases project the LCD endpoint in Python's report to
its origin before comparing it with Rust. The byte-comparison cases use
Python's origin-only report directly.

## TP/SL and Leverage

The cases in `test_position_update.py` use the Python v0.1.2 preview
calculations and unsigned dry runs from commit
`8557a7e13dceab0accc369191182c8e01423c91b`. Signed cases also check the
ownership requirements from PR #148 at commit
`c6d85c210a441f3729d091151a114a8edeb0afa3`. Each case checks the ordered
preview or intent against a fixture after validating dynamic values such
as the oracle observation timestamp and intent key.

CI installs both pinned Python launchers. For a local run, set
`SAI_PARITY_BIN` to the Rust binary,
`SAI_PARITY_POSITION_V012_PYTHON` to the v0.1.2 launcher, and
`SAI_PARITY_POSITION_OWNER_PYTHON` to the PR #148 launcher. Run
`uv run --locked pytest tests/parity/test_position_update.py`.

The Rust confirmation test uses canonical digest bytes generated by the
Python digest function from PR #150 at commit
`9b20dd771050724dc8684ffca8de3f34ae417ffa`. The vectors apply its
`positionRoute` field to both standalone updates.
