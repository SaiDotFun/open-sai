"""Build intent receipts from Keeper lifecycle and trade history evidence."""

from __future__ import annotations

from typing import Any, Literal

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


def _indexed(row: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    # A row confirming the canonical execution block takes precedence if the
    # same intent key happens to appear on multiple history rows.
    canonical_block = (
        row.get("execBlock") if row.get("lifecycleStep") == "exec_pass" else None
    )
    matching = [
        item
        for item in history
        if canonical_block is None
        or (item.get("block") or {}).get("block") == canonical_block
    ]
    for item in matching:
        if item.get("tradeChangeType") == "position_liquidated":
            return item
    for item in matching:
        change = item.get("tradeChangeType")
        if change and not _matches_action(row, item, change):
            return item
    return matching[0] if matching else {}


def _action(row: dict[str, Any], indexed: dict[str, Any]) -> dict[str, Any]:
    kind = row.get("kind")
    body = row.get("body")
    order_type = (
        body.get("orderType")
        if kind == "OPEN_TRADE" and isinstance(body, dict)
        else None
    )
    if order_type is None and kind in ("OPEN_TRADE", None):
        order_type = PLACEMENTS.get(indexed.get("tradeChangeType"))
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
    return change in REQUESTED_CHANGES.get(kind, ())


def _requests_update(patch: object) -> bool:
    return isinstance(patch, dict) and patch.get("operation") in ("set", "clear")


def success(
    row: dict[str, Any], history: list[dict[str, Any]]
) -> Literal["filled", "placed", "succeeded", "liquidated", "unexpected"]:
    indexed = _indexed(row, history)
    action = _action(row, indexed)
    change = indexed.get("tradeChangeType")
    if change == "position_liquidated":
        return "liquidated"
    if change:
        if not _matches_action(row, indexed, change):
            return "unexpected"
        if change in PLACEMENTS:
            return "placed"
        return "filled" if change in TRADE_FILLS else "succeeded"
    if action["orderType"] in ("limit", "stop"):
        return "placed"
    return "succeeded"


def build(
    outcome: str,
    row: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    request_tx_hash: str | None,
    request_block: int | None,
) -> dict[str, Any]:
    """Keep the request transaction distinct from execution and fill data."""
    indexed = _indexed(row, history)
    order = indexed.get("marketOrder") or {}
    block = indexed.get("block") or {}
    change = indexed.get("tradeChangeType")
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
            or order.get("orderExecBlock")
            or row.get("execBlock"),
            "blockTs": block.get("block_ts")
            or (row.get("execBlockTs") if not indexed else None),
            "historyId": indexed.get("id"),
            "tradeChangeType": change,
            "marketOrderSequence": order.get("sequence"),
        }
    fill = None
    if outcome == "filled":
        # Keeper's history joins the current position, so its entry price and
        # collateral can change after this fill. The oracle quote and escrow
        # in the correlated market order describe the indexed execution.
        fill = {
            "price": None,
            "sizeCollateralBaseUnits": None,
            "oraclePrice": order.get("execPrice"),
            "escrowedCollateralBaseUnits": order.get("collateralAmount"),
        }
    fees = {
        "opening": indexed.get("openingFeeUsd"),
        "closing": indexed.get("closingFeeUsd"),
        "borrowing": indexed.get("borrowingFeeUsd"),
    }
    return {
        "requestTxHash": request_tx_hash
        or order.get("orderReqTxHash")
        or indexed.get("txHash"),
        "requestBlock": request_block or order.get("orderReqBlock"),
        "action": _action(row, indexed),
        "execution": execution,
        "fill": fill,
        "positionNow": indexed.get("trade"),
        "positionFeesUsd": fees
        if any(value is not None for value in fees.values())
        else None,
    }
