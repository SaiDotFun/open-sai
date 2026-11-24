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


class PositionChanged(PreviewRefused):
    """A live recheck found a different position quote."""


def _checked(
    before_submit: SubmitCheck,
    preview: dict[str, Any],
    estimate: Estimate | None,
) -> None:
    try:
        before_submit(preview)
    except PreviewRefused as exc:
        if exc.preview is not None and estimate is not None:
            exc.preview["estimates"] = estimate(exc.preview)
        raise


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
            preview["estimates"] = estimate(preview)
        return preview
    active = get_session()
    _checked(before_submit, preview, estimate)
    return intents.submit(
        endpoint, active, payload, allow_concurrent_open=allow_concurrent_open
    )


def open_usdc_market(
    endpoint: str, market_id: int, *, timeout: float = 20
) -> dict[str, Any]:
    """Return Keeper's open USDC borrowing row for MARKET_ID."""
    markets = keeper.request(endpoint, keeper.MARKETS, timeout=timeout)["perp"][
        "borrowings"
    ]
    candidates = [
        item
        for item in markets
        if item["marketId"] == market_id
        and item["collateralToken"]["symbol"] == "USDC"
        and item["isOpen"]
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
        row = open_usdc_market(endpoint, market_id, timeout=timeout)
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
        if mcp_market:
            read_market()
        elif lcd_endpoint is not None:
            try:
                read_market(estimates.QUOTE_TIMEOUT)
            except estimates.READ_ERRORS:
                pass
        preview = {
            "dryRun": True,
            "requiresMarketPriceRefresh": True,
            "intent": payload,
            "estimates": estimate({}),
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

    def estimate(_preview: dict[str, Any]) -> dict[str, Any]:
        if cancel_order:
            return estimates.non_fill_estimates(cancels=True, updates_triggers=False)
        return _estimate_position(endpoint, lcd_endpoint, get_session, trade_id)

    return preview_or_submit(
        endpoint, get_session, payload, dry_run, before_submit, estimate
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
        lambda _: estimates.non_fill_estimates(cancels=False, updates_triggers=True),
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
) -> dict[str, Any]:
    state, effective = None, None
    if lcd is not None:
        try:
            addr = get_session()["evmAddress"]
            state = margin.snapshot(endpoint, lcd, addr, trade_id)
            effective, _ = margin.leverage_inputs(lcd, state, trade_id)
        except estimates.READ_ERRORS:
            pass
    return estimates.position_estimates(
        lcd or "", state, effective, close=target is None, target=target
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
        preview["estimates"] = estimate(state, inputs, preview)
        return preview

    state, leverage_inputs, fresh = build()
    changed = not margin.position_unchanged(
        lcd_endpoint, state, trade_id, leverage_inputs
    )
    if changed:
        try:
            state, leverage_inputs, fresh = build()
        except (ValueError, keeper.KeeperError, KeyError, TypeError) as exc:
            raise PositionChanged(
                f"{changed_message}. Could not quote the latest position. {exc}"
            ) from exc
    if changed or fresh != preview:
        fresh["estimates"] = estimate(state, leverage_inputs, fresh)
        raise PositionChanged(changed_message, {**fresh, "addr": addr})
    _checked(before_submit, fresh, lambda _: estimate(state, leverage_inputs, fresh))
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
        state = margin.snapshot(endpoint, lcd_endpoint, addr, trade_id)
        leverage, inputs = margin.leverage_inputs(lcd_endpoint, state, trade_id)
        if leverage is None:
            raise ValueError("Cannot verify effective leverage for this margin change")
        quote = margin.margin_quote(
            state.raw,
            state.funds,
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
            _quoted_leverage(inputs),
            target=Decimal(fresh["quote"]["requestedLeverage"]),
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
        return (
            state,
            inputs,
            {
                "dryRun": True,
                "intent": payload,
                "quote": {
                    "percent": percent,
                    "collateralBefore": str(raw["collateral_amount"]),
                    "leverageBefore": str(raw["leverage"]),
                    **inputs,
                    **partial.notional_quote(lcd_endpoint, state, delta, leverage),
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
        estimate=lambda state, inputs, fresh: estimates.position_estimates(
            lcd_endpoint,
            state,
            _quoted_leverage(inputs),
            reduce_by=int(fresh["intent"]["body"]["collateral"]["amountUsdc"]),
        ),
    )
    if dry_run:
        return preview
    return intents.submit(endpoint, active, preview["intent"])


def _quoted_leverage(inputs: dict[str, Any]) -> Decimal | None:
    value = inputs["effectiveLeverage"]
    return Decimal(value) if value is not None else None
