"""Perp contract inputs and rounding for a partial-close notional quote."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import intents, margin


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise TypeError("Expected a contract decimal string")
    return (
        Decimal(0) if value == "0" else intents.positive(value, "Contract quote input")
    )


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
    market_index = raw["market_index"]
    collateral_index = raw["collateral_index"]
    inputs = {"marketIndex": market_index, "collateralIndex": collateral_index}
    effective_leverage: Decimal | None = None
    try:
        effective_leverage = _effective_leverage(
            lcd, state, trade_id, stored_leverage, inputs
        )
    except (ValueError, TypeError, KeyError):
        # Older deployments or unavailable queries leave the leverage unknown.
        pass

    try:
        price = _decimal(
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
        "effectiveLeverage": str(effective_leverage)
        if effective_leverage is not None
        else None,
        "collateralPriceUsd": str(price) if price is not None else None,
        "closedNotionalUsdc": _format_micros(size),
        "closedNotionalUsd": _format_micros(_floor_product(size, price))
        if price is not None
        else None,
        "closedNotionalIsEstimate": effective_leverage is None or price is None,
    }


def _effective_leverage(
    lcd: str,
    state: margin.PositionSnapshot,
    trade_id: int,
    stored: Decimal,
    inputs: dict[str, Any],
) -> Decimal:
    """Mirror Perp's get_effective_leverage for an open, filled position."""
    market_index = inputs["marketIndex"]
    enabled = margin.smart_value(
        lcd, state.contract, {"is_after_hours_market": {"market_index": market_index}}
    )
    if not isinstance(enabled, bool):
        raise TypeError("Expected the contract's after-hours flag")
    inputs["afterHoursMarket"] = enabled
    if not enabled:
        return stored

    market = margin.smart(lcd, state.contract, {"get_market": {"index": market_index}})
    opened, closed = market["block_last_opened"], market["block_last_closed"]
    if any(value is not None and type(value) is not int for value in (opened, closed)):
        raise ValueError("Expected the market's session blocks")
    inputs.update(marketLastOpenedBlock=opened, marketLastClosedBlock=closed)
    if opened is None or closed is None:
        return stored
    if opened > closed:
        infos = margin.smart_value(
            lcd,
            state.contract,
            {
                "get_trade_infos": {
                    "trader": state.raw["user"],
                    "start_idx": trade_id,
                    "scan_limit": 1,
                }
            },
        )
        if (
            not isinstance(infos, list)
            or len(infos) != 1
            or not isinstance(infos[0], dict)
        ):
            raise ValueError("Expected one trade info record")
        created = infos[0]["created_block"]
        if type(created) is not int:
            raise ValueError("Expected the trade's last risk-modifying block")
        inputs["positionRiskBlock"] = created
        if created >= opened:
            return stored

    cap = _decimal(
        margin.smart_value(lcd, state.contract, {"get_after_hours_leverage": {}})
    )
    inputs["afterHoursLeverageCap"] = str(cap)
    return min(stored, cap)
