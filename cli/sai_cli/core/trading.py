"""Trading actions that preview or submit one intent.

With `dry_run`, an action returns the intent it would submit. The
`before_submit` check receives the final preview and may refuse submission.
Actions take a session loader and call it only when they need the session.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from . import intents, keeper, margin, partial
from . import preview as estimates
from .confirmation import digest_material
from .errors import Code, SaiError

SessionLoader = Callable[[], dict[str, str]]
Notify = Callable[[str], None]
# Called with the preview of the exact intent about to be submitted, after
# every refresh and recheck; raising stops the submission.
SubmitCheck = Callable[[dict[str, Any]], None]
PositionPreview = tuple[margin.PositionSnapshot, dict[str, Any], dict[str, Any]]
Estimate = Callable[[dict[str, Any]], dict[str, Any]]

MARGIN_WARNING = (
    "The request authorizes target leverage, not an amount cap; the actual"
    " margin change may differ if the position changes before execution."
)
MARKET_PRICE_NOTICE = (
    "Market orders use the refreshed Keeper price; --price is a dry-run reference."
)


class PreviewRefused(ValueError):
    """A pre-submit check refused the intent and may return a fresh preview."""

    def __init__(self, message: str, preview: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.preview = preview


class PositionChanged(SaiError, PreviewRefused):
    """A live check found different terms for a confirmed trade."""

    code = Code.PREVIEW_MISMATCH

    def __init__(self, message: str, preview: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.preview = preview


class ExecutionRejected(SaiError, ValueError):
    """Current Perp state rules out the requested execution."""

    code = Code.PREVIEW_MISMATCH

    def __init__(self, reason: str, preview: dict[str, Any]) -> None:
        super().__init__(reason, details={"preview": preview})
        self.preview = preview


def _reject_execution(
    preview: dict[str, Any],
    values: dict[str, Any],
    addr: str,
    *,
    require_oracle_time: bool = False,
) -> None:
    reason = estimates.rejection_reason(values)
    if require_oracle_time and estimates.close_oracle_time_unknown(values):
        reason = values["execution"]["reason"]
    if reason:
        fresh = {**preview, "addr": addr}
        _attach_estimates(fresh, values)
        raise ExecutionRejected(reason, fresh)


def _checked(
    before_submit: SubmitCheck | None,
    preview: dict[str, Any],
    estimate: Estimate | None,
    *,
    addr: str,
    check_execution: bool = True,
) -> None:
    values = estimate(preview) if check_execution and estimate is not None else None
    try:
        if before_submit is not None:
            before_submit(preview)
    except PreviewRefused as exc:
        if exc.preview is not None and estimate is not None:
            _attach_estimates(
                exc.preview, values if values is not None else estimate(exc.preview)
            )
        raise
    if values is not None:
        _reject_execution(
            preview, values, addr, require_oracle_time=before_submit is not None
        )


def _attach_estimates(preview: dict[str, Any], values: dict[str, Any]) -> None:
    if "collateral" in values:
        preview["collateral"] = values["collateral"]
        values = {key: value for key, value in values.items() if key != "collateral"}
    preview["estimates"] = values
    rejected = estimates.rejection_reason(values) is not None
    if rejected:
        preview.pop("quote", None)
    if rejected or estimates.close_oracle_time_unknown(values):
        preview.pop("previewDigest", None)


def _ignore(_message: str) -> None:
    return None


def _network_mismatch(exc: margin.TradingNetworkMismatch) -> PositionChanged:
    return PositionChanged(
        f"{exc}. Use an LCD on Keeper's chain and request a new preview."
    )


def _trading_route(endpoint: str, lcd: str, confirming: bool) -> margin.PositionRoute:
    try:
        return margin.trading_route(endpoint, lcd)
    except margin.TradingNetworkMismatch as exc:
        if confirming:
            raise _network_mismatch(exc) from exc
        raise


@contextmanager
def _check_unavailable_target(
    check: SubmitCheck | None, preview: dict[str, Any]
) -> Generator[None]:
    """Reject a changed route even when its target cannot be quoted."""
    try:
        yield
    except (*estimates.READ_ERRORS, margin.PositionOwnerMismatch):
        if check is not None and "positionRoute" in preview:
            try:
                check(preview)
            except PreviewRefused as exc:
                raise PositionChanged(
                    "The trade no longer matches the confirmed preview."
                    " Request a new preview before submitting."
                ) from exc
        raise


def usdc_markets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return borrowing rows for the collateral Keeper uses to open trades."""
    return [row for row in rows if row["collateralToken"]["symbol"] == "USDC"]


def open_usdc_market(
    endpoint: str, market_id: int, *, timeout: float = 20, require_open: bool = True
) -> dict[str, Any]:
    """Return Keeper's USDC borrowing row for MARKET_ID."""
    markets = keeper.request(endpoint, keeper.MARKETS, timeout=timeout)["perp"][
        "borrowings"
    ]
    candidates = [
        item
        for item in usdc_markets(markets)
        if item["marketId"] == market_id and (not require_open or item["isOpen"])
    ]
    if len(candidates) != 1:
        raise ValueError("Current open USDC market data is unavailable")
    return candidates[0]


def open_trade(
    endpoint: str,
    get_session: SessionLoader,
    *,
    market_id: int,
    side: str,
    order_type: str,
    amount: str,
    leverage: str,
    price: str | None,
    slippage: str,
    take_profit: str | None,
    stop_loss: str | None,
    dry_run: bool,
    lcd_endpoint: str | None = None,
    mcp_market: bool = False,
    notify: Notify = _ignore,
    before_price_refresh: SubmitCheck | None = None,
    before_submit: SubmitCheck | None = None,
    allow_concurrent_open: bool = False,
) -> dict[str, Any]:
    """Open a market, limit, or stop trade.

    A live market order replaces the intent price with Keeper's current price
    for an open USDC market and reports it as `referencePrice`. A dry run
    reports a price from Keeper when available. MCP requires one to bind it.
    """
    payload = intents.open_intent(
        market_id,
        side,
        order_type,
        amount,
        leverage,
        price,
        slippage,
        take_profit,
        stop_loss,
    )
    row: dict[str, Any] | None = None
    reference: dict[str, Any] | None = None

    def read_market(timeout: float = 20) -> None:
        nonlocal row, reference
        row = open_usdc_market(
            endpoint, market_id, timeout=timeout, require_open=order_type == "market"
        )
        reference = estimates.price_input(
            estimates.market_price(row), "Sai Keeper market price", intents.now()
        )

    def estimate(preview: dict[str, Any]) -> dict[str, Any]:
        if order_type != "market" and lcd_endpoint is not None:
            try:
                read_market(estimates.QUOTE_TIMEOUT)
            except estimates.READ_ERRORS:
                pass
        if lcd_endpoint is not None and "positionRoute" not in preview:
            try:
                preview["positionRoute"] = margin.trading_route(endpoint, lcd_endpoint)
            except estimates.READ_ERRORS:
                pass
        market_reference = reference
        if order_type == "market" and dry_run and price is not None and not mcp_market:
            market_reference = estimates.price_input(
                Decimal(price), "CLI --price", None
            )
        quote = estimates.open_estimates(
            lcd_endpoint,
            preview.get("positionRoute", {}).get("contractAddress"),
            payload["body"],
            row,
            market_reference,
            market_bound=mcp_market or "positionRoute" in preview,
            fresh_market=not dry_run,
        )
        fill = quote["worstCaseFill"]
        if order_type == "market" and not mcp_market and fill["status"] == "bound":
            fill["assumption"] = "Submit with --preview-digest and the quoted --price."
        return quote

    active = None if dry_run else get_session()
    preview: dict[str, Any] = {"dryRun": True, "intent": payload}
    if lcd_endpoint is not None and not dry_run and before_submit is not None:
        preview["positionRoute"] = _trading_route(
            endpoint, lcd_endpoint, confirming=True
        )

    def recheck_route() -> None:
        if (
            before_submit is None
            or lcd_endpoint is None
            or "positionRoute" not in preview
        ):
            return
        fresh_route = _trading_route(endpoint, lcd_endpoint, confirming=True)
        if fresh_route != preview["positionRoute"]:
            raise PositionChanged(
                "The trading route changed while checking the open."
                " Request a new preview before submitting."
            )

    def check_open(preview: dict[str, Any]) -> None:
        recheck_route()
        if before_submit is not None:
            before_submit(preview)

    market_error: Exception | None = None
    if order_type == "market":
        preview["requiresMarketPriceRefresh"] = True
        if active is not None:
            mismatch = False
            if before_price_refresh is not None:
                try:
                    before_price_refresh(preview)
                except PreviewRefused:
                    mismatch = True
            if price is not None and (before_submit is None or mcp_market):
                notify(MARKET_PRICE_NOTICE)
            try:
                read_market()
            except estimates.READ_ERRORS as exc:
                if mismatch:
                    raise PositionChanged(
                        "The market open no longer matches the confirmed preview."
                        " Request a new preview before submitting."
                    ) from exc
                recheck_route()
                values = estimate(preview)
                recheck_route()
                _reject_execution(preview, values, active["evmAddress"])
                raise
            assert reference is not None
            payload["body"]["intentPrice"] = reference["price"]
        elif mcp_market or lcd_endpoint is not None:
            try:
                read_market(20 if mcp_market else estimates.QUOTE_TIMEOUT)
            except estimates.READ_ERRORS as exc:
                if mcp_market:
                    market_error = exc
        if dry_run and price is not None and not mcp_market:
            preview["referencePrice"] = price
        elif reference is not None:
            preview["referencePrice"] = reference["price"]

    if active is None:
        values = estimate(preview)
        if market_error is not None and estimates.rejection_reason(values) is None:
            raise market_error
        _attach_estimates(preview, values)
        return preview
    _checked(
        check_open,
        preview,
        estimate,
        addr=active["evmAddress"],
    )
    result = intents.submit(
        endpoint, active, payload, allow_concurrent_open=allow_concurrent_open
    )
    if "referencePrice" in preview:
        result["referencePrice"] = preview["referencePrice"]
    return result


def close_trade(
    endpoint: str,
    get_session: SessionLoader,
    *,
    trade_id: int,
    cancel_order: bool,
    dry_run: bool,
    lcd_endpoint: str | None = None,
    before_submit: SubmitCheck | None = None,
) -> dict[str, Any]:
    """Close a position or cancel a pending order."""
    payload = intents.close_intent(trade_id, cancel_order)

    if cancel_order:

        def pending_order(addr: str, lcd: str, contract: str) -> None:
            trade = margin.read_cancel_target(lcd, contract, addr, trade_id)
            if (
                not isinstance(trade, dict)
                or trade.get("user_trade_index") != f"UserTradeIndex({trade_id})"
                or trade.get("is_open") is not True
                or trade.get("trade_type") not in ("limit", "stop")
            ):
                raise ValueError(
                    "Target is not an open pending order. Preview a market close for a filled position."
                )
            if not keeper.same_account(addr, trade.get("user")):
                raise margin.PositionOwnerMismatch(
                    "Contract pending order belongs to a different account"
                )

        def cancel_estimate(_preview: dict[str, Any]) -> dict[str, Any]:
            reason = "A pending order can fill before cancellation executes. Perp may close a filled position at market."
            return {
                key: estimates.unavailable(reason)
                for key in ("fees", "liquidationPrice", "worstCaseFill")
            }

        preview = {"dryRun": True, "intent": payload}
        active = get_session()
        if lcd_endpoint is None:
            raise ValueError("Cannot verify the pending order without an LCD")
        route = _trading_route(
            endpoint, lcd_endpoint, confirming=not dry_run and before_submit is not None
        )
        preview["positionRoute"] = route
        with _check_unavailable_target(before_submit, preview):
            pending_order(active["evmAddress"], lcd_endpoint, route["contractAddress"])
        if dry_run:
            return {**preview, "estimates": cancel_estimate(preview)}
        _checked(before_submit, preview, cancel_estimate, addr=active["evmAddress"])
        return intents.submit(endpoint, get_session(), payload)

    return _position_intent(
        endpoint,
        get_session,
        payload,
        dry_run,
        before_submit,
        lcd_endpoint,
        trade_id,
    )


def set_tpsl(
    endpoint: str,
    get_session: SessionLoader,
    *,
    trade_id: int,
    take_profit: str | None,
    stop_loss: str | None,
    clear_take_profit: bool,
    clear_stop_loss: bool,
    dry_run: bool,
    lcd_endpoint: str | None = None,
    before_submit: SubmitCheck | None = None,
) -> dict[str, Any]:
    """Set or clear take-profit and stop-loss prices."""
    payload = intents.tpsl_intent(
        trade_id, take_profit, stop_loss, clear_take_profit, clear_stop_loss
    )
    return _position_intent(
        endpoint,
        get_session,
        payload,
        dry_run,
        before_submit,
        lcd_endpoint,
        trade_id,
        updates_triggers=True,
    )


def set_leverage(
    endpoint: str,
    get_session: SessionLoader,
    *,
    trade_id: int,
    leverage: str,
    dry_run: bool,
    lcd_endpoint: str | None = None,
    before_submit: SubmitCheck | None = None,
    bind_collateral: bool = False,
) -> dict[str, Any]:
    """Set target leverage without changing TP or SL."""
    payload = intents.leverage_intent(trade_id, leverage)

    return _position_intent(
        endpoint,
        get_session,
        payload,
        dry_run,
        before_submit,
        lcd_endpoint,
        trade_id,
        target=Decimal(leverage),
        bind_collateral=bind_collateral,
    )


def _position_intent(
    endpoint: str,
    get_session: SessionLoader,
    payload: dict[str, Any],
    dry_run: bool,
    before_submit: SubmitCheck | None,
    lcd: str | None,
    trade_id: int,
    *,
    target: Decimal | None = None,
    updates_triggers: bool = False,
    bind_collateral: bool = False,
) -> dict[str, Any]:
    addr = get_session()["evmAddress"]
    preview = {"dryRun": True, "intent": payload}
    if lcd is None and not dry_run:
        raise ValueError("Cannot verify the position without an LCD")
    route = (
        _trading_route(endpoint, lcd, confirming=True)
        if lcd is not None and not dry_run and before_submit is not None
        else None
    )
    if route is not None:
        preview["positionRoute"] = route
    with _check_unavailable_target(before_submit, preview):
        indexed = margin.indexed_position(endpoint, addr, trade_id)
        verified = None
        if lcd is not None:
            try:
                verified = margin.verify_position_owner(
                    endpoint, lcd, addr, trade_id, route=route
                )
            except estimates.READ_ERRORS:
                if not dry_run:
                    raise
    if verified is not None:
        preview["positionRoute"] = verified.route

    def estimate(quoted_preview: dict[str, Any]) -> dict[str, Any]:
        values = _estimate_position(
            endpoint,
            lcd,
            addr,
            trade_id,
            indexed,
            verified,
            target,
            updates_triggers,
            show_collateral=target is not None
            and (dry_run or before_submit is not None),
            bind_collateral=bind_collateral and (dry_run or before_submit is not None),
        )
        if collateral := values.pop("collateralBaseUnits", None):
            quoted_preview["collateralBaseUnits"] = collateral
        elif bind_collateral and not dry_run and before_submit is not None:
            # A definitive rejection may arrive before Perp can quote collateral.
            _reject_execution(quoted_preview, values, addr)
        return values

    if dry_run:
        if verified is None:
            preview["positionOwnerVerified"] = False
        _attach_estimates(preview, estimate(preview))
        return preview
    _checked(
        before_submit,
        preview,
        estimate,
        addr=addr,
        check_execution=not updates_triggers,
    )
    return intents.submit(endpoint, get_session(), payload)


def _estimate_position(
    endpoint: str,
    lcd: str | None,
    addr: str,
    trade_id: int,
    indexed: dict[str, Any],
    verified: margin.VerifiedPosition | None,
    target: Decimal | None,
    updates_triggers: bool,
    *,
    show_collateral: bool = False,
    bind_collateral: bool = False,
) -> dict[str, Any]:
    state = None
    if lcd is not None and verified is not None:
        try:
            state = margin.snapshot(
                endpoint,
                lcd,
                addr,
                trade_id,
                indexed=indexed,
                verified=verified,
                require_open_market=target is None,
            )
        except estimates.READ_ERRORS:
            pass
    if target is None and not updates_triggers:
        return estimates.close_estimates(lcd or "", state, trade_id, None)
    return estimates.position_estimates(
        lcd or "",
        state,
        trade_id,
        target=target,
        show_collateral=show_collateral,
        bind_collateral=bind_collateral,
    )


def _position_preview(
    build: Callable[[], PositionPreview],
    *,
    lcd_endpoint: str,
    addr: str,
    trade_id: int,
    dry_run: bool,
    before_submit: SubmitCheck | None,
    changed_message: str,
    estimate: Callable[
        [
            margin.PositionSnapshot,
            dict[str, Any],
            dict[str, Any],
            margin.RecheckedPosition | None,
        ],
        dict[str, Any],
    ],
) -> dict[str, Any]:
    def verify_replacement(
        state: margin.PositionSnapshot, inputs: dict[str, Any]
    ) -> margin.RecheckedPosition:
        try:
            checked = margin.recheck_position(lcd_endpoint, state, trade_id, inputs)
        except estimates.READ_ERRORS as exc:
            raise PositionChanged(
                f"Could not verify the replacement position quote. {exc}"
            ) from exc
        if checked is not None:
            return checked
        raise PositionChanged(
            "The position quote changed again during verification. Request a new preview."
        )

    def read_quote() -> PositionPreview:
        try:
            return build()
        except margin.TradingNetworkMismatch as exc:
            if dry_run or before_submit is None:
                raise
            try:
                state, inputs, fresh = build()
                checked = verify_replacement(state, inputs)
                _attach_estimates(fresh, estimate(state, inputs, fresh, checked))
            except margin.TradingNetworkMismatch:
                raise _network_mismatch(exc) from exc
            except PositionChanged:
                raise
            except estimates.READ_ERRORS as refresh_error:
                raise PositionChanged(
                    "Could not refresh the position preview after an LCD network mismatch."
                    " Request a new preview before submitting."
                ) from refresh_error
            raise PositionChanged(
                "Keeper trading route and LCD network differed during confirmation."
                " Review the replacement preview before submitting.",
                {**fresh, "addr": addr},
            ) from exc

    state, inputs, preview = read_quote()
    if dry_run:
        _attach_estimates(preview, estimate(state, inputs, preview, None))
        return preview

    try:
        state, inputs, fresh = read_quote()
    except (
        margin.IndexLag,
        margin.PositionUnavailable,
        margin.MarketUnavailable,
    ) as exc:
        raise PositionChanged(f"{changed_message}. {exc}") from exc
    rechecked = margin.recheck_position(lcd_endpoint, state, trade_id, inputs)
    changed = rechecked is None
    if changed:
        try:
            state, inputs, fresh = read_quote()
        except PositionChanged:
            raise
        except (ValueError, keeper.KeeperError, KeyError, TypeError) as exc:
            raise PositionChanged(
                f"{changed_message}. Could not quote the latest position. {exc}"
            ) from exc
        rechecked = verify_replacement(state, inputs)
    if changed or digest_material(fresh) != digest_material(preview):
        _attach_estimates(fresh, estimate(state, inputs, fresh, rechecked))
        raise PositionChanged(changed_message, {**fresh, "addr": addr})
    _checked(
        before_submit,
        fresh,
        lambda _: estimate(state, inputs, fresh, rechecked),
        addr=addr,
    )
    return fresh


def change_margin(
    endpoint: str,
    get_session: SessionLoader,
    *,
    trade_id: int,
    direction: str,
    amount: str,
    lcd_endpoint: str,
    dry_run: bool,
    before_submit: SubmitCheck | None = None,
    max_collateral_usd: str | None = None,
) -> dict[str, Any]:
    """Add or remove margin through a target-leverage intent.

    The amount is a quote, not a cap. Before submitting, a second snapshot
    must match the first, or the change is refused for review.
    """
    delta = margin.margin_amount_usdc(amount)
    cap = None
    if max_collateral_usd is not None:
        if direction != "add":
            raise ValueError("--max-collateral-usd requires --direction add")
        cap = intents.positive(max_collateral_usd, "Maximum collateral in USD")
    addr = get_session()["evmAddress"]

    def build_preview() -> PositionPreview:
        state = margin.snapshot(
            endpoint, lcd_endpoint, addr, trade_id, require_open_market=False
        )
        leverage, inputs = margin.leverage_inputs(lcd_endpoint, state, trade_id)
        if leverage is None:
            raise ValueError("Cannot verify effective leverage for this margin change")
        quote_delta = delta
        if cap is not None:
            cap_num, cap_den = cap.as_integer_ratio()
            max_units = cap_num * 1_000_000 // cap_den
            quote_delta = min(delta, max_units - int(state.raw["collateral_amount"]))
            if quote_delta <= 0:
                raise ValueError("The USD collateral cap leaves no room to add margin")
        quote = margin.margin_quote(
            state.raw,
            None,
            state.market,
            direction,
            quote_delta,
            effective_leverage=leverage,
        )
        if cap is not None:
            quote["maxCollateralUsd"] = format(cap, "f")
        return (
            state,
            inputs,
            {
                "dryRun": True,
                "positionRoute": state.route,
                "quote": {
                    **quote,
                    **inputs,
                    "marginChangeIsEstimate": leverage
                    < Decimal(quote["leverageBefore"]),
                },
                "intent": intents.leverage_intent(trade_id, quote["requestedLeverage"]),
                "warning": MARGIN_WARNING,
            },
        )

    def estimate(
        state: margin.PositionSnapshot,
        inputs: dict[str, Any],
        fresh: dict[str, Any],
        _: margin.RecheckedPosition | None,
    ) -> dict[str, Any]:
        quote = fresh["quote"]
        values = estimates.position_estimates(
            lcd_endpoint,
            state,
            trade_id,
            target=Decimal(quote["requestedLeverage"]),
            quoted_effective=_quoted_leverage(inputs),
            require_unchanged=not dry_run,
        )
        if dry_run or estimates.rejection_reason(values) is None:
            values["collateral"] = margin.collateral_usd(
                int(quote["collateralBefore"]), int(quote["collateralAfter"])
            )
        return values

    preview = _position_preview(
        build_preview,
        lcd_endpoint=lcd_endpoint,
        addr=addr,
        trade_id=trade_id,
        dry_run=dry_run,
        before_submit=before_submit,
        changed_message="Position changed; review the margin change again",
        estimate=estimate,
    )
    if dry_run:
        return preview
    result = intents.submit(endpoint, get_session(), preview["intent"])
    result["quote"] = preview["quote"]
    result["warning"] = MARGIN_WARNING
    return result


def partial_close(
    endpoint: str,
    get_session: SessionLoader,
    *,
    trade_id: int,
    percent: int,
    lcd_endpoint: str,
    dry_run: bool,
    before_submit: SubmitCheck | None = None,
) -> dict[str, Any]:
    """Reduce an open USDC position by a whole percentage.

    The reduction is sized from contract state and re-checked before submitting.
    """
    addr = get_session()["evmAddress"]

    def build_preview() -> PositionPreview:
        state = margin.snapshot(endpoint, lcd_endpoint, addr, trade_id)
        raw = state.raw
        position = {
            "id": trade_id,
            "collateralAmount": raw["collateral_amount"],
            "leverage": raw["leverage"],
        }
        payload = intents.partial_intent(position, percent)
        delta = int(payload["body"]["collateral"]["amountUsdc"])
        leverage, inputs = margin.leverage_inputs(lcd_endpoint, state, trade_id)
        notional = partial.notional_quote(lcd_endpoint, state, delta, leverage)
        return (
            state,
            {**inputs, "collateralPriceUsd": notional["collateralPriceUsd"]},
            {
                "dryRun": True,
                "positionRoute": state.route,
                "intent": payload,
                "quote": {
                    "percent": percent,
                    "collateralBefore": str(raw["collateral_amount"]),
                    "leverageBefore": str(raw["leverage"]),
                    **inputs,
                    **notional,
                },
            },
        )

    preview = _position_preview(
        build_preview,
        lcd_endpoint=lcd_endpoint,
        addr=addr,
        trade_id=trade_id,
        dry_run=dry_run,
        before_submit=before_submit,
        changed_message="Position changed; review the reduction again",
        estimate=lambda state, inputs, fresh, rechecked: estimates.close_estimates(
            lcd_endpoint,
            state,
            trade_id,
            int(fresh["intent"]["body"]["collateral"]["amountUsdc"]),
            require_unchanged=not dry_run,
            rechecked=rechecked,
        ),
    )
    if dry_run:
        return preview
    return intents.submit(endpoint, get_session(), preview["intent"])


def _quoted_leverage(inputs: dict[str, Any]) -> Decimal | None:
    value = inputs["effectiveLeverage"]
    return Decimal(value) if value is not None else None
