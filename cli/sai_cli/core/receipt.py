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
FILL_KINDS = {"CLOSE_POSITION", "INCREASE_POSITION_SIZE", "DECREASE_POSITION_SIZE"}


def _indexed(row: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    # A row confirming the canonical execution block takes precedence if the
    # same intent key happens to appear on multiple history rows.
    canonical_block = (
        row.get("execBlock") if row.get("lifecycleStep") == "exec_pass" else None
    )
    return next(
        (
            item
            for item in history
            if canonical_block is None
            or (item.get("block") or {}).get("block") == canonical_block
        ),
        {},
    )


def _action(row: dict[str, Any], indexed: dict[str, Any]) -> dict[str, Any]:
    kind = row.get("kind")
    body = row.get("body")
    order_type = (
        body.get("orderType")
        if kind == "OPEN_TRADE" and isinstance(body, dict)
        else None
    )
    return {
        "kind": kind,
        "orderType": order_type or PLACEMENTS.get(indexed.get("tradeChangeType")),
    }


def success(
    row: dict[str, Any], history: list[dict[str, Any]], request_block: int | None
) -> Literal["filled", "placed", "succeeded"]:
    indexed = _indexed(row, history)
    action = _action(row, indexed)
    if action["orderType"] in ("limit", "stop"):
        return "placed"
    if (
        indexed.get("tradeChangeType") in TRADE_FILLS
        or action["kind"] in FILL_KINDS
        or action["orderType"] == "market"
        or (action["kind"] == "OPEN_TRADE" and request_block is not None)
    ):
        return "filled"
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
    if outcome in ("filled", "placed", "succeeded", "failed"):
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
