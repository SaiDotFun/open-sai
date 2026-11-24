"""Trading actions that preview or submit one intent.

With `dry_run`, an action returns the intent it would submit. The
`before_submit` check receives the final preview and may refuse submission.
Actions take a session loader and call it only when they need the session.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from . import intents, keeper, margin, partial
from . import preview as estimates
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
    """A live recheck found a different position quote."""

    code = Code.PREVIEW_MISMATCH

    def __init__(self, message: str, preview: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.preview = preview


def _checked(
    before_submit: SubmitCheck,
    preview: dict[str, Any],
    estimate: Estimate | None,
) -> None:
    try:
        before_submit(preview)
    except PreviewRefused as exc:
        if exc.preview is not None and estimate is not None:
            _attach_estimates(exc.preview, estimate(exc.preview))
        raise


def _attach_estimates(preview: dict[str, Any], values: dict[str, Any]) -> None:
    preview["estimates"] = values
    if estimates.rejection_reason(values) is not None:
        preview.pop("quote", None)
        preview.pop("previewDigest", None)


def _ignore(_message: str) -> None:
    return None


def _accept(_preview: dict[str, Any]) -> None:
    return None


def preview_or_submit(
    endpoint: str,
    get_session: SessionLoader,
    payload: dict[str, Any],
    dry_run: bool,
    before_submit: SubmitCheck = _accept,
    estimate: Estimate | None = None,
    *,
    allow_concurrent_open: bool = False,
) -> dict[str, Any]:
    """Return the dry-run preview of PAYLOAD, or check and submit it."""
    preview = {"dryRun": True, "intent": payload}
    if dry_run:
        if estimate is not None:
            _attach_estimates(preview, estimate(preview))
        return preview
    active = get_session()
    _checked(before_submit, preview, estimate)
    return intents.submit(
        endpoint, active, payload, allow_concurrent_open=allow_concurrent_open
    )


def open_usdc_market(
    endpoint: str, market_id: int, *, timeout: float = 20, require_open: bool = True
) -> dict[str, Any]:
    """Return Keeper's USDC borrowing row for MARKET_ID."""
    markets = keeper.request(endpoint, keeper.MARKETS, timeout=timeout)["perp"][
        "borrowings"
    ]
    candidates = [
        item
        for item in markets
        if item["marketId"] == market_id
        and item["collateralToken"]["symbol"] == "USDC"
        and (not require_open or item["isOpen"])
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
    before_submit: SubmitCheck = _accept,
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

    def estimate(_preview: dict[str, Any]) -> dict[str, Any]:
        if order_type != "market" and lcd_endpoint is not None:
            try:
                read_market(estimates.QUOTE_TIMEOUT)
            except estimates.READ_ERRORS:
                pass
        return estimates.open_estimates(
            endpoint,
            lcd_endpoint,
            payload["body"],
            row,
            reference,
            mcp_market=mcp_market,
        )

    if order_type != "market":
        return preview_or_submit(
            endpoint,
            get_session,
            payload,
            dry_run,
            before_submit,
            estimate,
            allow_concurrent_open=allow_concurrent_open,
        )
    if dry_run:
        market_error = None
        if mcp_market or lcd_endpoint is not None:
            try:
                read_market(20 if mcp_market else estimates.QUOTE_TIMEOUT)
            except estimates.READ_ERRORS as exc:
                if mcp_market:
                    market_error = exc
        execution_estimates = estimate({})
        if (
            market_error is not None
            and estimates.rejection_reason(execution_estimates) is None
        ):
            raise market_error
        preview = {
            "dryRun": True,
            "requiresMarketPriceRefresh": True,
            "intent": payload,
            "estimates": execution_estimates,
        }
        if price is not None and not mcp_market:
            preview["referencePrice"] = price
        elif reference is not None:
            preview["referencePrice"] = reference["price"]
        return preview
    active = get_session()
    if price is not None:
        notify(MARKET_PRICE_NOTICE)
    read_market()
    assert reference is not None
    current_price = reference["price"]
    payload["body"]["intentPrice"] = current_price
    _checked(
        before_submit,
        {
            "dryRun": True,
            "requiresMarketPriceRefresh": True,
            "intent": payload,
            "referencePrice": current_price,
        },
        estimate,
    )
    result = intents.submit(
        endpoint, active, payload, allow_concurrent_open=allow_concurrent_open
    )
    result["referencePrice"] = current_price
    return result


def close_trade(
    endpoint: str,
    get_session: SessionLoader,
    *,
    trade_id: int,
    cancel_order: bool,
    dry_run: bool,
    lcd_endpoint: str | None = None,
    before_submit: SubmitCheck = _accept,
) -> dict[str, Any]:
    """Close a position or cancel a pending order."""
    payload = intents.close_intent(trade_id, cancel_order)

    if cancel_order:

        def pending_order(addr: str) -> None:
            if lcd_endpoint is None:
                raise ValueError("Cannot verify the pending order without an LCD")
            contract = margin.trading_contract(endpoint, lcd_endpoint)
            trade = margin.read_cancel_target(lcd_endpoint, contract, addr, trade_id)
            if (
                not isinstance(trade, dict)
                or trade.get("user_trade_index") != f"UserTradeIndex({trade_id})"
                or trade.get("is_open") is not True
                or trade.get("trade_type") not in ("limit", "stop")
            ):
                raise ValueError(
                    "Target is not an open pending order. Preview a market close for a filled position."
                )

        def cancel_estimate(_preview: dict[str, Any]) -> dict[str, Any]:
            reason = "A pending order can fill before cancellation executes. Perp may close a filled position at market."
            return {
                key: estimates.unavailable(reason)
                for key in ("fees", "liquidationPrice", "worstCaseFill")
            }

        preview = {"dryRun": True, "intent": payload}
        active = get_session()
        pending_order(active["evmAddress"])
        if dry_run:
            return {**preview, "estimates": cancel_estimate(preview)}
        _checked(before_submit, preview, cancel_estimate)
        return intents.submit(endpoint, get_session(), payload)

    return preview_or_submit(
        endpoint,
        get_session,
        payload,
        dry_run,
        before_submit,
        lambda _: _estimate_position(endpoint, lcd_endpoint, get_session, trade_id),
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
    before_submit: SubmitCheck = _accept,
) -> dict[str, Any]:
    """Set or clear take-profit and stop-loss prices."""
    payload = intents.tpsl_intent(
        trade_id, take_profit, stop_loss, clear_take_profit, clear_stop_loss
    )
    return preview_or_submit(
        endpoint,
        get_session,
        payload,
        dry_run,
        before_submit,
        lambda _: _estimate_position(
            endpoint, lcd_endpoint, get_session, trade_id, updates_triggers=True
        ),
    )


def set_leverage(
    endpoint: str,
    get_session: SessionLoader,
    *,
    trade_id: int,
    leverage: str,
    dry_run: bool,
    lcd_endpoint: str | None = None,
    before_submit: SubmitCheck = _accept,
) -> dict[str, Any]:
    """Set target leverage without changing TP or SL."""
    payload = intents.leverage_intent(trade_id, leverage)

    def estimate(_preview: dict[str, Any]) -> dict[str, Any]:
        return _estimate_position(
            endpoint, lcd_endpoint, get_session, trade_id, target=Decimal(leverage)
        )

    return preview_or_submit(
        endpoint, get_session, payload, dry_run, before_submit, estimate
    )


def _estimate_position(
    endpoint: str,
    lcd: str | None,
    get_session: SessionLoader,
    trade_id: int,
    *,
    target: Decimal | None = None,
    updates_triggers: bool = False,
) -> dict[str, Any]:
    state = None
    if lcd is not None:
        try:
            addr = get_session()["evmAddress"]
            state = margin.snapshot(
                endpoint, lcd, addr, trade_id, require_open_market=target is None
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
    )


def _position_preview(
    build: Callable[[], PositionPreview],
    *,
    lcd_endpoint: str,
    addr: str,
    trade_id: int,
    dry_run: bool,
    before_submit: SubmitCheck,
    changed_message: str,
    estimate: Callable[
        [margin.PositionSnapshot, dict[str, Any], dict[str, Any]], dict[str, Any]
    ],
) -> dict[str, Any]:
    state, inputs, preview = build()
    if dry_run:
        _attach_estimates(preview, estimate(state, inputs, preview))
        return preview

    try:
        state, inputs, fresh = build()
    except (
        margin.IndexLag,
        margin.PositionUnavailable,
        margin.MarketUnavailable,
    ) as exc:
        raise PositionChanged(f"{changed_message}. {exc}") from exc
    changed = not margin.position_unchanged(lcd_endpoint, state, trade_id, inputs)
    if changed:
        try:
            state, inputs, fresh = build()
        except (ValueError, keeper.KeeperError, KeyError, TypeError) as exc:
            raise PositionChanged(
                f"{changed_message}. Could not quote the latest position. {exc}"
            ) from exc
    if changed or fresh != preview:
        _attach_estimates(fresh, estimate(state, inputs, fresh))
        raise PositionChanged(changed_message, {**fresh, "addr": addr})
    _checked(before_submit, fresh, lambda _: estimate(state, inputs, fresh))
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
    before_submit: SubmitCheck = _accept,
) -> dict[str, Any]:
    """Add or remove margin through a target-leverage intent.

    The amount is a quote, not a cap. Before submitting, a second snapshot
    must match the first, or the change is refused for review.
    """
    active = get_session()
    addr = active["evmAddress"]

    def build_preview() -> PositionPreview:
        state = margin.snapshot(
            endpoint, lcd_endpoint, addr, trade_id, require_open_market=not dry_run
        )
        leverage, inputs = margin.leverage_inputs(lcd_endpoint, state, trade_id)
        if leverage is None:
            raise ValueError("Cannot verify effective leverage for this margin change")
        quote = margin.margin_quote(
            state.raw,
            None if dry_run else state.funds,
            state.market,
            direction,
            amount,
            effective_leverage=leverage,
        )
        return (
            state,
            inputs,
            {
                "dryRun": True,
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

    preview = _position_preview(
        build_preview,
        lcd_endpoint=lcd_endpoint,
        addr=addr,
        trade_id=trade_id,
        dry_run=dry_run,
        before_submit=before_submit,
        changed_message="Position changed; review the margin change again",
        estimate=lambda state, inputs, fresh: estimates.position_estimates(
            lcd_endpoint,
            state,
            trade_id,
            target=Decimal(fresh["quote"]["requestedLeverage"]),
            quoted_effective=_quoted_leverage(inputs),
        ),
    )
    if dry_run:
        return preview
    result = intents.submit(endpoint, active, preview["intent"])
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
    before_submit: SubmitCheck = _accept,
) -> dict[str, Any]:
    """Reduce an open USDC position by a whole percentage.

    The reduction is sized from contract state, checked against the market's
    minimum position size, and re-checked before submitting.
    """
    active = get_session()
    addr = active["evmAddress"]

    def build_preview() -> PositionPreview:
        state = margin.snapshot(endpoint, lcd_endpoint, addr, trade_id)
        raw = state.raw
        market_id = state.market["marketId"]
        markets = keeper.request(endpoint, keeper.MARKETS)["perp"]["borrowings"]
        candidates = [
            item
            for item in markets
            if item["marketId"] == market_id
            and item["collateralToken"]["symbol"] == "USDC"
        ]
        if len(candidates) != 1 or not candidates[0]["isOpen"]:
            raise ValueError("Open market data not available")
        position = {
            "id": trade_id,
            "collateralAmount": raw["collateral_amount"],
            "leverage": raw["leverage"],
        }
        payload = intents.partial_intent(
            position, percent, Decimal(str(candidates[0]["minPositionSizeUSD"]))
        )
        delta = int(payload["body"]["collateral"]["amountUsdc"])
        leverage, inputs = margin.leverage_inputs(lcd_endpoint, state, trade_id)
        notional = partial.notional_quote(lcd_endpoint, state, delta, leverage)
        return (
            state,
            {**inputs, "collateralPriceUsd": notional["collateralPriceUsd"]},
            {
                "dryRun": True,
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
        estimate=lambda state, inputs, fresh: estimates.close_estimates(
            lcd_endpoint,
            state,
            trade_id,
            int(fresh["intent"]["body"]["collateral"]["amountUsdc"]),
        ),
    )
    if dry_run:
        return preview
    return intents.submit(endpoint, active, preview["intent"])


def _quoted_leverage(inputs: dict[str, Any]) -> Decimal | None:
    value = inputs["effectiveLeverage"]
    return Decimal(value) if value is not None else None
