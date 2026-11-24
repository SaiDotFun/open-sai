"""Wait for an intent using Keeper reconciliation and a monotonic clock."""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from . import keeper, log, receipt, reconcile, storage

DEFAULT_TIMEOUT = 30.0
MCP_MAX_TIMEOUT = 10.0
POLL_REQUEST_TIMEOUT = 2.0
INITIAL_DELAY = 0.25
MAX_DELAY = 2.0
OUTCOME_CODES = {
    "unknown": "OUTCOME_UNKNOWN",
    "rejected": "ADMISSION_REJECTED",
    "failed": "KEEPER_ERROR",
}
Outcome = Literal["filled", "failed", "rejected", "unknown"]


@dataclass(frozen=True)
class Clock:
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def validate_timeout(seconds: float) -> float:
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Timeout must be a positive finite number of seconds")
    return seconds


def for_intent(
    endpoint: str,
    addr: str,
    key: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    clock: Clock = Clock(),
) -> dict[str, Any]:
    """Poll the same intent until Keeper confirms it or the wait expires."""
    validate_timeout(timeout)
    started = clock.now()
    deadline = started + timeout

    def remaining() -> float:
        return deadline - clock.now()

    @contextmanager
    def write() -> Generator[None]:
        left = remaining()
        if left <= 0:
            raise TimeoutError
        with storage.busy_timeout(left):
            yield

    fields = {"intentKey": key, "addr": addr, "endpoint": log.endpoint(endpoint)}
    outcome: Outcome = "unknown"
    request_tx_hash: str | None = None
    request_block: int | None = None
    row: dict[str, Any] = {}
    history: list[dict[str, Any]] = []
    delay = INITIAL_DELAY
    log.event("intent.wait.start", **fields, timeoutSeconds=timeout)
    try:
        with write():
            local = storage.intent_record(endpoint, key)
        if local == (addr.lower(), "rejected"):
            outcome = "rejected"
        while outcome == "unknown" and remaining() > 0:
            try:
                data = reconcile.reconcile(
                    endpoint,
                    addr,
                    key,
                    timeout=POLL_REQUEST_TIMEOUT,
                    write=write,
                    clock=clock.now,
                    sleep=clock.sleep,
                    remaining=remaining,
                )
            except keeper.KeeperError:
                # A failed read says nothing about execution. Retry within the wait.
                data = {}
            rows = data.get("userIntents") or []
            row = rows[0] if rows else {}
            if row.get("execActor") == "server":
                request_tx_hash = row.get("txHash") or request_tx_hash
                if row.get("lifecycleStep") == "pending_exec":
                    request_block = row.get("execBlock") or request_block
            left = remaining()
            if left <= 0:
                break
            classified = reconcile.classify_outcome(row)
            if (
                classified == "terminal-filled"
                or data.get("outcome") == "terminal-filled"
            ):
                outcome = "filled"
                history = data.get("matchedTradeHistory", [])
                break
            if classified == "terminal-failed":
                outcome = "failed"
                break
            clock.sleep(min(delay, left))
            delay = min(delay * 2, MAX_DELAY)
    except TimeoutError:
        pass
    except sqlite3.OperationalError:
        if remaining() > 0:
            raise
    finally:
        log.event(
            "intent.wait.end",
            **fields,
            outcome=outcome,
            code=OUTCOME_CODES.get(outcome),
            durationMs=round((clock.now() - started) * 1000),
        )

    result: dict[str, Any] = {
        "intentKey": key,
        "addr": addr,
        "outcome": outcome,
        "receipt": receipt.build(
            outcome,
            row,
            history,
            request_tx_hash=request_tx_hash,
            request_block=request_block,
        ),
    }
    if outcome == "failed":
        result.update(keeperErrorCode=row.get("errorCode"), message=row.get("errorMsg"))
    if outcome == "rejected":
        result["message"] = "Keeper rejected admission for this intent."
    if outcome != "filled":
        result["code"] = OUTCOME_CODES[outcome]
    if outcome == "unknown":
        result["hint"] = "Check this same intent again. Do not resubmit."
    return result
