# Keeper Test Fixtures

`mainnet_markets_http.json` and `mainnet_prices_http.json` contain the complete
public GraphQL responses from `sai --json market list` and `sai --json market
prices` on 2026-11-13 at about 06:24 UTC. The `data` envelope was restored
because the CLI emits its contents. These include public market data only.

`mainnet_price_ws_payload.json` is a public BTC subscription payload observed
with `sai --json watch price 3` at the same time. It is the decoded GraphQL
payload printed by the CLI, not the WebSocket transport frame. Values and
timestamps are historical observations and must not be used as current quotes.

`mainnet_account_overview.json` records public Keeper reads from 2026-11-24.
The account had Sai USDC and credits, with no open trades or queued market
orders. Two historical trades record the deployed response shape with a
synthetic trader identifier. Overview
tests add synthetic states and orders to exercise positions, limit and stop
orders, and next-oracle orders. The loopback LCD fixture supplies block times.

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

`intent_outcomes.json` contains selected mainnet `userIntents` and
`tradeHistory` rows captured on 2026-11-24 under `recorded`. The fixture
replaces wallet addresses, intent keys, transaction hashes, and trade and
history IDs. The account's latest history was from November 18, before
next-oracle activation. The `replays` section adapts the mainnet wire shape
using Keeper PR #682's lifecycle producers and cancellation codes. Those
rows include a request, a sweep with a missing outcome, and a later fill.
The tests serve the responses over loopback HTTP.
