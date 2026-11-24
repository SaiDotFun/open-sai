"""Build intent receipts from Keeper lifecycle and trade history evidence."""

from __future__ import annotations

from typing import Any

TRADE_FILLS = {
    "position_opened",
    "position_closed_user",
    "trade_size_increase",
    "trade_size_decrease",
}


def build(
    outcome: str,
    row: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    request_tx_hash: str | None,
    request_block: int | None,
) -> dict[str, Any]:
    """Keep the request transaction distinct from execution and fill data."""
    # A row confirming the canonical execution block takes precedence if the
    # same intent key happens to appear on multiple history rows.
    canonical_block = (
        row.get("execBlock") if row.get("lifecycleStep") == "exec_pass" else None
    )
    indexed = next(
        (
            item
            for item in history
            if canonical_block is None
            or (item.get("block") or {}).get("block") == canonical_block
        ),
        {},
    )
    order = indexed.get("marketOrder") or {}
    block = indexed.get("block") or {}
    change = indexed.get("tradeChangeType")
    position = indexed.get("trade")
    execution = None
    if outcome in ("filled", "failed"):
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
    if outcome == "filled" and (
        change in TRADE_FILLS or order or (not indexed and request_block)
    ):
        fill = _fill(change, position or {}, order, indexed)
    return {
        "requestTxHash": request_tx_hash
        or order.get("orderReqTxHash")
        or indexed.get("txHash"),
        "requestBlock": request_block or order.get("orderReqBlock"),
        "execution": execution,
        "fill": fill,
        "position": position,
    }


def _fill(
    change: str | None,
    position: dict[str, Any],
    order: dict[str, Any],
    history: dict[str, Any],
) -> dict[str, Any]:
    # The indexed trade is the latest position. Only use its entry or exit
    # price when the matching action is the one that recorded that price.
    block = (history.get("block") or {}).get("block")
    price = None
    size = None
    if (
        change == "position_opened"
        and block is not None
        and block == (position.get("openBlock") or {}).get("block")
    ):
        price = position.get("openPrice")
        size = position.get("openCollateralAmount")
    if (
        change == "position_closed_user"
        and block is not None
        and block == (position.get("closeBlock") or {}).get("block")
    ):
        price = position.get("closePrice")
        size = position.get("collateralAmount")
    fees = {
        "opening": history.get("openingFeeUsd"),
        "closing": history.get("closingFeeUsd"),
        "borrowing": history.get("borrowingFeeUsd"),
    }
    return {
        "price": price,
        "oraclePrice": order.get("execPrice"),
        "sizeCollateralBaseUnits": size,
        "escrowedCollateralBaseUnits": order.get("collateralAmount"),
        "positionFeesUsd": fees
        if any(value is not None for value in fees.values())
        else None,
    }
