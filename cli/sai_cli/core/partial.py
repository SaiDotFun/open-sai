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
    lcd: str, state: margin.PositionSnapshot, trade_id: int, collateral_delta: int
) -> dict[str, Any]:
    """Quote the requested slice with the Perp after-hours rule and oracle."""
    raw = state.raw
    stored_leverage = Decimal(str(raw["leverage"]))
    collateral_index = raw["collateral_index"]
    effective_leverage, inputs = margin.leverage_inputs(lcd, state, trade_id)

    try:
        price = margin.contract_decimal(
            margin.smart_value(
                lcd,
                state.contract,
                {"get_collateral_price": {"collateral_index": collateral_index}},
            )
        )
    except (ValueError, TypeError):
        price = None

    # Perp floors in collateral units first, then again in USD units.
    leverage = stored_leverage if effective_leverage is None else effective_leverage
    size = _floor_product(collateral_delta, leverage)
    return {
        **inputs,
        "collateralPriceUsd": str(price) if price is not None else None,
        "closedNotionalUsdc": _format_micros(size),
        "closedNotionalUsd": _format_micros(_floor_product(size, price))
        if price is not None
        else None,
        "closedNotionalIsEstimate": effective_leverage is None or price is None,
    }
