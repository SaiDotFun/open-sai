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
    lcd: str,
    state: margin.PositionSnapshot,
    collateral_delta: int,
    effective_leverage: Decimal | None,
    *,
    fixed_usdc_price: bool = False,
) -> dict[str, Any]:
    """Quote the requested slice with the Perp after-hours rule."""
    raw = state.raw
    stored_leverage = Decimal(str(raw["leverage"]))
    price: Decimal | None = Decimal(1)
    if not (fixed_usdc_price and state.usdc_collateral):
        try:
            price = margin.collateral_price(lcd, state)
        except (ValueError, TypeError):
            price = None

    # Perp floors in collateral units first, then again in USD units.
    leverage = stored_leverage if effective_leverage is None else effective_leverage
    size = _floor_product(collateral_delta, leverage)
    return {
        "collateralPriceUsd": str(price) if price is not None else None,
        "closedNotionalUsdc": _format_micros(size),
        "closedNotionalUsd": _format_micros(_floor_product(size, price))
        if price is not None
        else None,
        "closedNotionalIsEstimate": effective_leverage is None or price is None,
    }
