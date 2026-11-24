"""Build intent receipts from Keeper lifecycle and trade history evidence."""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from . import keeper

TRADE_FILLS = {
    "position_opened",
    "position_closed_user",
    "trade_size_increase",
    "trade_size_decrease",
}
PLACEMENTS = {"limit_order_created": "limit", "stop_order_created": "stop"}
OPEN_CHANGES = {order_type: change for change, order_type in PLACEMENTS.items()}
REQUESTED_CHANGES = {
    "CLOSE_POSITION": {"position_closed_user"},
    "INCREASE_POSITION_SIZE": {"trade_size_increase"},
    "DECREASE_POSITION_SIZE": {"trade_size_decrease"},
    "CANCEL_PENDING_TRADE": {"order_closed_user"},
    "UPDATE_OPEN_ORDER": {"order_updated"},
    "UPDATE_TRADE": {"tp_updated", "sl_updated", "tpsl_updated", "leverage_updated"},
}


def _execution_rows(
    row: dict[str, Any], history: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep the history rows for the execution Keeper confirmed."""
    canonical_block = (
        row.get("execBlock") if row.get("lifecycleStep") == "exec_pass" else None
    )
    return [
        item
        for item in history
        if canonical_block is None
        or (item.get("block") or {}).get("block") == canonical_block
    ]


def _indexed(row: dict[str, Any], matching: list[dict[str, Any]]) -> dict[str, Any]:
    for item in matching:
        if item.get("tradeChangeType") == "position_liquidated":
            return item
    confirming = None
    for item in matching:
        change = item.get("tradeChangeType")
        if change is None or change == "after_hours_risk_trigger":
            continue
        if not _matches_action(row, item, change):
            return item
        if confirming is None:
            confirming = item
    return confirming or (matching[0] if matching else {})


def _action(row: dict[str, Any], indexed: dict[str, Any]) -> dict[str, Any]:
    kind = row.get("kind")
    body = row.get("body")
    order_type = (
        body.get("orderType")
        if kind == "OPEN_TRADE" and isinstance(body, dict)
        else None
    )
    if order_type not in ("market", "limit", "stop"):
        change = indexed.get("tradeChangeType")
        order_type = (
            PLACEMENTS.get(change)
            if isinstance(change, str) and kind in ("OPEN_TRADE", None)
            else None
        )
    return {"kind": kind, "orderType": order_type}


def _matches_action(row: dict[str, Any], indexed: dict[str, Any], change: str) -> bool:
    kind = row.get("kind")
    if kind == "OPEN_TRADE":
        return change == OPEN_CHANGES.get(
            _action(row, indexed)["orderType"], "position_opened"
        )
    if kind is None:
        return change in TRADE_FILLS or change in PLACEMENTS
    body = row.get("body")
    if kind == "UPDATE_TRADE" and isinstance(body, dict):
        tp = _requests_update(body.get("takeProfit"))
        sl = _requests_update(body.get("stopLoss"))
        return {
            "leverage_updated": body.get("requestedLeverage") is not None,
            "tp_updated": tp,
            "sl_updated": sl,
            "tpsl_updated": tp or sl,
        }.get(change, False)
    return isinstance(kind, str) and change in REQUESTED_CHANGES.get(kind, ())


def _requests_update(patch: object) -> bool:
    return isinstance(patch, dict) and patch.get("operation") in ("set", "clear")


def success(
    row: dict[str, Any], history: list[dict[str, Any]]
) -> Literal["filled", "placed", "succeeded", "liquidated", "unexpected"]:
    indexed = _indexed(row, _execution_rows(row, history))
    action = _action(row, indexed)
    change = indexed.get("tradeChangeType")
    if change == "position_liquidated":
        return "liquidated"
    if change:
        if not isinstance(change, str) or not _matches_action(row, indexed, change):
            return "unexpected"
        if change in PLACEMENTS:
            return "placed"
        return "filled" if change in TRADE_FILLS else "succeeded"
    if action["orderType"] in ("limit", "stop"):
        return "placed"
    return "succeeded"


def _number(value: object) -> int | float | str | None:
    if type(value) is int and -(2**63) <= value < 2**63:
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, str) and re.fullmatch(
        r"-?[0-9]{1,24}(?:\.[0-9]{1,24})?", value
    ):
        return value
    return None


def _position(row: dict[str, Any], public: dict[str, Any]) -> dict[str, Any] | None:
    trade = row.get("trade")
    if not isinstance(trade, dict):
        return None
    result = dict(public.get("trade", {}))
    for field in ("isOpen", "isLong"):
        if isinstance(value := trade.get(field), bool):
            result[field] = value
    if trade.get("tradeType") in ("trade", "limit", "stop"):
        result["tradeType"] = trade["tradeType"]
    for field in (
        "collateralAmount",
        "openCollateralAmount",
        "leverage",
        "openPrice",
        "closePrice",
        "tp",
        "sl",
    ):
        if field in trade:
            result[field] = _number(trade[field])
    for field in ("openBlock", "closeBlock"):
        if isinstance(block := trade.get(field), dict):
            result[field] = {"block": _number(block.get("block"))}
        elif field in trade:
            result[field] = None
    if isinstance(borrowing := trade.get("perpBorrowing"), dict):
        market: dict[str, Any] = {"marketId": _number(borrowing.get("marketId"))}
        result["perpBorrowing"] = market
        if isinstance(token := borrowing.get("collateralToken"), dict):
            symbol = token.get("symbol")
            if isinstance(symbol, str) and re.fullmatch(r"[A-Z0-9]{1,12}", symbol):
                market["collateralToken"] = {"symbol": symbol}
    return result


def build(
    outcome: str,
    row: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    request_tx_hash: str | None,
    request_block: int | None,
) -> dict[str, Any]:
    """Keep the request transaction distinct from execution and fill data."""
    matching = _execution_rows(row, history)
    indexed = _indexed(row, matching)
    public = keeper.safe_intent_history(indexed)
    intent = keeper.safe_intent_record(row)
    order = indexed.get("marketOrder")
    order = order if isinstance(order, dict) else {}
    block = public.get("block", {})
    change = public.get("tradeChangeType")
    if indexed.get("tradeChangeType") == "order_updated":
        change = "order_updated"
    execution = None
    if outcome in (
        "filled",
        "placed",
        "succeeded",
        "failed",
        "liquidated",
        "unexpected",
    ):
        execution = {
            "source": "trade_history" if indexed else "keeper_intent",
            "block": block.get("block")
            or _number(order.get("orderExecBlock"))
            or intent.get("execBlock"),
            "blockTs": block.get("block_ts")
            or (intent.get("execBlockTs") if not indexed else None),
            "historyId": public.get("id"),
            "tradeChangeType": change,
            "marketOrderSequence": _number(order.get("sequence")),
            "extraEffects": [
                {
                    "tradeChangeType": extra["tradeChangeType"],
                    "historyId": extra.get("id"),
                    "block": extra.get("block", {}).get("block"),
                }
                for item in matching
                if item is not indexed
                and item.get("tradeChangeType") == "after_hours_risk_trigger"
                for extra in [keeper.safe_intent_history(item)]
            ],
        }
    fill = None
    if outcome == "filled":
        # Keeper's history joins the current position, so its entry price and
        # collateral can change after this fill. The oracle quote and escrow
        # in the correlated market order describe the indexed execution.
        fill = {
            "price": None,
            "sizeCollateralBaseUnits": None,
            "oraclePrice": _number(order.get("execPrice")),
            "escrowedCollateralBaseUnits": _number(order.get("collateralAmount")),
        }
    fees = {
        "opening": _number(indexed.get("openingFeeUsd")),
        "closing": _number(indexed.get("closingFeeUsd")),
        "borrowing": _number(indexed.get("borrowingFeeUsd")),
    }
    return {
        "requestTxHash": keeper.request_hash(request_tx_hash)[0]
        or keeper.request_hash(order.get("orderReqTxHash"))[0]
        or public.get("txHash"),
        "requestBlock": _number(request_block) or _number(order.get("orderReqBlock")),
        "action": {**_action(row, indexed), "kind": intent.get("kind")},
        "execution": execution,
        "fill": fill,
        "positionNow": _position(indexed, public),
        "positionFeesUsd": fees
        if any(value is not None for value in fees.values())
        else None,
    }
