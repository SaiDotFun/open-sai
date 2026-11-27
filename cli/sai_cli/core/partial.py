"""Perp contract inputs and rounding for a partial-close notional quote."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import margin


def _floor_product(amount: int, multiplier: Decimal) -> int:
    numerator, denominator = multiplier.as_integer_ratio()
    return amount * numerator // denominator


def _format_micros(amount: int) -> str:
    whole, micros = divmod(amount, 1_000_000)
    return f"{whole}.{micros:06d}".rstrip("0").rstrip(".")


def notional_quote(
    state: margin.PositionSnapshot,
    collateral_delta: int,
    effective_leverage: Decimal | None,
) -> dict[str, Any]:
    """Quote the requested slice with the Perp after-hours rule."""
    raw = state.raw
    stored_leverage = Decimal(str(raw["leverage"]))
    # USDC converts to USD at exactly one dollar per token.
    leverage = stored_leverage if effective_leverage is None else effective_leverage
    size = _format_micros(_floor_product(collateral_delta, leverage))
    return {
        "collateralPriceUsd": "1",
        "closedNotionalUsdc": size,
        "closedNotionalUsd": size,
        "closedNotionalIsEstimate": effective_leverage is None,
    }
