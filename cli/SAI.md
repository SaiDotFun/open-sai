# Sai CLI Design

The Python CLI in sai-cli v0.1.2 reads Sai Keeper's GraphQL API and submits
trading intents. An intent tells Keeper what operation to execute for a
wallet. The CLI defaults to mainnet. Use endpoint flags or config settings
to select another Keeper and its matching Nibiru REST endpoint.

## Submission and Reconciliation

A live trade command reserves one intent key in the local ledger before
sending the request. Keeper admission can precede execution by several
blocks. For example, a mainnet market open can wait for the next oracle
price. `sai intent wait KEY` follows the same key and returns a receipt.
Use `--addr ADDR` if you do not have the ledger or a session on this machine.

A connection can fail after Keeper receives the request. In that case the
CLI reports `OUTCOME_UNKNOWN` and exits 5. Keep the key and reconcile it
against the original Keeper endpoint. After five minutes, an empty Keeper
result can mark an unresolved local intent `absent` and let trading resume.
A delayed admission can still appear.

## Sessions

A Sai session is a JWT that Keeper issues after a wallet signs a challenge.
Production sessions last 30 days. For a browser session, use **Connect Sai
CLI / MCP** in the Sai Web Mainnet wallet menu and follow the dialog's
`sai auth import` command. The CLI reads a token from a hidden prompt or
the clipboard.

The dialog sets the Mainnet Keeper endpoint for that command. Use the same
endpoint for later CLI and MCP calls. Set it as the default with
`sai config set keeper.endpoint https://sai-keeper.nibiru.fi/query`.

Use `sai auth login NAME` to sign in with a Foundry keystore, then
`sai auth renew [ADDR]` to renew using the saved key name. To refresh an
imported session, copy a fresh token in Sai Web and import it again.

The CLI keeps sessions in `$SAI_HOME/trader/sessions.json` at mode 0600 and
encrypts them with a built-in key. Anyone who can read the file can decrypt
it. `sai auth logout [ADDR]` removes the local copy. A token copied elsewhere
remains usable until it expires.

See the [CLI guide](sai_cli/README.md) for command options and JSON fields.
