# Sai CLI v0.2.0

Sai 0.2.0 is the first native CLI release for Linux and macOS. It uses the
same profile and command names as the Python CLI, including `sai mcp serve`.

## Upgrade

- If you made a trade preview before upgrading, make a fresh preview before
  confirming the trade.
- If uv installed the Sai 0.1.3 release wheel, run `sai update` to switch to
  native Sai. You can also run `sai update --to 0.1.4` after the Python
  bridge is published, then run `sai update` to switch. For an older release
  wheel, update to 0.1.3 first. The switch keeps the uv launcher and your
  profile. Restart agent clients to load the updated MCP server.
  [#226](https://github.com/SaiDotFun/open-sai/pull/226),
  [#232](https://github.com/SaiDotFun/open-sai/pull/232),
  [#238](https://github.com/SaiDotFun/open-sai/pull/238)
- On Windows, `sai update` stays on 0.1.3 because native Sai does not run
  on Windows ([#260](https://github.com/SaiDotFun/open-sai/issues/260)).
  Run the managed installer to install the full Python CLI instead:

  ```console
  uv run https://raw.githubusercontent.com/SaiDotFun/open-sai/main/scripts/install.py --full-python --version 0.2.0
  ```

- If uv installed Sai from Git, uninstall the `sai-cli` tool, use the
  [managed installer](../../README.md#trader-cli) with `--version 0.1.3`
  or `--version 0.1.4`, then run `sai update`. Your Sai profile stays in place.
- The standard `sai-cli` wheel is a small launcher that downloads and runs
  the matching native binary. The full Python wheel and source package are
  inside `sai-cli-0.2.0-full-python.zip`. Windows runs full Python.
  To keep using Python on Linux or macOS, use the
  [managed installer](../../README.md#trader-cli) with
  `--full-python --version 0.2.0`.
  [#227](https://github.com/SaiDotFun/open-sai/pull/227),
  [#246](https://github.com/SaiDotFun/open-sai/pull/246)

## Trade Previews and Order Entry

- Open a trade with a market symbol such as `--market BTC`. If the symbol
  matches several markets, Sai lists their IDs. Text mode shows a compact
  market table. Use `sai trade cancel ID` to cancel a pending order.
  [#235](https://github.com/SaiDotFun/open-sai/pull/235),
  [#249](https://github.com/SaiDotFun/open-sai/pull/249)
- Leverage and margin previews show estimated collateral before and after
  the trade in USDC and USD. Use `--max-collateral-usd` to size a margin-add
  quote. The contract settles target leverage against the position at
  execution, so final collateral can differ from the quote.
  [#235](https://github.com/SaiDotFun/open-sai/pull/235),
  [#248](https://github.com/SaiDotFun/open-sai/pull/248),
  [#249](https://github.com/SaiDotFun/open-sai/pull/249)
- Sai rechecks market rules before sending a confirmed trade. If a market
  stopped accepting opens after the preview, Sai reports the change. If
  a close needs a market price and its publication time is unavailable or
  stale, get a fresh preview once the price is available.
  [#233](https://github.com/SaiDotFun/open-sai/pull/233),
  [#239](https://github.com/SaiDotFun/open-sai/pull/239)
- A partial close can leave a position below the opening minimum. If Sai
  cannot read the collateral price, a rebuilt partial-close quote can show
  the USDC estimate. Preview again when the price is available before
  confirming.
  [#241](https://github.com/SaiDotFun/open-sai/pull/241),
  [#256](https://github.com/SaiDotFun/open-sai/pull/256)

## Trade Results and Account Reads

- With `--wait`, trade commands poll Keeper for up to 60 seconds for an
  outcome, and text mode prints a progress line. Without `--wait`, the
  command returns while the intent is pending. For a delayed result, the
  recovery command points to the same intent. MCP replies
  include the account and intent key when a trade may already be pending.
  [#225](https://github.com/SaiDotFun/open-sai/pull/225),
  [#228](https://github.com/SaiDotFun/open-sai/pull/228),
  [#241](https://github.com/SaiDotFun/open-sai/pull/241),
  [#249](https://github.com/SaiDotFun/open-sai/pull/249)
- A market-open receipt shows the fill price and filled collateral when
  Keeper ties those values to the opening fill. Sai leaves those fields empty
  when the fill details are unavailable.
  [#228](https://github.com/SaiDotFun/open-sai/pull/228),
  [#240](https://github.com/SaiDotFun/open-sai/pull/240)
- Balances show their source block and age. Text mode marks reads older
  than five minutes as stale and says when the age is unknown. Account
  overview keeps available positions when a position or fill read fails and
  reports the error in that section.
  [#235](https://github.com/SaiDotFun/open-sai/pull/235),
  [#237](https://github.com/SaiDotFun/open-sai/pull/237),
  [#248](https://github.com/SaiDotFun/open-sai/pull/248),
  [#249](https://github.com/SaiDotFun/open-sai/pull/249)

## Command Help, Logs, and MCP

- Commands such as `sai intent wait` and `sai logs intent` show their full
  help when you leave out required input. Invalid config overrides tell you
  which setting and source caused the error.
  [#200](https://github.com/SaiDotFun/open-sai/pull/200),
  [#230](https://github.com/SaiDotFun/open-sai/pull/230)
- The native MCP server offers account overview and intent logs. Agent
  clients can read the retained intent trail offline or include the current
  Keeper status when connected.
  [#229](https://github.com/SaiDotFun/open-sai/pull/229)
- MCP install reports show the resolved executable and summarize saved
  environment entries. `sai doctor` and foreground updates can repair stale
  Codex and Cursor registrations through native upgrade and rollback.
  Sai prints manual repair steps for Claude Code entries that need attention.
  [#230](https://github.com/SaiDotFun/open-sai/pull/230),
  [#238](https://github.com/SaiDotFun/open-sai/pull/238),
  [#242](https://github.com/SaiDotFun/open-sai/pull/242),
  [#244](https://github.com/SaiDotFun/open-sai/pull/244)

## Release Checks

- Release rehearsals stage both package layouts and smoke-test all four
  native builds. The tests compare behavior on the Python and native CLIs,
  including intent logs that span midnight UTC.
  [#170](https://github.com/SaiDotFun/open-sai/pull/170),
  [#223](https://github.com/SaiDotFun/open-sai/pull/223),
  [#227](https://github.com/SaiDotFun/open-sai/pull/227),
  [#246](https://github.com/SaiDotFun/open-sai/pull/246),
  [#247](https://github.com/SaiDotFun/open-sai/pull/247),
  [#259](https://github.com/SaiDotFun/open-sai/pull/259)
