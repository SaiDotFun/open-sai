# Keeper Test Fixtures

`mainnet_markets_http.json` and `mainnet_prices_http.json` contain the complete
public GraphQL responses from `sai --json market list` and `sai --json market
prices` on 2026-11-13 at about 06:24 UTC. The `data` envelope was restored
because the CLI emits its contents. These include public market data only.

`mainnet_price_ws_payload.json` is a public BTC subscription payload observed
with `sai --json watch price 3` at the same time. It is the decoded GraphQL
payload printed by the CLI, not the WebSocket transport frame. Values and
timestamps are historical observations and must not be used as current quotes.

`synthetic_protocol.json` is independently authored from the Keeper schema and
the protocol shape exercised by the CLI. It is not a network capture. Its
address, account data, signature placeholder, session token, and transaction
hash are synthetic. Intent examples describe the inputs validated by the
local implementation and the known Keeper protocol; they are not an independent
guarantee that every example was accepted by a live deployment.

The tests replay these fixtures with in-process HTTP stubs and local loopback
WebSocket servers. They do not reach a live HTTP or WebSocket service. The
stored examples contain no real JWT or private key.

An autouse fixture blocks external TCP connections in the pytest process.
It does not affect spawned CLI processes, whose integration tests explicitly
select loopback endpoints.

## Log Event Goldens

The JSONL files in `logs/` come from real subprocesses with temporary
`SAI_HOME` directories. Each line follows the [log event schema](../../../../docs/log-events.md).

- `cli-trade-close.jsonl` records `sai --json --endpoint URL trade close 7`
  when the loopback Keeper acknowledges the request and returns a synthetic
  transaction hash.
- `mcp-prices-failure.jsonl` records the `market_prices` call after
  `mcp_info` in one stdio server session. The Keeper URL has no listener,
  so the request and tool result carry `NOT_SENT`.

The capture checks field types against the registry before replacing
volatile values, the loopback port and its endpoint identity, and the
generated intent key. It preserves sequence numbers and ID relationships. Regenerate both files with this command.

```sh
SAI_REGENERATE_LOG_GOLDENS=1 uv run pytest cli/sai_cli/tests/test_logs.py \
  -k 'joins_cli_and_mcp or serve_logs_tool_calls'
```

`intent_outcomes.json` contains selected mainnet `userIntents` and
`tradeHistory` rows captured on 2026-11-24 under `recorded`. The fixture
replaces wallet addresses, intent keys, transaction hashes, and trade and
history IDs. The account's latest history was from November 18, before
next-oracle activation. The `replays` section adapts the mainnet wire shape
using Keeper PR #682's lifecycle producers and cancellation codes. Those
rows include a request, a sweep with a missing outcome, and a later fill.
The tests serve the responses over loopback HTTP.
