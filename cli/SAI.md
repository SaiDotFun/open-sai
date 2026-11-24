# Sai CLI Design

Sai Keeper exposes market and account data over GraphQL and accepts semantic
trading intents authenticated with a wallet session JWT. This CLI constructs
those requests directly. The client defaults to mainnet and accepts explicit
HTTP and WebSocket endpoints for local development and tests.

One command submits one intent. Before submission, the CLI records its identity
locally. A transport error can follow acceptance, so the CLI reports an unknown
outcome and requires reconciliation rather than retrying a new intent. Keeper
expires recorded intents after two minutes, so `intent status` treats a key
that Keeper has not recorded five minutes after submission as never admitted
and marks it `absent`. Keeper's acceptance is not trade finality. `intent status` reads Keeper's current state.

Keeper issues one credential: a session JWT signed after a wallet challenge,
valid for 30 days in production, with no scopes and no revocation. The CLI
uses that same token. On Sai Web Mainnet, open the wallet menu and select
**Connect Sai CLI / MCP** beside **Send to phone** for the selected, connected
wallet with an active trading session. Review its expiry and click **Copy
session token**. Run the dialog's `sai auth import` command. Paste the token at
the hidden prompt or press Enter to read the clipboard. Agents can run the
command without a terminal to read the clipboard directly.

`auth login NAME` signs the challenge with the Foundry keystore of that name
through `cast`. `auth logout` deletes the local copy. The CLI
stores the session under `$SAI_HOME/trader` at mode 0600, encrypted with a
fixed built-in key, so no command prompts for a passphrase.
Per-device credentials are not yet part of this repository.
