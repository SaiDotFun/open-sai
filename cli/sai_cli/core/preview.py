"""Read-only estimates derived from Sai Perp's execution rules."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, DecimalException, localcontext
from typing import Any

from . import intents, keeper, margin

QUOTE_TIMEOUT = 3
ATOM = Decimal("1e-18")
LIQUIDATION_THRESHOLD = Decimal("0.90")
LIQUIDATION_FEE_RATE = Decimal("0.05")
READ_ERRORS = (ValueError, TypeError, KeyError, DecimalException, keeper.KeeperError)


def unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def not_applicable(reason: str) -> dict[str, Any]:
    return {"status": "not_applicable", "reason": reason}


def price_input(price: Decimal, source: str, observed_at: str | None) -> dict[str, Any]:
    return {"price": format(price, "f"), "source": source, "observedAt": observed_at}


def market_price(row: dict[str, Any]) -> Decimal:
    price = format(Decimal(str(row["price"])), "f")
    return intents.positive(price, "Current price")


def slippage_price(price: Decimal, slippage: Decimal, long: bool) -> Decimal:
    """Perp rounds the slippage distance down before applying it."""
    with localcontext() as context:
        context.prec = 90
        distance = (price * slippage).quantize(ATOM, rounding=ROUND_DOWN)
        return price + distance if long else price - distance


def fill_range(
    order_type: str,
    long: bool,
    slippage: Decimal,
    reference: dict[str, Any] | None,
    *,
    mcp_market: bool,
) -> dict[str, Any]:
    if reference is None:
        return unavailable("Keeper market price is unavailable.")
    price = Decimal(reference["price"])
    steps = 2 if order_type == "market" and mcp_market else 1
    for _ in range(steps):
        price = slippage_price(price, slippage, long)
    price_text = format(price, "f")
    return {
        "status": "estimate" if order_type == "market" and not mcp_market else "bound",
        "price": price_text.rstrip("0").rstrip(".")
        if "." in price_text
        else price_text,
        "side": "maximum" if long else "minimum",
        "reference": reference,
    }


def liquidation(
    entry: dict[str, Any], long: bool, collateral: int, leverage: Decimal
) -> dict[str, Any]:
    """Apply Perp's liquidation formula before accrued borrowing fees."""
    if collateral <= 0 or leverage <= 0:
        return unavailable("The estimated remaining collateral or leverage is zero.")
    with localcontext() as context:
        context.prec = 90
        price = Decimal(entry["price"])
        # Perp floors each of the two 5% liquidation fee components.
        fee = 2 * int(collateral * LIQUIDATION_FEE_RATE)
        price_per_unit = (price / collateral).quantize(ATOM, rounding=ROUND_DOWN)
        price_per_unit = (price_per_unit / leverage).quantize(ATOM, rounding=ROUND_DOWN)
        distance = (
            price_per_unit * (collateral * LIQUIDATION_THRESHOLD - fee)
        ).quantize(ATOM, rounding=ROUND_DOWN)
        price = max(Decimal(0), price - distance if long else price + distance)
        return {
            "status": "estimate",
            "price": format(price.normalize(), "f"),
            "entry": entry,
            "excludesBorrowingFees": True,
        }


def base_fees(
    row: dict[str, Any],
    collateral_price: dict[str, Any],
    notional: int,
    *,
    opening: bool,
    triggered: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Perp's minimum fee basis and undiscounted execution fee components."""
    with localcontext() as context:
        context.prec = 90
        open_rate = margin.contract_decimal(row["open_fee_p"])
        close_rate = margin.contract_decimal(row["close_fee_p"])
        trigger_rate = margin.contract_decimal(row["trigger_order_fee_p"])
        minimum_usd = int(row["min_position_size_usd"])
        if minimum_usd < 0 or notional <= 0:
            raise ValueError("Invalid fee basis")
        minimum_fee = int(minimum_usd * (2 * open_rate + trigger_rate))
        basis = max(notional, int(minimum_fee / Decimal(collateral_price["price"])))
        fee = int(basis * (open_rate if opening else close_rate))
        if triggered:
            fee += int(basis * trigger_rate)
        return fee, {
            "status": "estimate",
            "amountUsdc": format(Decimal(fee).scaleb(-6).normalize(), "f"),
            "basis": "Undiscounted execution fees using Perp's contract fee schedule.",
            "collateralPriceUsd": collateral_price,
            "excludes": (
                ["Trader discounts", "absolute exposure fees", "network fees"]
                if opening
                else [
                    "Trader discounts",
                    "borrowing fees",
                    "absolute exposure fees",
                    "profit fees",
                    "short-hold penalties and fee clawbacks",
                    "network fees",
                ]
            ),
        }


def read_collateral_price(lcd: str, contract: str, token: str) -> dict[str, Any]:
    price = margin.contract_decimal(
        margin.smart_value(
            lcd,
            contract,
            {"get_collateral_price": {"collateral_index": token}},
        )
    )
    if price <= 0:
        raise ValueError("Invalid collateral oracle price")
    return price_input(price, "Sai Perp collateral oracle", intents.now())


def fee_schedule(lcd: str, contract: str, market: str) -> dict[str, Any]:
    pair = margin.smart(lcd, contract, {"get_market": {"index": market}})
    return margin.smart(lcd, contract, {"get_fees": {"index": pair["fee_index"]}})


def _notional(collateral: int, leverage: Decimal) -> int:
    numerator, denominator = leverage.as_integer_ratio()
    return collateral * numerator // denominator


def open_estimates(
    endpoint: str,
    lcd: str | None,
    body: dict[str, Any],
    row: dict[str, Any] | None,
    market_reference: dict[str, Any] | None,
    *,
    mcp_market: bool,
) -> dict[str, Any]:
    order_type, long = body["orderType"], body["side"] == "long"
    reference = market_reference
    if order_type != "market":
        reference = price_input(Decimal(body["intentPrice"]), "Order trigger", None)
    elif not mcp_market and "intentPrice" in body:
        reference = price_input(Decimal(body["intentPrice"]), "CLI --price", None)

    fill = fill_range(
        order_type,
        long,
        Decimal(body["maxSlippagePct"]).scaleb(-2),
        reference,
        mcp_market=mcp_market,
    )
    fees = unavailable("Perp fee schedule or collateral oracle price is unavailable.")
    liq = unavailable("An entry price and opening fee estimate are required.")
    if lcd and row is not None:
        try:
            token_id = row["collateralToken"]["id"]
            contract = margin.trading_contract(endpoint, lcd)
            schedule = fee_schedule(lcd, contract, body["market"]["marketIndex"])
            token = f"TokenIndex({token_id})"
            collateral_price = read_collateral_price(lcd, contract, token)
            collateral = int(Decimal(body["amountUsd"]).scaleb(6))
            leverage = Decimal(body["leverage"])
            fee, fees = base_fees(
                schedule,
                collateral_price,
                _notional(collateral, leverage),
                opening=True,
                triggered=order_type != "market",
            )
            if reference is not None:
                liq = liquidation(reference, long, collateral - fee, leverage)
                if liq["status"] == "estimate":
                    liq["assumption"] = (
                        "Entry at the reference price with only the estimated execution fees deducted."
                    )
        except READ_ERRORS:
            pass
    return {"fees": fees, "liquidationPrice": liq, "worstCaseFill": fill}


def position_estimates(
    lcd: str,
    state: margin.PositionSnapshot | None,
    effective: Decimal | None,
    *,
    close: bool = False,
    reduce_by: int = 0,
    target: Decimal | None = None,
    updates_triggers: bool = False,
) -> dict[str, Any]:
    """Quote a close or a surviving position from the same snapshot as the intent."""
    if target is not None or updates_triggers:
        unknown_guard = unavailable(
            "Cannot verify that the Perp after-hours guard is inactive."
        )
        result = dict.fromkeys(
            ("fees", "liquidationPrice", "worstCaseFill"), unknown_guard
        )
    else:
        result = {
            "fees": unavailable("Current position or Perp fee inputs are unavailable."),
            "liquidationPrice": not_applicable("A full close leaves no position.")
            if close
            else unavailable("A current position and effective leverage are required."),
            "worstCaseFill": {
                "status": "unbounded",
                "reason": "This Keeper intent carries no slippage limit.",
            },
        }
    if state is None or effective is None:
        return result
    try:
        raw = state.raw
        stored = margin.contract_decimal(raw["leverage"])
        if effective != stored:
            risk = unavailable(
                "The after-hours guard can change or close this position."
            )
            return dict.fromkeys(result, risk)
        if target is not None or updates_triggers:
            result["fees"] = {
                "status": "exact",
                "amountUsdc": "0",
                "excludes": ["Network fees"],
            }
            result["worstCaseFill"] = not_applicable("This action has no price fill.")
        if updates_triggers:
            result["liquidationPrice"] = {
                "status": "unchanged",
                "reason": "This action changes only TP/SL triggers.",
            }
            return result
        collateral = int(raw["collateral_amount"])
        if target is None:
            try:
                schedule = fee_schedule(lcd, state.contract, raw["market_index"])
                price = read_collateral_price(
                    lcd, state.contract, raw["collateral_index"]
                )
                _, result["fees"] = base_fees(
                    schedule,
                    price,
                    _notional(collateral if close else reduce_by, effective),
                    opening=False,
                )
            except READ_ERRORS:
                pass
        if close:
            return result

        entry = price_input(
            intents.positive(raw["open_price"], "Entry price"),
            "Sai Perp stored entry price",
            state.observed_at,
        )
        long = raw["long"]
        if type(long) is not bool:
            return result
        with localcontext() as context:
            context.prec = 90
            if target is not None:
                remaining = int(collateral * effective / target)
                leverage = target
                assumption = (
                    "The target leverage takes effect at the quoted collateral."
                )
            else:
                remaining = collateral - reduce_by
                remaining_size = _notional(collateral, effective) - _notional(
                    reduce_by, effective
                )
                leverage = (Decimal(remaining_size) / remaining).quantize(
                    ATOM, rounding=ROUND_DOWN
                )
                assumption = "The close payout covers its own losses and fees."
            liq = liquidation(entry, long, remaining, leverage)
            if liq["status"] == "estimate":
                liq["assumption"] = assumption
            result["liquidationPrice"] = liq
    except READ_ERRORS:
        pass
    return result
