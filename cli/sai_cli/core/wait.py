"""Wait for an intent using Keeper reconciliation and a monotonic clock."""

from __future__ import annotations

import asyncio
import math
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from . import keeper, log, receipt, reconcile, storage
from .errors import Code

DEFAULT_TIMEOUT = 30.0
MCP_MAX_TIMEOUT = 10.0
POLL_REQUEST_TIMEOUT = 2.0
INITIAL_DELAY = 0.25
MAX_DELAY = 2.0
OUTCOME_CODES = {
    "unknown": Code.OUTCOME_UNKNOWN,
    "rejected": Code.ADMISSION_REJECTED,
    "not_sent": Code.NOT_SENT,
    "failed": Code.KEEPER_ERROR,
    "liquidated": Code.KEEPER_ERROR,
    "unexpected": Code.KEEPER_ERROR,
}
Outcome = Literal[
    "filled",
    "placed",
    "succeeded",
    "failed",
    "liquidated",
    "unexpected",
    "rejected",
    "not_sent",
    "absent",
    "terminal",
    "unknown",
]

RECORDED_OUTCOMES: dict[str, Outcome] = {
    "terminal": "terminal",
    "absent": "absent",
    "rejected": "rejected",
    "not_sent": "not_sent",
}


@dataclass(frozen=True)
class Clock:
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] | None = None

    async def pause(self, seconds: float) -> None:
        if self.sleep is None:
            await asyncio.sleep(seconds)
        else:
            self.sleep(seconds)


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
    started: float | None = None,
    clock: Clock = Clock(),
) -> dict[str, Any]:
    """Run the CLI wait with the same polling logic as MCP."""
    try:
        return asyncio.run(
            for_intent_async(
                endpoint, addr, key, timeout=timeout, started=started, clock=clock
            )
        )
    except KeyboardInterrupt:
        try:
            recorded = _recorded_outcome(endpoint, addr, key)
        except (sqlite3.Error, OSError, ValueError):
            recorded = None
        return {
            **_result(key, addr, recorded or "unknown", {}, [], None, None),
            "interrupted": True,
            "hint": storage.intent_status_hint(endpoint, addr, key),
        }


def _recorded_outcome(endpoint: str, addr: str, key: str) -> Outcome | None:
    """Read a definitive disposition without waiting on a locked ledger."""
    with storage.busy_timeout(0):
        try:
            local = storage.intent_record(endpoint, key)
        except sqlite3.OperationalError as exc:
            if not ledger_busy(exc):
                raise
            return None
    if local is not None and local[0] == keeper.canonical_account(addr):
        return RECORDED_OUTCOMES.get(local[1])
    return None


async def for_intent_async(
    endpoint: str,
    addr: str,
    key: str,
    *,
    timeout: float,
    started: float | None = None,
    clock: Clock = Clock(),
) -> dict[str, Any]:
    """Poll the same intent within one deadline, including every network read."""
    validate_timeout(timeout)
    account = keeper.canonical_account(addr)
    if account is None:
        raise ValueError("Pass a valid EVM or Nibiru account address")
    started = clock.now() if started is None else started
    deadline = started + timeout

    def remaining() -> float:
        return deadline - clock.now()

    @contextmanager
    def write() -> Generator[None]:
        left = remaining()
        if left <= 0:
            raise TimeoutError
        # SQLite writes run on the event loop. A busy database must give
        # control back to the caller instead of blocking cancellation.
        with storage.busy_timeout(0):
            yield

    endpoint_label = log.endpoint(endpoint)
    endpoint_id = log.endpoint_identity(endpoint)
    outcome: Outcome = "unknown"
    request_tx_hash: str | None = None
    request_block: int | None = None
    row: dict[str, Any] = {}
    history: list[dict[str, Any]] = []
    delay = INITIAL_DELAY
    log.event(
        "intent.wait.start",
        intentKey=key,
        addr=addr,
        endpoint=endpoint_label,
        endpointId=endpoint_id,
        timeoutMs=round(timeout * 1000),
    )
    try:
        try:
            await asyncio.sleep(0)
            with write():
                recorded = _recorded_outcome(endpoint, account, key)
            if recorded in ("rejected", "not_sent"):
                outcome = recorded
            else:
                async with httpx.AsyncClient() as client:
                    while remaining() > 0:
                        steps = reconcile.poll_steps(
                            endpoint,
                            addr,
                            key,
                            timeout=POLL_REQUEST_TIMEOUT,
                            write=write,
                            clock=clock.now,
                            remaining=remaining,
                            include_action=True,
                        )
                        try:
                            data = await _reconcile(
                                client, endpoint, steps, clock, remaining
                            )
                        except keeper.KeeperError:
                            data = {}
                        except sqlite3.OperationalError as exc:
                            if not ledger_busy(exc):
                                raise
                            data = {}
                        rows = data.get("userIntents") or []
                        row = rows[0] if rows else {}
                        if row.get("execActor") == "server":
                            request_tx_hash = row.get("txHash") or request_tx_hash
                            if row.get("lifecycleStep") == "pending_exec":
                                request_block = row.get("execBlock") or request_block
                        classified = reconcile.classify_outcome(row)
                        if (
                            classified == "terminal-filled"
                            or data.get("outcome") == "terminal-filled"
                        ):
                            history = data.get("matchedTradeHistory", [])
                            outcome = receipt.success(row, history)
                            break
                        if classified == "terminal-failed":
                            outcome = "failed"
                            break
                        if (
                            data.get("localStatus") == "absent"
                            and classified == "unknown"
                        ):
                            outcome = "absent"
                            break
                        left = remaining()
                        if left <= 0:
                            break
                        await clock.pause(min(delay, left))
                        delay = min(delay * 2, MAX_DELAY)
        except TimeoutError:
            pass
        except sqlite3.OperationalError as exc:
            if not ledger_busy(exc):
                raise
        if outcome == "unknown":
            outcome = _recorded_outcome(endpoint, account, key) or "unknown"
    finally:
        log.event(
            "intent.wait.end",
            intentKey=key,
            addr=addr,
            endpoint=endpoint_label,
            endpointId=endpoint_id,
            status=outcome,
            code=OUTCOME_CODES.get(outcome),
            durationMs=round((clock.now() - started) * 1000),
        )

    return _result(key, addr, outcome, row, history, request_tx_hash, request_block)


def ledger_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    return code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


async def _reconcile(
    client: httpx.AsyncClient,
    endpoint: str,
    steps: reconcile.Steps,
    clock: Clock,
    remaining: Callable[[], float],
) -> dict[str, Any]:
    data = None
    failure = None
    with closing(steps):
        while True:
            # Take cancellation before resuming reconciliation, which can
            # open and commit a SQLite transaction without another await.
            await asyncio.sleep(0)
            try:
                step = steps.throw(failure) if failure else steps.send(data)
            except StopIteration as done:
                return done.value
            data, failure = None, None
            if not isinstance(step, reconcile.Read):
                await clock.pause(min(step, max(0, remaining())))
                continue
            left = min(step.timeout, remaining())
            if left <= 0:
                failure = keeper.KeeperError("Keeper read deadline expired")
                continue
            try:
                async with asyncio.timeout(left):
                    data = await keeper.read_async(
                        client, endpoint, step.query, step.variables
                    )
            except TimeoutError:
                failure = keeper.KeeperError("Keeper read deadline expired")
            except keeper.KeeperError as exc:
                failure = exc


def unknown_result(key: str, addr: str | None) -> dict[str, Any]:
    return _result(key, addr, "unknown", {}, [], None, None)


def rejected_result(key: str, addr: str, message: str) -> dict[str, Any]:
    return {**_result(key, addr, "rejected", {}, [], None, None), "message": message}


def _result(
    key: str,
    addr: str | None,
    outcome: Outcome,
    row: dict[str, Any],
    history: list[dict[str, Any]],
    request_tx_hash: str | None,
    request_block: int | None,
) -> dict[str, Any]:
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
    if outcome == "liquidated":
        result["message"] = "Keeper recorded liquidation while processing the intent."
    if outcome == "unexpected":
        change = result["receipt"]["execution"]["tradeChangeType"]
        result["message"] = (
            f"Keeper recorded {change} for this intent. Review the receipt."
        )
    if outcome == "rejected":
        result["message"] = "Keeper rejected admission for this intent."
    if outcome == "not_sent":
        result["message"] = "The local ledger recorded that the intent was not sent."
    if outcome == "terminal":
        result["message"] = (
            "The local ledger recorded a final outcome. Its execution details are unavailable."
        )
    if outcome == "absent":
        result["message"] = (
            "Keeper had no record after the admission grace period. The local block was released. A delayed admission can still appear."
        )
    if outcome in OUTCOME_CODES:
        result["code"] = OUTCOME_CODES[outcome]
    if outcome == "unknown":
        result["hint"] = "Check this same intent again. Do not resubmit."
    return result
