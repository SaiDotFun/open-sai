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
executable path or a command on `PATH` to compare a port. Arguments belong in
`cases.toml`. The runner prints a Markdown report to stdout and exits 1 if
any selected case differs or fails its expectations.

The intent wait and trade `--wait` replays use Python commit
`bdc11da6c9938aa0371835d921f27d45daa77c36`, the main merge of PR #70. Set the
reference launcher separately from the C4 parity reference. The cancel tests
compare the Keeper route, LCD checks, submission, and wait requests. They
also check that a filled target produces zero mutations for both submit
modes. The dry-run case compares each CLI with and without `--wait`.

```sh
SAI_PARITY_WAIT_PYTHON=/absolute/path/to/pinned/.venv/bin/sai \
SAI_PARITY_BIN=/absolute/path/to/rust/sai \
uv run pytest tests/parity/test_intent_wait.py tests/parity/test_trade_wait.py
```

`PASS` means the selected invocation or MCP tool call matched. `LISTED`
means the MCP tool's schema and metadata matched in `tools/list`.
`DEFERRED` marks a CLI command awaiting a fixture or a native implementation.

## Auth Security Parity

The namespace matrix compares rejected import operands and malformed stored
expiry in text and JSON against the native auth implementation from
[PR #106](https://github.com/SaiDotFun/open-sai/pull/106). Those four cases
disable logging. The auth regression tests check output and log redaction
directly, including Python MCP stdio responses.

The matrix omits `auth-use-missing` while the native diagnostic quotes the
unregistered operand. Python returns a fixed diagnostic and omits argument
values from auth command logs. Native auth logging also differs from Python
when the command includes an argument value.

## Native Distribution

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

## Compare MCP Stdio

Check out Python commit `ea09f0d417a53a71079bb8c5e426005fa29051a4` in a
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
- Keep the coverage inventory current. The runner checks that each leaf
  command in Python's Click command tree has a case or a `[deferred]` entry.
  The MCP table comes from the actual stdio `tools/list` response.

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
| Doctor checks | `generatedAt` and check durations, including durations embedded in network-check summaries |
| Doctor provenance | Source checkout path, commit, and dirty flag |
| `update.json` | `checkedAt` |
| Encrypted session envelopes | The nonce and ciphertext, after verifying the decrypted payload |
| Intent ledger and outputs | Generated `intentKey` values and their ledger creation timestamps. The latter must be stored as REAL Unix seconds within the invocation window. |
| Log file grouping | Dated files for each surface are merged in UTC date order. Each event requires a UTC timestamp in `YYYY-MM-DDTHH:MM:SS.sssZ` format that belongs to the file's date and falls within the invocation window. |
| Log events | `ts`, `durationMs`, `pid`, `runId`, `callId`, `commandId`, `endpointId`, generated `intentKey`, and CLI version. The `doctor.result` and `update.check` events use the same rules as their stdout reports. |
| `logs/.endpoint-key` | The randomly generated contents. The file must contain 32 bytes, and its mode is compared. |

Generated IDs use the same placeholder wherever the original ID reappears
within a run. An event with the wrong call ID or an output with a different
intent key can therefore differ even when both IDs have valid shapes.
`LOG_VOLATILE_FIELDS` in `compare.py` assigns normalization rules to the
volatile fields listed in `docs/log-events.json` and to generated intent keys.

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

## Doctor and MCP Registration Reference

The C9a cases compare against Python main at
`3feb83a1ab3e46b4f618f497a4820d6179d19eba`. Install that checkout with
`uv sync --locked`, then point `SAI_C9A_PYTHON` at its
`.venv/bin/sai` executable. Run the driver with
`--case 'doctor-*' --case 'mcp-list*' --case 'mcp-install-*'` and set
`SAI_PARITY_BIN` to the Rust binary. With `SAI_C9A_PYTHON` unset, the
driver uses the Python entry point from its own environment.

The `client_seed` key supplies private Claude Code, Codex, and Cursor
configs. The runner uses fixture programs for client subprocesses and
compares their arguments, file modes, config values, Codex comments, and
which files changed. It also checks that stdout and stderr omit the
fixture credential.

In `doctor-mcp-verification-failure`, Rust restores the Codex config when
verification rejects the repaired entry. The case checks that Rust keeps the
original bytes and mode. The pinned Python reference leaves the replacement
on disk.

The `lcd-origin-only` cases project the LCD endpoint in Python's report to
its origin before comparing it with Rust. They cover successful probes and
an unavailable LCD with a private path. Rust output is compared as emitted.

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
