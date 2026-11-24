# Keeper Intent Outcomes

Use the latest `userIntents` record for the requested key. For a keyed
reconciliation, require both `intentKey` and `evmAddress` to match the request.
Compare the address without regard to case.

Python callers can import `classify_outcome(row)` and its return type
`IntentOutcome` from `sai_cli.core.reconcile`. The classifier uses only the
Keeper row. Reconciliation applies the history and ledger rules below.

| `lifecycleStep` | `execActor` | `errorCode` | Ledger Meaning |
| --- | --- | --- | --- |
| `jwt_pass`, `exec_req_pass` | `server` | null | Accepted. Keeper admitted the intent or recorded the execution handoff. |
| `pending_exec` | `server` | null | Pending. The request transaction landed. The market order awaits an end-block fill or cancellation. |
| `exec_fail` | `server` | `market_order_outcome_not_indexed` | Pending. Keeper's sweep found the order missing on chain and lacks an indexed outcome. A correlated history row can establish success. |
| `exec_fail` | `server` | `chain_execution_failed` or `market_order_<reason>` other than `market_order_outcome_not_indexed` | Terminal-failed. The request transaction failed or Keeper has a cancellation. |
| `exec_req_fail` | `server` | `admission_expired`, `simulation_failed`, `broadcast_rejected`, or another rejection code | Terminal-failed. Admission expired, preflight failed, or the broadcast was rejected. |
| `exec_pass` | `server` | null | Terminal-filled. Keeper observed the intent succeed. Trade history can arrive later or be unavailable for a fill recovered from chain state. |
| `exec_finality_observed` | `user` or `server` | null | Pending for a Perp trade until history contains a row with the same `intentKey` and a `trade`. That row makes the intent terminal-filled. For other kinds, unknown. |
| `intent_start`, `jwt_failed`, `sign_reject`, `sign_failed` | Applicable actor | Any | Unknown. These lifecycle records give no authoritative execution outcome. |
| `jwt_pass`, `exec_req_pass`, `exec_req_fail` | `user` | Any | Unknown. Browser handoffs and rejections carry no Keeper execution authority. |
| Other step or actor | Any | Any | Unknown. Leave the ledger status alone. |

Keeper forwards the chain cancellation reason verbatim after `market_order_`.
Known reasons are `expired`, `fill_failed`, `trading_paused`, `market_closed`,
and `trade_not_open`. Slippage at fill reports `market_order_fill_failed`.
The client also treats the sweep code on a server `exec_req_fail` as
provisional if it encounters that combination. Server withdrawal preflight
can report `withdraw_evm_unconfigured`, `invalid_withdraw`,
`intent_already_used`, `below_minimum`, and `insufficient_idle` on
`exec_req_fail`. [Keeper PR #682](https://github.com/SaiDotFun/sai-keeper/pull/682)

`pending_exec` applies to next-oracle market opens, closes, size changes,
and leverage changes. `execBlock` identifies the request block while
pending and the completion block once final. `txHash` stays the request
transaction hash for the deferred fill or cancellation. For other trade
kinds, terminal-filled means the requested mutation succeeded. For example,
for a limit order placement, it means the conditional order was placed.
Perp kinds with trade history are `OPEN_TRADE`, `CLOSE_POSITION`,
`CANCEL_PENDING_TRADE`, `UPDATE_TRADE`, `UPDATE_OPEN_ORDER`,
`INCREASE_POSITION_SIZE`, and `DECREASE_POSITION_SIZE`.
[Keeper issue #692](https://github.com/SaiDotFun/sai-keeper/issues/692)

## Local Ledger Transitions

Both terminal outcomes store `terminal`. A row in `pending` blocks new
trades for its account and Keeper endpoint. The SQLite row stores
`status='unknown'` and `keeper_status='pending'` so the v0.1.0 trade guard
also blocks it. A row in `unknown` blocks at the same scope. Admission
retains the existing `accepted` rule for a matching trade ID.

An expired `unknown` can become `absent` on an empty Keeper read. A later
admission restores an account-wide block as `pending`. Keeper can still
admit a delayed request after the client releases its block.
[Keeper issue #673](https://github.com/SaiDotFun/sai-keeper/issues/673)

Only a terminal outcome clears a `pending` row. The ledger records
`keeper_status='terminal'` when it verifies a terminal outcome. A
terminal row written before this column existed can be repaired when
Keeper explicitly reports `pending_exec` or the sweep code. A terminal
outcome verified by this version stays terminal.

A keyed check reads the latest 50 history rows for the trader when it can
corroborate a fill or attach history to a canonical `exec_pass`. If the
first read has no match, it tries once more after 250 ms with a two-second
request timeout cap per read. It stops on a history request error. Later
status checks can pick up history that arrives after both reads.
