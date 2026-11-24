"""Semantic intent construction and reliable submission."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from secrets import choice
from typing import Any

from . import keeper, log, storage
from .errors import Code


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def positive(value: str, name: str) -> Decimal:
    try:
        if not re.fullmatch(r"(0|[1-9][0-9]*)(\.[0-9]{1,18})?", value):
            raise ValueError
        result = Decimal(value)
        if not result.is_finite() or result <= 0 or result * 10**18 > 2**128 - 1:
            raise ValueError
        return result
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite decimal") from exc


def open_intent(
    market: int,
    side: str,
    order_type: str,
    amount: str,
    leverage: str,
    price: str | None,
    slippage: str,
    tp: str | None,
    sl: str | None,
) -> dict[str, Any]:
    if market < 0:
        raise ValueError("Market index must not be negative")
    for value, name in ((amount, "Amount"), (leverage, "Leverage")):
        positive(value, name)
    if price is not None:
        positive(price, "Price")
    elif order_type != "market":
        raise ValueError("A trigger price is required for limit and stop orders")
    if Decimal(amount) * 1_000_000 != (Decimal(amount) * 1_000_000).to_integral_value():
        raise ValueError("Amount must have at most six decimal places")
    if slippage != "0" and (
        positive(slippage, "Slippage") > 100 or len(slippage.partition(".")[2]) > 16
    ):
        raise ValueError(
            "Slippage must be between 0 and 100 with at most 16 decimal places"
        )
    body: dict[str, Any] = {
        "market": {"marketIndex": f"MarketIndex({market})"},
        "side": side,
        "orderType": order_type,
        "amountUsd": amount,
        "leverage": leverage,
        "maxSlippagePct": slippage,
    }
    if price is not None:
        body["intentPrice"] = price
    for key, value in (("takeProfit", tp), ("stopLoss", sl)):
        if value is not None:
            positive(value, key)
            body[key] = value
    return {"kind": "OPEN_TRADE", "body": body}


def close_intent(trade_id: int, cancel: bool = False) -> dict[str, Any]:
    if trade_id < 0:
        raise ValueError("Trade id must not be negative")
    return {
        "kind": "CANCEL_PENDING_TRADE" if cancel else "CLOSE_POSITION",
        "target": {"userTradeId": str(trade_id)},
        "body": {},
    }


def leverage_intent(trade_id: int, leverage: str) -> dict[str, Any]:
    if trade_id < 0:
        raise ValueError("Trade id must not be negative")
    positive(leverage, "Leverage")
    return {
        "kind": "UPDATE_TRADE",
        "target": {"userTradeId": str(trade_id)},
        "body": {
            "takeProfit": {"operation": "keep"},
            "stopLoss": {"operation": "keep"},
            "requestedLeverage": leverage,
        },
    }


def tpsl_intent(
    trade_id: int, tp: str | None, sl: str | None, clear_tp: bool, clear_sl: bool
) -> dict[str, Any]:
    if (
        trade_id < 0
        or (not tp and not sl and not clear_tp and not clear_sl)
        or (tp and clear_tp)
        or (sl and clear_sl)
    ):
        raise ValueError(
            "Specify a trade and at least one non-conflicting TP or SL change"
        )

    def patch(value: str | None, clear: bool) -> dict[str, str]:
        if value is not None:
            positive(value, "TP/SL price")
            return {"operation": "set", "value": value}
        return {"operation": "clear" if clear else "keep"}

    return {
        "kind": "UPDATE_TRADE",
        "target": {"userTradeId": str(trade_id)},
        "body": {"takeProfit": patch(tp, clear_tp), "stopLoss": patch(sl, clear_sl)},
    }


def partial_intent(
    trade: dict[str, Any], percent: int, min_size: Decimal
) -> dict[str, Any]:
    if percent < 1 or percent > 99:
        raise ValueError("Partial close percentage must be from 1 to 99")
    amount = Decimal(str(trade["collateralAmount"]))
    leverage = Decimal(str(trade["leverage"]))
    delta = (amount * percent / 100).to_integral_value(rounding=ROUND_DOWN)
    if delta <= 0 or (amount - delta) / Decimal(1_000_000) * leverage < min_size:
        raise ValueError(
            "Reduction is too small or leaves less than the market minimum"
        )
    usd = format(delta / Decimal(1_000_000), "f")
    return {
        "kind": "DECREASE_POSITION_SIZE",
        "target": {"userTradeId": str(trade["id"])},
        "body": {
            "collateral": {"amountUsdc": str(delta), "amountUsd": usd},
            "leverageDelta": "0",
        },
    }


def _record_submission_status(
    key: str, endpoint: str, account: str, status: str
) -> BaseException | None:
    """Try the ledger update without masking the outcome from Keeper."""
    try:
        storage.set_status(key, endpoint, account, status)
    except (sqlite3.Error, OSError, ValueError, KeyboardInterrupt) as exc:
        log.event(
            "intent.status_write_failed",
            "warn",
            code=Code.INTERNAL_ERROR,
            intentKey=key,
            addr=account,
            endpoint=log.endpoint(endpoint),
            endpointId=log.endpoint_identity(endpoint),
            status=status,
            errorType=type(exc).__name__,
        )
        return exc
    return None


def submit(
    endpoint: str,
    session: dict[str, str],
    intent: dict[str, Any],
    *,
    allow_concurrent_open: bool = False,
) -> dict[str, Any]:
    instant = datetime.now(UTC)
    key = (
        instant.strftime("%Y%m%dT%H%M%S")
        + f"{instant.microsecond // 1000:03d}Z_"
        + "".join(choice("0123456789ABCDEFGHJKMNPQRSTVWXYZ") for _ in range(16))
    )
    stamp = now()
    account = session["evmAddress"]
    envelope = {
        "intentKey": key,
        "intent": {"intentSchemaVersion": "v0", "tsIntentInit": stamp, **intent},
        "lifecycle": {
            "sequence": 1,
            "lifecycleStep": "jwt_pass",
            "tsLifecycleStep": stamp,
        },
    }
    storage.pending(
        key,
        endpoint,
        account,
        intent["kind"],
        intent.get("target", {}).get("userTradeId", ""),
        allow_concurrent_open=allow_concurrent_open,
    )
    try:
        data = keeper.request(
            endpoint, keeper.SUBMIT, {"input": envelope}, session["token"]
        )
        ack = data["recordUserIntent"]
        if (
            ack["intentKey"] != key
            or ack["lifecycleStep"] != "jwt_pass"
            or ack["execActor"] != "server"
            or ack["evmAddress"].lower() != account.lower()
        ):
            raise keeper.KeeperError("Acknowledgement does not match submitted intent")
    except (keeper.AdmissionRejected, keeper.NotSent) as exc:
        exc.intent_key = key
        status = "rejected" if isinstance(exc, keeper.AdmissionRejected) else "not_sent"
        if failure := _record_submission_status(key, endpoint, account, status):
            exc.hint = storage.intent_status_hint(endpoint, account, key)
            raise exc from failure
        raise
    except (
        keeper.KeeperError,
        KeyError,
        TypeError,
        AttributeError,
        KeyboardInterrupt,
    ) as exc:
        raise keeper.OutcomeUnknown(
            f"Outcome unknown for intent {key}; reconcile before another trade",
            key,
            hint=storage.intent_status_hint(endpoint, account, key),
        ) from exc
    tx_hash, invalid_hash = keeper.request_hash(ack.get("txHash"))
    log.event(
        "intent.submitted",
        intentKey=key,
        kind=intent["kind"],
        addr=account,
        endpoint=log.endpoint(endpoint),
        endpointId=log.endpoint_identity(endpoint),
        txHash=tx_hash,
        txHashInvalid=invalid_hash,
    )
    if failure := _record_submission_status(key, endpoint, account, "accepted"):
        raise keeper.OutcomeUnknown(
            f"Keeper accepted intent {key}, but the local ledger update failed;"
            " reconcile before another trade",
            key,
            hint=storage.intent_status_hint(endpoint, account, key),
        ) from failure
    return {
        "intentKey": key,
        "status": "accepted",
        "message": "Accepted by Keeper; execution is not yet confirmed",
    }
