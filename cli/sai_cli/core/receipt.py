"""Build intent receipts from Keeper lifecycle and trade history evidence."""

from __future__ import annotations

import math
import re
from fractions import Fraction
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
FillValues = tuple[int | float | str | None, int | None]


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


def _block(value: object) -> int | None:
    block = value.get("block") if isinstance(value, dict) else None
    return block if type(block) is int and 0 < block < 2**63 else None


def opening_fill_position(
    row: dict[str, Any], history: list[dict[str, Any]]
) -> tuple[int, int] | None:
    """Identify a position opened by a correlated end-block market order."""
    indexed = _indexed(row, _execution_rows(row, history))
    if indexed.get("tradeChangeType") != "position_opened":
        return None
    block = _block(indexed.get("block"))
    order, trade = indexed.get("marketOrder"), indexed.get("trade")
    if block is None or not isinstance(order, dict) or not isinstance(trade, dict):
        return None
    trade_id = trade.get("id")
    order_block = order.get("orderExecBlock")
    if (
        type(trade_id) is not int
        or not 0 <= trade_id < 2**63
        or type(order_block) is not int
        or order_block != block
        or _block(trade.get("openBlock")) != block
    ):
        return None
    # Opening collateral cannot exceed the amount originally escrowed.
    collateral = trade.get("openCollateralAmount")
    escrow = order.get("collateralAmount")
    if type(collateral) is int and type(escrow) is int and collateral > escrow:
        return None
    return trade_id, block


def opening_fill(
    data: dict[str, Any], trade_id: int, block: int, account: str, row: dict[str, Any]
) -> FillValues:
    perp = data.get("perp")
    trade = perp.get("trade") if isinstance(perp, dict) else None
    # Ordinary transactions finish before the end-block market fill. A new
    # position still at its opening update therefore describes that fill.
    if (
        not isinstance(trade, dict)
        or type(trade.get("id")) is not int
        or trade.get("id") != trade_id
        or keeper.canonical_account(trade.get("trader")) != account
        or trade.get("tradeType") != "trade"
        or trade.get("isOpen") is not True
        or _block(trade.get("openBlock")) != block
        or _block(trade.get("lastUpdatedBlock")) != block
    ):
        return None, None
    price = _number(trade.get("openPrice"))
    if price is not None and Fraction(str(price)) <= 0:
        price = None
    collateral = trade.get("collateralAmount")
    leverage = _number(trade.get("leverage"))
    body = row.get("body")
    requested = _number(body.get("leverage")) if isinstance(body, dict) else None
    if (
        type(collateral) is not int
        or not 0 < collateral < 2**63
        or leverage is None
        or not isinstance(requested, str)
        or float(requested) != float(leverage)
    ):
        return price, None
    # The intent preserves the exact decimal. GraphQL Float rounding could
    # otherwise change the size when flooring to whole collateral base units.
    size = int(collateral * Fraction(requested))
    return price, size if 0 < size < 2**63 else None


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
    fill_values: FillValues = (None, None),
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
        fill = {
            "price": fill_values[0],
            "sizeCollateralBaseUnits": fill_values[1],
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
