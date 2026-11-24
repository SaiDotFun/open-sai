"""Reconcile locally recorded intents with Keeper's intent records."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from . import keeper, log, storage
from .errors import Code

WriteGate = Callable[[], AbstractContextManager[object]]

FAILED_STEPS = ("exec_fail", "exec_req_fail")
ACCEPTED_STEPS = ("jwt_pass", "exec_req_pass", "exec_pass")


def reconcile(
    endpoint: str,
    addr: str,
    key: str | None = None,
    *,
    timeout: float = 20,
    write: WriteGate = nullcontext,
) -> dict[str, Any]:
    """Read Keeper's latest intent records for ADDR.

    With KEY, update the local status of that intent from Keeper's record:
    a server failure is terminal, a server acceptance is accepted, and an
    executed intent with a matching trade history row is terminal. When
    Keeper has no record past the admission expiry, the key stops blocking
    new trades and `localStatus` reports the change.

    TIMEOUT applies to each Keeper request. Each ledger write happens inside
    `with write():`, so a caller can refuse writes after a deadline by
    raising from it.
    """
    data = keeper.request(
        endpoint, keeper.INTENTS, {"user": addr, "key": key}, timeout=timeout
    )
    if key and data.get("userIntents"):
        row = data["userIntents"][0]
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
            code=Code.KEEPER_ERROR if step in FAILED_STEPS else None,
        )
        if (
            row.get("intentKey") == key
            and str(row.get("evmAddress") or "").lower() == addr.lower()
        ):
            server = row.get("execActor") == "server"
            if server and step in FAILED_STEPS:
                with write():
                    storage.set_status(key, endpoint, addr, "terminal")
            elif server and step in ACCEPTED_STEPS:
                with write():
                    storage.set_status(key, endpoint, addr, "accepted")
            if (step == "exec_pass" and server) or step == "exec_finality_observed":
                history_data = keeper.request(
                    endpoint, keeper.HISTORY, {"trader": addr}, timeout=timeout
                )
                matches = [
                    item
                    for item in history_data["perp"]["tradeHistory"]
                    if item.get("intentKey") == key and item.get("trade")
                ]
                data["matchedTradeHistory"] = matches
                if matches:
                    with write():
                        storage.set_status(key, endpoint, addr, "terminal")
    elif key and data.get("userIntents") == []:
        log.event(
            "intent.keeper",
            intentKey=key,
            found=False,
            addr=addr,
            endpoint=log.endpoint(endpoint),
        )
        # Keeper has no record. Past its admission expiry it never will,
        # so the key no longer blocks new trades.
        with write():
            local = storage.mark_absent(
                key, endpoint, addr, keeper.ABSENT_AFTER_SECONDS
            )
        if local is not None:
            data["localStatus"] = local
    return data
