# Sai CLI v0.1.3

## Trade Previews and Submission

- Trade confirmations use the chain and Perp contract shown in the preview.
  Request a fresh preview after upgrading or if the route changes. The CLI accepts
  `--preview-digest` for confirmation. For market opens, pass the preview's
  reference price with `--price` too.
  [open-sai PR 150](https://github.com/SaiDotFun/open-sai/pull/150),
  [open-sai PR 194](https://github.com/SaiDotFun/open-sai/pull/194)
- Partial-close confirmations stay valid when only the USD estimate changes.
  Changes to the position's collateral or leverage require a new preview.
  [open-sai PR 219](https://github.com/SaiDotFun/open-sai/pull/219)
- A partial close can leave a nonzero position smaller than the fee schedule
  minimum. Margin and partial-close quotes check that the position belongs to
  the requested account. Open slippage must be positive, at most 100%, and
  representable by the contract.
  [open-sai PR 167](https://github.com/SaiDotFun/open-sai/pull/167),
  [open-sai PR 148](https://github.com/SaiDotFun/open-sai/pull/148)
- Previews explain known contract rejections, including close-only markets and
  leverage changes that would put a position past its liquidation price.
  MCP replies distinguish replacement quotes you can confirm, positions you
  need to preview again, and signed-out open estimates that require sign-in
  before confirmation.
  [open-sai PR 155](https://github.com/SaiDotFun/open-sai/pull/155),
  [open-sai PR 192](https://github.com/SaiDotFun/open-sai/pull/192)
- The CLI and MCP check integer arguments and margin amounts before using
  them. If the local intent ledger cannot reserve a trade, Sai reports it as
  unsent.
  [open-sai PR 154](https://github.com/SaiDotFun/open-sai/pull/154),
  [open-sai PR 125](https://github.com/SaiDotFun/open-sai/pull/125)

## Prices, Account Overview, and Intent Logs

- Market prices show `null` for unknown block times and still report the block
  height. Account overview records when each section loaded and explains when
  it skips prices. It closes slow chain reads at their deadline and uses the
  same method to total unrealized PnL across supported Python versions.
  [open-sai PR 168](https://github.com/SaiDotFun/open-sai/pull/168),
  [open-sai PR 119](https://github.com/SaiDotFun/open-sai/pull/119),
  [open-sai PR 160](https://github.com/SaiDotFun/open-sai/pull/160)
- `sai logs intent` resumes within a millisecond shared by multiple events and
  finds events whose link to the intent appears in an older part of the log.
  If a retained file shrinks during a read, start a new query.
  [open-sai PR 157](https://github.com/SaiDotFun/open-sai/pull/157),
  [open-sai PR 203](https://github.com/SaiDotFun/open-sai/pull/203)

## Sign-In, Settings, and MCP Setup

- Session warnings give you a pasteable renewal command for the original
  account and Keeper. Import errors omit rejected arguments. Account switching
  asks you to sign in again if the stored expiry is unreadable.
  [open-sai PR 156](https://github.com/SaiDotFun/open-sai/pull/156),
  [open-sai PR 152](https://github.com/SaiDotFun/open-sai/pull/152)
- Invalid setting overrides identify the setting and its source and return
  exit code 2. Configuration lists, diagnostics, and older log entries
  display only the scheme, host, and port of endpoints.
  [open-sai PR 191](https://github.com/SaiDotFun/open-sai/pull/191),
  [open-sai PR 151](https://github.com/SaiDotFun/open-sai/pull/151),
  [open-sai PR 157](https://github.com/SaiDotFun/open-sai/pull/157)
- Doctor checks a proposed Codex MCP repair before saving it. For a Claude
  Code entry that differs from the expected setup, Sai shows what differs and
  prints a manual update command. MCP shutdown flushes finished replies and
  gives pending requests a short window to finish. Doctor checks for trader
  data before adjusting profile directory permissions.
  [open-sai PR 153](https://github.com/SaiDotFun/open-sai/pull/153),
  [open-sai PR 161](https://github.com/SaiDotFun/open-sai/pull/161),
  [open-sai PR 154](https://github.com/SaiDotFun/open-sai/pull/154)

## Installing and Using the Bundled Guide

- The installer reports an incomplete GitHub reply as a release lookup
  error. The standalone installer adds a native install path for Linux and
  macOS once native archives are published. The updater can keep the uv
  launcher in place when a later bridge release switches an existing
  Python install to the native CLI.
  [open-sai PR 127](https://github.com/SaiDotFun/open-sai/pull/127),
  [open-sai PR 182](https://github.com/SaiDotFun/open-sai/pull/182),
  [open-sai PR 116](https://github.com/SaiDotFun/open-sai/pull/116)
- The CLI guide and bundled Sai skill explain how to read intent wait
  receipts, partial account overviews, and log pages. If the collateral price
  is unavailable, the skill tells agents to preview the partial close again
  once Sai can read the price and get a fresh confirmation before submitting.
  [open-sai PR 169](https://github.com/SaiDotFun/open-sai/pull/169),
  [open-sai PR 164](https://github.com/SaiDotFun/open-sai/pull/164)
