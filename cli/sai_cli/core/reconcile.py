"""Reconcile locally recorded intents with Keeper's intent records."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Literal

from . import keeper, log, storage
from .errors import Code

WriteGate = Callable[[], AbstractContextManager[object]]
IntentOutcome = Literal[
    "accepted", "pending", "terminal-filled", "terminal-failed", "unknown"
]

FAILED_STEPS = ("exec_fail", "exec_req_fail")
ACCEPTED_STEPS = ("jwt_pass", "exec_req_pass")
OUTCOME_NOT_INDEXED = "market_order_outcome_not_indexed"
HISTORY_READS = 2
HISTORY_TIMEOUT = 2
HISTORY_RETRY_DELAY = 0.25
PERP_HISTORY_KINDS = {
    "OPEN_TRADE",
    "CLOSE_POSITION",
    "CANCEL_PENDING_TRADE",
    "UPDATE_TRADE",
    "UPDATE_OPEN_ORDER",
    "INCREASE_POSITION_SIZE",
    "DECREASE_POSITION_SIZE",
}
PENDING_HINT = "Fill pending at the next oracle price. Do not resubmit."
UNINDEXED_HINT = "The order may have filled. Check positions and run intent status again. Do not resubmit."
HISTORY_HINT = "Waiting for matching trade history. Do not resubmit."
LOCAL_PENDING_HINT = "This intent is still pending locally. Do not resubmit."


def classify_outcome(row: Mapping[str, Any]) -> IntentOutcome:
    """Classify one Keeper row using the contract in intent-outcomes.md.

    This classifier needs neither a local ledger nor a history query.
    A keyed reconciliation can corroborate some pending rows with history.
    """
    step = row.get("lifecycleStep")
    actor = row.get("execActor")
    if step == "exec_finality_observed" and actor in ("user", "server"):
        # A browser observation can carry the server actor on a relayed intent.
        return "pending" if row.get("kind") in PERP_HISTORY_KINDS else "unknown"
    if actor != "server":
        return "unknown"
    if step == "pending_exec":
        return "pending"
    if step in FAILED_STEPS:
        return (
            "pending"
            if row.get("errorCode") == OUTCOME_NOT_INDEXED
            else "terminal-failed"
        )
    if step == "exec_pass":
        return "terminal-filled"
    if step in ACCEPTED_STEPS:
        return "accepted"
    return "unknown"


def _pending_hint(row: dict[str, Any]) -> str:
    if row.get("lifecycleStep") == "pending_exec":
        return PENDING_HINT
    if row.get("errorCode") == OUTCOME_NOT_INDEXED:
        return UNINDEXED_HINT
    return HISTORY_HINT


def _explicit_market_pending(row: dict[str, Any]) -> bool:
    return row.get("lifecycleStep") == "pending_exec" or (
        row.get("errorCode") == OUTCOME_NOT_INDEXED
        and row.get("lifecycleStep") in FAILED_STEPS
    )


def _matches_account(row: Mapping[str, Any], account: str) -> bool:
    nibi = row.get("nibiAddress")
    return keeper.canonical_account(row.get("evmAddress")) == account and (
        nibi is None or keeper.canonical_account(nibi) == account
    )


def _hash(value: str | None) -> str:
    return (value or "").lower().removeprefix("0x")


def _read_history(
    endpoint: str, addr: str, account: str, row: dict[str, Any], timeout: float
) -> list[dict[str, Any]]:
    request_hash = _hash(row.get("txHash"))
    if not request_hash:
        return []
    for attempt in range(HISTORY_READS):
        if attempt:
            time.sleep(HISTORY_RETRY_DELAY)
        try:
            history = keeper.request(
                endpoint, keeper.HISTORY, {"trader": addr}, timeout=timeout
            )
        except keeper.KeeperError:
            # The lifecycle still carries the best available outcome.
            break
        matches = [
            item
            for item in history["perp"]["tradeHistory"]
            if item.get("intentKey") == row["intentKey"]
            and keeper.canonical_account((item.get("trade") or {}).get("trader"))
            == account
            and request_hash
            in (_hash(item.get("txHash")), _hash(item.get("evmTxHash")))
        ]
        if matches:
            return matches
    return []


def _store_outcome(
    endpoint: str,
    account: str,
    row: dict[str, Any],
    outcome: IntentOutcome,
    write: WriteGate,
    *,
    only_if_absent: bool = False,
) -> str | None:
    status = "terminal" if outcome.startswith("terminal-") else outcome
    with write():
        return storage.set_status(
            row["intentKey"],
            endpoint,
            account,
            status,
            repair_legacy_terminal=outcome == "pending"
            and _explicit_market_pending(row),
            only_if_absent=only_if_absent,
        )


def reconcile(
    endpoint: str,
    addr: str,
    key: str | None = None,
    *,
    timeout: float = 20,
    write: WriteGate = nullcontext,
) -> dict[str, Any]:
    """Read Keeper's latest intent records for ADDR.

    An account list records pending observations and restores blocks on late
    admissions. With KEY, advance that intent using the matching Keeper
    record and at most two history reads. Each ledger write uses
    `with write():` so a caller can enforce its own deadline.
    """
    data = keeper.request(
        endpoint, keeper.INTENTS, {"user": addr, "key": key}, timeout=timeout
    )
    rows = data.get("userIntents")
    account = keeper.canonical_account(addr)
    matching = [
        row
        for row in rows or []
        if isinstance(row, dict)
        and account is not None
        and _matches_account(row, account)
        and (key is None or row.get("intentKey") == key)
    ]
    if rows is not None:
        data["userIntents"] = matching
    if account is None:
        return data
    if key is None:
        for row in matching:
            outcome = classify_outcome(row)
            if not row.get("intentKey") or outcome not in ("pending", "accepted"):
                continue
            local = _store_outcome(
                endpoint,
                account,
                row,
                outcome,
                write,
                only_if_absent=outcome == "accepted",
            )
            if local == "pending" or (
                local is None and outcome == "pending" and _explicit_market_pending(row)
            ):
                hint = (
                    _pending_hint(row) if outcome == "pending" else LOCAL_PENDING_HINT
                )
                row.update(outcome="pending", hint=hint)
        return data

    if rows == []:
        log.event(
            "intent.keeper",
            intentKey=key,
            found=False,
            addr=account,
            endpoint=log.endpoint(endpoint),
            endpointId=log.endpoint_id(endpoint),
        )
        with write():
            local = storage.mark_absent(
                key, endpoint, account, keeper.ABSENT_AFTER_SECONDS
            )
        if local is not None:
            data["localStatus"] = local
        if local == "pending":
            data.update(outcome="pending", hint=LOCAL_PENDING_HINT)
        return data
    if not matching:
        return data

    row = matching[0]
    step = row.get("lifecycleStep")
    tx_hash, invalid_hash = keeper.request_hash(row.get("txHash"))
    error_code = keeper.intent_error_code(row)
    outcome = classify_outcome(row)
    log.event(
        "intent.keeper",
        intentKey=key,
        kind=keeper.known_text(row.get("kind"), keeper.INTENT_KINDS),
        lifecycleStep=keeper.known_text(step, keeper.INTENT_STEPS),
        execActor=keeper.known_text(row.get("execActor"), keeper.INTENT_ACTORS),
        errorCode=error_code,
        txHash=tx_hash,
        txHashInvalid=invalid_hash,
        addr=account,
        endpoint=log.endpoint(endpoint),
        endpointId=log.endpoint_id(endpoint),
        code=Code.KEEPER_ERROR if outcome == "terminal-failed" else None,
    )
    if outcome == "unknown":
        return data
    local = _store_outcome(endpoint, account, row, outcome, write)
    kind = row.get("kind")
    if (
        outcome == "terminal-filled" and (kind is None or kind in PERP_HISTORY_KINDS)
    ) or (outcome == "pending" and step != "pending_exec"):
        matches = _read_history(
            endpoint, addr, account, row, min(timeout, HISTORY_TIMEOUT)
        )
        data["matchedTradeHistory"] = matches
        if matches and outcome == "pending":
            outcome = "terminal-filled"
            local = _store_outcome(endpoint, account, row, outcome, write)
            data["outcome"] = outcome
            if local is not None:
                data["localStatus"] = local

    if outcome == "pending" or local == "pending":
        if local is not None:
            data["localStatus"] = local
        if local == "pending" or local is None:
            hint = _pending_hint(row) if outcome == "pending" else LOCAL_PENDING_HINT
            data.update(outcome="pending", hint=hint)
    return data
