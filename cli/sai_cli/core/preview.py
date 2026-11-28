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
UNKNOWN_ORACLE_TIME = "OraclePublicationTimeUnknown"
ESTIMATE_FIELDS = ("fees", "liquidationPrice", "worstCaseFill")


def unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def close_oracle_time_unknown(estimates: dict[str, Any]) -> bool:
    return estimates.get("execution", {}).get("code") == UNKNOWN_ORACLE_TIME


def rejected(code: str, reason: str) -> dict[str, Any]:
    return {
        **dict.fromkeys(ESTIMATE_FIELDS, unavailable(reason)),
        "execution": {"status": "would_be_rejected", "code": code, "reason": reason},
    }


def rejection_reason(estimates: dict[str, Any]) -> str | None:
    execution = estimates.get("execution", {})
    return (
        execution["reason"] if execution.get("status") == "would_be_rejected" else None
    )


def market_open_rejection(
    pair: dict[str, Any], *, market_order: bool
) -> dict[str, Any] | None:
    """Check market state for opens and leverage updates."""
    state = pair.get("market_trading_state")
    if state == "close_only":
        return rejected(
            "MarketClosed", "Perp rejects this request in a close-only market."
        )
    if state == "closed" and market_order:
        return rejected("MarketClosed", "Perp rejects this request in a closed market.")
    return None


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


def market_price_move(
    reference: Decimal, current: Decimal, slippage: str
) -> str | None:
    """Return the percentage moved if the refreshed price exceeds the quote tolerance."""
    fraction = Decimal(slippage).scaleb(-2)
    lower = slippage_price(reference, fraction, False)
    upper = slippage_price(reference, fraction, True)
    if lower <= current <= upper:
        return None
    with localcontext() as context:
        context.prec = 90
        moved = abs(current - reference) / reference * 100
        return format(moved.quantize(Decimal("0.0001")), "f")


def fill_range(
    order_type: str,
    long: bool,
    slippage: Decimal,
    reference: dict[str, Any] | None,
    *,
    market_bound: bool,
) -> dict[str, Any]:
    if reference is None:
        return unavailable("Keeper market price is unavailable.")
    price = Decimal(reference["price"])
    steps = 2 if order_type == "market" and market_bound else 1
    for _ in range(steps):
        price = slippage_price(price, slippage, long)
    price_text = format(price, "f")
    return {
        "status": "estimate"
        if order_type == "market" and not market_bound
        else "bound",
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


def minimum_fee_usd(schedule: dict[str, Any]) -> int:
    """Floor Perp's USD minimum fee to base units."""
    minimum = int(schedule["min_position_size_usd"])
    if minimum < 0:
        raise ValueError("Invalid minimum position size")
    opening = margin.contract_decimal(schedule["open_fee_p"])
    trigger = margin.contract_decimal(schedule["trigger_order_fee_p"])
    return int(minimum * (2 * opening + trigger))


def leverage_allowed(
    lcd: str,
    contract: str,
    pair: dict[str, Any],
    market_id: int,
    requested: Decimal,
    *,
    current: Decimal | None = None,
    height: str | None = None,
) -> bool:
    """Apply Perp's group limits and any nonzero pair override."""
    group = contract_group(lcd, contract, pair, height=height)
    minimum = margin.contract_decimal(group["min_leverage"])
    maximum = margin.contract_decimal(group["max_leverage"])
    custom = margin.pair_leverage_cap(lcd, contract, market_id, height=height)
    if custom:
        maximum = min(maximum, custom)
    return (
        requested != current
        and requested <= maximum
        and (requested >= minimum or (current is not None and requested > current))
    )


def base_fees(
    row: dict[str, Any],
    collateral_price: dict[str, Any],
    notional: int,
    *,
    opening: bool,
    triggered: bool = False,
    available_collateral: int | None = None,
) -> tuple[int, dict[str, Any]]:
    """Perp's minimum fee basis and undiscounted execution fee components."""
    with localcontext() as context:
        context.prec = 90
        open_rate = margin.contract_decimal(row["open_fee_p"])
        close_rate = margin.contract_decimal(row["close_fee_p"])
        trigger_rate = margin.contract_decimal(row["trigger_order_fee_p"])
        if notional <= 0:
            raise ValueError("Invalid fee basis")
        basis = max(
            notional, int(minimum_fee_usd(row) / Decimal(collateral_price["price"]))
        )
        fee = int(basis * (open_rate if opening else close_rate))
        if triggered:
            fee += int(basis * trigger_rate)
        if available_collateral is not None:
            fee = min(fee, available_collateral)
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


def contract_market(
    lcd: str,
    contract: str,
    market: str,
    *,
    height: str | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    pair = margin.smart_value(
        lcd, contract, {"get_market": {"index": market}}, height=height, fresh=fresh
    )
    if not isinstance(pair, dict):
        raise TypeError("Perp market is unavailable")
    return pair


def contract_group(
    lcd: str, contract: str, pair: dict[str, Any], *, height: str | None = None
) -> dict[str, Any]:
    group = margin.smart_value(
        lcd, contract, {"get_group": {"index": pair["group_index"]}}, height=height
    )
    if not isinstance(group, dict):
        raise TypeError("Perp market group is unavailable")
    return group


def fee_schedule(
    lcd: str, contract: str, pair: dict[str, Any], *, height: str | None = None
) -> dict[str, Any]:
    fees = margin.smart_value(
        lcd, contract, {"get_fees": {"index": pair["fee_index"]}}, height=height
    )
    if not isinstance(fees, dict):
        raise TypeError("Perp fee schedule is unavailable")
    return fees


def _notional(collateral: int, leverage: Decimal) -> int:
    numerator, denominator = leverage.as_integer_ratio()
    return collateral * numerator // denominator


def open_estimates(
    lcd: str | None,
    contract: str | None,
    body: dict[str, Any],
    row: dict[str, Any] | None,
    market_reference: dict[str, Any] | None,
    *,
    market_bound: bool,
    fresh_market: bool = False,
) -> dict[str, Any]:
    order_type, long = body["orderType"], body["side"] == "long"
    reference = market_reference
    if order_type != "market":
        reference = price_input(Decimal(body["intentPrice"]), "Order trigger", None)

    fill = fill_range(
        order_type,
        long,
        Decimal(body["maxSlippagePct"]).scaleb(-2),
        reference,
        market_bound=market_bound,
    )
    fees = unavailable("Perp fee schedule or collateral oracle price is unavailable.")
    liq = unavailable("An entry price and opening fee estimate are required.")
    if lcd and contract:
        try:
            pair = contract_market(
                lcd, contract, body["market"]["marketIndex"], fresh=fresh_market
            )
            if refusal := market_open_rejection(
                pair, market_order=order_type == "market"
            ):
                return refusal
            if row is None:
                return {"fees": fees, "liquidationPrice": liq, "worstCaseFill": fill}
            token_id = row["collateralToken"]["id"]
            leverage = Decimal(body["leverage"])
            within_leverage = leverage_allowed(
                lcd, contract, pair, row["marketId"], leverage
            )
            schedule = fee_schedule(lcd, contract, pair)
            token = f"TokenIndex({token_id})"
            collateral_price = read_collateral_price(lcd, contract, token)
            collateral = int(Decimal(body["amountUsd"]).scaleb(6))
            notional = _notional(collateral, leverage)
            with localcontext() as context:
                context.prec = 90
                # Trade.validate floors USD notional before dividing by leverage.
                collateral_value = int(
                    int(notional * Decimal(collateral_price["price"])) / leverage
                )
                allowed = within_leverage and collateral_value >= 5 * minimum_fee_usd(
                    schedule
                )
            if not allowed:
                outside = unavailable(
                    "The open request is outside the market's collateral or leverage limits."
                )
                return {
                    "fees": outside,
                    "liquidationPrice": outside,
                    "worstCaseFill": fill,
                }
            fee, fees = base_fees(
                schedule,
                collateral_price,
                notional,
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


def fills_at_next_oracle_price(lcd: str, contract: str, height: str) -> bool:
    config = margin.smart_value(
        lcd, contract, {"market_order_config": {}}, height=height
    )
    queued = config["fill_at_next_oracle_price"]
    if type(queued) is not bool:
        raise TypeError("Invalid Perp market-order config")
    return queued


def oracle_publication_time(oracle: dict[str, Any]) -> int | None:
    times = [
        time
        for key in ("base_last_update_time", "quote_last_update_time")
        if (time := oracle.get(key)) is not None
    ]
    if any(type(time) is not int or time < 0 for time in times):
        raise ValueError("Invalid Perp oracle publication time")
    return min(times, default=None)


def stale_oracle(
    lcd: str, contract: str, pair: dict[str, Any], height: str, published_at: int
) -> bool:
    group = contract_group(lcd, contract, pair, height=height)
    ttl = group["stale_price_expiration_secs"]
    if type(ttl) is not int or ttl < 0:
        raise ValueError("Invalid Perp oracle TTL")
    return ttl > 0 and margin.block_timestamp(lcd, height) - published_at > ttl


def past_liquidation(price: Decimal, threshold: Decimal, long: bool) -> bool:
    return price <= threshold if long else price >= threshold


def stored_liquidation_price(
    lcd: str, state: margin.PositionSnapshot, height: str
) -> Decimal:
    threshold = margin.smart_value(
        lcd,
        state.contract,
        {
            "get_liquidation_price": {
                "trade_id": state.raw["user_trade_index"],
                "trader": state.raw["user"],
                "include_borrowing_fees": True,
            }
        },
        height=height,
    )
    return margin.contract_decimal(threshold["liquidation_price"])


def close_estimates(
    lcd: str,
    state: margin.PositionSnapshot | None,
    trade_id: int,
    reduce_by: int | None,
    *,
    require_unchanged: bool = False,
    rechecked: margin.RecheckedPosition | None = None,
) -> dict[str, Any]:
    """Check Perp's liquidation route before quoting the voluntary fee components."""
    unknown = unavailable(
        "Cannot verify the position and Perp liquidation inputs at one block."
    )
    result = {
        "execution": unknown,
        "fees": unknown,
        "liquidationPrice": not_applicable("A full close leaves no position.")
        if reduce_by is None
        else unknown,
        "worstCaseFill": {
            "status": "unbounded",
            "reason": "This Keeper intent carries no slippage limit.",
        },
    }
    if state is None or type(long := state.raw.get("long")) is not bool:
        return result

    raw = state.raw
    try:
        try:
            height = (
                margin.verified_position_height(lcd, state, trade_id)
                if rechecked is None
                else rechecked.height
            )
            prices = (
                margin.perp_prices(lcd, state, height)
                if rechecked is None
                else rechecked.close_prices
            )
            oracle = prices.get("market") if isinstance(prices, dict) else None
            published_at = (
                oracle_publication_time(oracle) if isinstance(oracle, dict) else None
            )
        except margin.PositionChanged:
            raise
        except READ_ERRORS:
            prices, oracle, published_at = None, None, None
        if (
            not isinstance(prices, dict)
            or not isinstance(oracle, dict)
            or published_at is None
        ):
            missing_time = unavailable("The oracle price publication time is unknown.")
            return {
                **dict.fromkeys(result, missing_time),
                "execution": {**missing_time, "code": UNKNOWN_ORACLE_TIME},
            }
        if fills_at_next_oracle_price(lcd, state.contract, height):
            return dict.fromkeys(
                result,
                unavailable("Queued closes settle on a fresh oracle update."),
            )
        pair = None
        if published_at is not None:
            pair = contract_market(
                lcd, state.contract, raw["market_index"], height=height
            )
            if stale_oracle(lcd, state.contract, pair, height, published_at):
                return rejected(
                    "OraclePriceStale",
                    "Perp rejects the close at the observed stale oracle price.",
                )
        market = margin.contract_decimal(oracle["base_per_quote"])
        trigger = stored_liquidation_price(lcd, state, height)
        if market <= 0:
            return result
        observed_at = intents.now()
        collateral = int(raw["collateral_amount"])
        if past_liquidation(market, trigger, long):
            fee = 2 * (collateral * 5 // 100)
            return {
                **result,
                "execution": {
                    "status": "would_liquidate",
                    "marketPrice": price_input(
                        market, "Sai Perp market oracle", observed_at
                    ),
                    "liquidationPrice": format(trigger, "f"),
                },
                "fees": {
                    "status": "estimate",
                    "amountUsdc": format(Decimal(fee).scaleb(-6).normalize(), "f"),
                    "basis": "Gross Perp liquidation fee components.",
                    "excludes": ["Borrowing fees", "network fees"],
                },
                "liquidationPrice": not_applicable(
                    "Liquidation closes the entire position."
                ),
            }

        # A full close settles directly at effective leverage. A partial close runs the guard.
        effective = (
            margin.leverage_inputs(lcd, state, trade_id, height=height)[0]
            if rechecked is None
            else rechecked.effective_leverage
        )
        if effective is None or (
            reduce_by is not None
            and effective != margin.contract_decimal(raw["leverage"])
        ):
            guarded = unavailable(
                "The after-hours guard can change or close this position."
            )
            return {
                **result,
                "execution": guarded,
                "fees": guarded,
                "liquidationPrice": guarded,
            }

        if reduce_by is not None:
            settlement = unavailable(
                "Partial-close settlement can draw on the remaining collateral. "
                "Execution may be rejected if the resulting leverage exceeds the market cap."
            )
            result.update(execution=settlement, liquidationPrice=settlement)
        else:
            result["execution"] = {"status": "voluntary_close"}

        result["fees"] = unavailable(
            "Perp fee schedule or collateral oracle price is unavailable."
        )
        price = margin.contract_decimal(prices["collateral"])
        if price <= 0:
            return result
        if pair is None:
            pair = contract_market(
                lcd, state.contract, raw["market_index"], height=height
            )
        schedule = fee_schedule(lcd, state.contract, pair, height=height)
        _, result["fees"] = base_fees(
            schedule,
            price_input(price, "Sai Perp collateral oracle", observed_at),
            _notional(collateral if reduce_by is None else reduce_by, effective),
            opening=False,
            triggered=reduce_by is not None,
            available_collateral=collateral,
        )
    except margin.PositionChanged:
        if require_unchanged:
            raise
    except READ_ERRORS:
        pass
    return result


def position_estimates(
    lcd: str,
    state: margin.PositionSnapshot | None,
    trade_id: int,
    *,
    target: Decimal | None = None,
    quoted_effective: Decimal | None = None,
    require_unchanged: bool = False,
    show_collateral: bool = False,
    bind_collateral: bool = False,
) -> dict[str, Any]:
    """Quote leverage or TP/SL changes after checking the after-hours guard."""
    unknown = unavailable(
        "Cannot verify the Perp execution inputs and after-hours guard at one block."
    )
    result = dict.fromkeys(ESTIMATE_FIELDS, unknown)
    if state is None:
        return result
    height = None
    try:
        height = margin.verified_position_height(lcd, state, trade_id)
        raw = state.raw
        if target is not None:
            return _leverage_estimates(
                lcd,
                state,
                trade_id,
                height,
                target,
                quoted_effective,
                show_collateral,
                bind_collateral,
            )
        effective, _ = margin.leverage_inputs(lcd, state, trade_id, height=height)
        if effective is None:
            return result
        if effective != margin.contract_decimal(raw["leverage"]):
            return dict.fromkeys(
                result,
                unavailable("The after-hours guard can change or close this position."),
            )
        return _position_change_estimates(
            {
                "status": "unchanged",
                "reason": "This action changes only TP/SL triggers.",
            },
        )
    except margin.PositionChanged:
        # A pinned balance read can invalidate the projection while the
        # starting collateral stays verified.
        if require_unchanged and (height is None or not bind_collateral):
            raise
    except READ_ERRORS:
        pass
    return result


def _position_change_estimates(liq: dict[str, Any]) -> dict[str, Any]:
    return {
        "fees": {
            "status": "exact",
            "amountUsdc": "0",
            "excludes": ["Network fees"],
        },
        "worstCaseFill": not_applicable("This action has no price fill."),
        "liquidationPrice": liq,
    }


def _leverage_estimates(
    lcd: str,
    state: margin.PositionSnapshot,
    trade_id: int,
    height: str,
    target: Decimal,
    quoted_effective: Decimal | None,
    show_collateral: bool,
    bind_collateral: bool,
) -> dict[str, Any]:
    raw, contract = state.raw, state.contract
    pair = contract_market(lcd, contract, raw["market_index"], height=height)
    if refusal := market_open_rejection(pair, market_order=True):
        return refusal

    queued = fills_at_next_oracle_price(lcd, contract, height)
    price = None
    if not queued:
        oracle = margin.perp_prices(lcd, state, height)["market"]
        if not isinstance(oracle, dict):
            raise TypeError("Invalid Perp market oracle price")
        published_at = oracle_publication_time(oracle)
        if published_at is not None and stale_oracle(
            lcd, contract, pair, height, published_at
        ):
            return rejected(
                "OraclePriceStale",
                "Perp rejects the leverage change at the observed stale oracle price.",
            )
        price = margin.contract_decimal(oracle["base_per_quote"])
        if price <= 0:
            raise ValueError("Invalid Perp market oracle price")

    effective, _ = margin.leverage_inputs(lcd, state, trade_id, height=height)
    if effective is None:
        raise ValueError("Cannot verify effective leverage")
    if effective < margin.contract_decimal(raw["leverage"]):
        long = raw.get("long")
        # Perp checks the stored position before it applies the after-hours cap.
        if (
            price is not None
            and type(long) is bool
            and past_liquidation(
                price, stored_liquidation_price(lcd, state, height), long
            )
        ):
            return rejected(
                "LiquidationReached",
                "The after-hours guard reaches liquidation at the stored leverage.",
            )
        return dict.fromkeys(
            ESTIMATE_FIELDS,
            unavailable("The after-hours guard can change or close this position."),
        )
    if quoted_effective is not None and quoted_effective != effective:
        return dict.fromkeys(
            ESTIMATE_FIELDS,
            unavailable("Effective leverage changed while quoting margin."),
        )

    outside = unavailable(
        "The leverage request is outside the market's collateral or leverage limits."
    )
    if not leverage_allowed(
        lcd,
        contract,
        pair,
        state.market["marketId"],
        target,
        current=effective,
        height=height,
    ):
        return _position_change_estimates(outside)
    schedule = fee_schedule(lcd, contract, pair, height=height)
    open_rate = margin.contract_decimal(schedule["open_fee_p"])
    with localcontext() as context:
        context.prec = 90
        before = int(raw["collateral_amount"])
        collateral = int(before * effective / target)
        minimum = int(_notional(before, effective) * open_rate)
    if collateral <= minimum:
        return _position_change_estimates(outside)

    long = raw["long"]
    if type(long) is not bool:
        raise TypeError("Invalid Perp position direction")
    entry = price_input(
        intents.positive(raw["open_price"], "Entry price"),
        "Sai Perp stored entry price",
        state.observed_at,
    )
    liq = liquidation(entry, long, collateral, target)
    # A crossing without borrowing is already conclusive. Between that
    # threshold and the actual one, Perp's quote includes accrued borrowing.
    if price is not None and (
        past_liquidation(price, Decimal(liq["price"]), long)
        or margin.quote_leverage_liquidates(lcd, state, target, height=height)
    ):
        return rejected(
            "LiquidationReached",
            "The observed oracle price has reached the prospective liquidation threshold.",
        )

    liq["assumption"] = (
        "The target leverage takes effect if Perp's borrowing-inclusive "
        "liquidation check passes."
    )
    result = _position_change_estimates(liq)
    if queued:
        result["execution"] = unavailable(
            "Queued leverage changes settle on a fresh oracle update."
        )

    required = collateral - before
    if required > 0:
        funds = margin.exchange_balance(
            lcd,
            contract,
            raw["user"],
            raw["collateral_index"],
            height=height,
        )
        available = int(funds["available_to_trade"])
        if available < required:
            funding = (
                f"Perp requires {Decimal(required).scaleb(-6):f} USDC of additional margin. "
                f"The observed available balance is {Decimal(available).scaleb(-6):f} USDC."
            )
            if not queued:
                return rejected("InsufficientDepositBalance", funding)
            result["execution"]["warning"] = f"{funding} Perp checks funding at fill."
    if bind_collateral:
        result["collateralBaseUnits"] = {
            "before": str(before),
            "after": str(collateral),
        }
    if show_collateral:
        result["collateral"] = margin.collateral_usd(before, collateral)
    return result
