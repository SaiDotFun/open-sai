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

`PASS` means the selected invocation or MCP tool call matched. `LISTED`
means the MCP tool's schema and metadata matched in `tools/list`.
`DEFERRED` marks a CLI command that needs another fixture.

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

The runner compares exit codes, stderr categories, and stdout. JSON and JSONL
object keys may appear in any order. Market, account, and watch cases also
compare stdout and stderr byte for byte. Array entries and JSONL events retain
their order. For the concurrent-close case, each process's events retain
their own order, and the two streams are matched by the process outcome.
For MCP, replies are matched by request ID and entries in `tools/list` by
tool name.

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
