"""Reconcile locally recorded intents with Keeper's intent records."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Literal

from . import keeper, log, storage

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


def _read_history(
    endpoint: str, addr: str, key: str, timeout: float
) -> list[dict[str, Any]]:
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
            if item.get("intentKey") == key and item.get("trade")
        ]
        if matches:
            return matches
    return []


def reconcile(
    endpoint: str,
    addr: str,
    key: str | None = None,
    *,
    timeout: float = 20,
    write: WriteGate = nullcontext,
) -> dict[str, Any]:
    """Read Keeper's latest intent records for ADDR.

    With KEY, advance the local ledger from the matching Keeper record.
    History can corroborate a fill and is read at most twice. Each ledger
    write uses `with write():` so a caller can enforce its own deadline.
    """
    data = keeper.request(
        endpoint, keeper.INTENTS, {"user": addr, "key": key}, timeout=timeout
    )
    rows = data.get("userIntents")
    if key is None:
        for row in rows or []:
            if classify_outcome(row) == "pending" and _explicit_market_pending(row):
                row.update(outcome="pending", hint=_pending_hint(row))
        return data

    if rows == []:
        log.event(
            "intent.keeper",
            intentKey=key,
            lifecycleStep=None,
            found=False,
            addr=addr,
            endpoint=log.endpoint(endpoint),
        )
        with write():
            local = storage.mark_absent(
                key, endpoint, addr, keeper.ABSENT_AFTER_SECONDS
            )
        if local is not None:
            data["localStatus"] = local
        if local == "pending":
            data.update(outcome="pending", hint=LOCAL_PENDING_HINT)
        return data
    if not rows:
        return data

    row = rows[0]
    step = row.get("lifecycleStep")
    log.event(
        "intent.keeper",
        intentKey=key,
        kind=row.get("kind"),
        lifecycleStep=step,
        execActor=row.get("execActor"),
        errorCode=row.get("errorCode"),
        errorMsg=log.untrusted(row.get("errorMsg")),
        txHash=row.get("txHash"),
        addr=addr,
        endpoint=log.endpoint(endpoint),
    )
    if (
        row.get("intentKey") != key
        or str(row.get("evmAddress") or "").lower() != addr.lower()
    ):
        return data

    outcome = classify_outcome(row)
    if outcome == "unknown":
        return data
    status = "terminal" if outcome.startswith("terminal-") else outcome
    with write():
        local = storage.set_status(
            key,
            endpoint,
            addr,
            status,
            repair_legacy_terminal=outcome == "pending"
            and _explicit_market_pending(row),
        )
    kind = row.get("kind")
    if (
        outcome == "terminal-filled" and (kind is None or kind in PERP_HISTORY_KINDS)
    ) or (outcome == "pending" and step != "pending_exec"):
        matches = _read_history(endpoint, addr, key, min(timeout, HISTORY_TIMEOUT))
        data["matchedTradeHistory"] = matches
        if matches and outcome == "pending":
            with write():
                local = storage.set_status(key, endpoint, addr, "terminal")
            outcome = "terminal-filled"
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
