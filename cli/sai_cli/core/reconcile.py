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
HISTORY_PAGE_SIZE = 25
HISTORY_MAX_PAGES = 4
HISTORY_BUDGET = 2.0
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
    return value.lower().removeprefix("0x") if isinstance(value, str) else ""


def _read_history(
    endpoint: str,
    addr: str,
    account: str,
    row: dict[str, Any],
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    remaining: Callable[[], float] | None,
) -> list[dict[str, Any]]:
    request_hash = _hash(row.get("txHash"))
    if not request_hash:
        return []
    end = clock() + min(timeout, HISTORY_BUDGET)

    def left() -> float:
        budget = end - clock()
        return min(budget, remaining()) if remaining else budget

    def matching(page: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            item
            for item in page
            if isinstance(item, dict)
            and item.get("intentKey") == row["intentKey"]
            and isinstance(item.get("trade"), dict)
            and keeper.canonical_account(item["trade"].get("trader")) == account
            and request_hash
            in (_hash(item.get("txHash")), _hash(item.get("evmTxHash")))
        ]

    # Keeper filters by trader. Re-read the newest page once for rows arriving
    # in the index while we check the recent pages.
    for offset in range(0, HISTORY_PAGE_SIZE * HISTORY_MAX_PAGES, HISTORY_PAGE_SIZE):
        page = _history_page(endpoint, addr, offset, left())
        if page is None:
            return []
        matches = matching(page)
        if matches:
            return matches
        if len(page) < HISTORY_PAGE_SIZE:
            break
    if left() > HISTORY_RETRY_DELAY:
        sleep(HISTORY_RETRY_DELAY)
        return matching(_history_page(endpoint, addr, 0, left()) or [])
    return []


def _history_page(
    endpoint: str, addr: str, offset: int, timeout: float
) -> list[dict[str, Any]] | None:
    if timeout <= 0:
        return None
    try:
        history = keeper.request(
            endpoint,
            keeper.INTENT_HISTORY,
            {"trader": addr, "limit": HISTORY_PAGE_SIZE, "offset": offset},
            timeout=timeout,
        )
    except keeper.KeeperError:
        # The lifecycle still carries the best available outcome.
        return None
    perp = history.get("perp")
    page = perp.get("tradeHistory") if isinstance(perp, dict) else None
    return page if isinstance(page, list) else None


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
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    remaining: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Read Keeper's latest intent records for ADDR.

    An account list records pending observations and restores blocks on late
    admissions. With KEY, advance that intent using the matching Keeper
    record and bounded history reads. A caller with a deadline supplies
    its remaining seconds and a context manager for ledger writes.
    """
    if remaining is not None:
        timeout = min(timeout, remaining())
        if timeout <= 0:
            raise TimeoutError
    data = keeper.request(
        endpoint, keeper.INTENTS, {"user": addr, "key": key}, timeout=timeout
    )
    rows = data.get("userIntents")
    if rows is None:
        return data
    if not isinstance(rows, list):
        data["userIntents"] = None
        return data
    account = keeper.canonical_account(addr)
    matching = [
        row
        for row in rows
        if isinstance(row, dict)
        and account is not None
        and _matches_account(row, account)
        and (key is None or row.get("intentKey") == key)
    ]
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
            lifecycleStep=None,
            found=False,
            addr=addr,
            endpoint=log.endpoint(endpoint),
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
    outcome = classify_outcome(row)
    if outcome == "unknown":
        return data
    local = _store_outcome(endpoint, account, row, outcome, write)
    kind = row.get("kind")
    if (
        outcome == "terminal-filled" and (kind is None or kind in PERP_HISTORY_KINDS)
    ) or (outcome == "pending" and step != "pending_exec"):
        matches = _read_history(
            endpoint, addr, account, row, timeout, clock, sleep, remaining
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
