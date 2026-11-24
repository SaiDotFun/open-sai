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

SessionLoader = Callable[[], dict[str, str]]
Notify = Callable[[str], None]
# Called with the preview of the exact intent about to be submitted, after
# every refresh and recheck; raising stops the submission.
SubmitCheck = Callable[[dict[str, Any]], None]

MARGIN_WARNING = (
    "The request authorizes target leverage, not an amount cap; the actual"
    " margin change may differ if the position changes before execution."
)
MARKET_PRICE_NOTICE = (
    "Market orders use the refreshed Keeper price; --price is a dry-run reference."
)


class PositionChanged(ValueError):
    """A live recheck found a different position quote."""

    def __init__(self, message: str, preview: dict[str, Any], addr: str) -> None:
        super().__init__(message)
        self.preview = {**preview, "addr": addr}


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
) -> dict[str, Any]:
    """Return the dry-run preview of PAYLOAD, or check and submit it."""
    preview = {"dryRun": True, "intent": payload}
    if dry_run:
        return preview
    active = get_session()
    before_submit(preview)
    return intents.submit(endpoint, active, payload)


def open_usdc_market(endpoint: str, market_id: int) -> dict[str, Any]:
    """Return Keeper's open USDC borrowing row for MARKET_ID."""
    markets = keeper.request(endpoint, keeper.MARKETS)["perp"]["borrowings"]
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
    notify: Notify = _ignore,
    before_submit: SubmitCheck = _accept,
) -> dict[str, Any]:
    """Open a market, limit, or stop trade.

    A live market order replaces the intent price with Keeper's current price
    for an open USDC market and reports it as `referencePrice`. Its dry run
    sets `requiresMarketPriceRefresh` because that price is not fetched yet.
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
    if order_type != "market":
        return preview_or_submit(endpoint, get_session, payload, dry_run, before_submit)
    if dry_run:
        return {"dryRun": True, "requiresMarketPriceRefresh": True, "intent": payload}
    active = get_session()
    if price is not None:
        notify(MARKET_PRICE_NOTICE)
    current = open_usdc_market(endpoint, market_id)
    current_price = format(Decimal(str(current["price"])), "f")
    intents.positive(current_price, "Current price")
    payload["body"]["intentPrice"] = current_price
    before_submit(
        {
            "dryRun": True,
            "requiresMarketPriceRefresh": True,
            "intent": payload,
            "referencePrice": current_price,
        }
    )
    result = intents.submit(endpoint, active, payload)
    result["referencePrice"] = current_price
    return result


def close_trade(
    endpoint: str,
    get_session: SessionLoader,
    *,
    trade_id: int,
    cancel_order: bool,
    dry_run: bool,
    before_submit: SubmitCheck = _accept,
) -> dict[str, Any]:
    """Close a position or cancel a pending order."""
    payload = intents.close_intent(trade_id, cancel_order)
    return preview_or_submit(endpoint, get_session, payload, dry_run, before_submit)


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
    return preview_or_submit(endpoint, get_session, payload, dry_run, before_submit)


def set_leverage(
    endpoint: str,
    get_session: SessionLoader,
    *,
    trade_id: int,
    leverage: str,
    dry_run: bool,
    before_submit: SubmitCheck = _accept,
) -> dict[str, Any]:
    """Set target leverage without changing TP or SL."""
    payload = intents.leverage_intent(trade_id, leverage)
    return preview_or_submit(endpoint, get_session, payload, dry_run, before_submit)


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

    def build_preview() -> dict[str, Any]:
        state = margin.snapshot(endpoint, lcd_endpoint, active["evmAddress"], trade_id)
        quote = margin.margin_quote(
            state.raw, state.funds, state.market, direction, amount
        )
        return {
            "dryRun": True,
            "quote": quote,
            "intent": intents.leverage_intent(trade_id, quote["requestedLeverage"]),
            "warning": MARGIN_WARNING,
        }

    preview = build_preview()
    if dry_run:
        return preview
    fresh = build_preview()
    if fresh != preview:
        raise PositionChanged(
            "Position changed; review the margin change again",
            fresh,
            active["evmAddress"],
        )
    before_submit(fresh)
    result = intents.submit(endpoint, active, fresh["intent"])
    result["quote"] = fresh["quote"]
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

    def build_preview() -> dict[str, Any]:
        state = margin.snapshot(endpoint, lcd_endpoint, active["evmAddress"], trade_id)
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
        return {
            "dryRun": True,
            "intent": payload,
            "quote": {
                "percent": percent,
                "collateralBefore": str(raw["collateral_amount"]),
                "leverageBefore": str(raw["leverage"]),
                **partial.notional_quote(lcd_endpoint, state, trade_id, delta),
            },
        }

    preview = build_preview()
    if dry_run:
        return preview
    fresh = build_preview()
    if fresh != preview:
        raise PositionChanged(
            "Position changed; review the reduction again", fresh, active["evmAddress"]
        )
    before_submit(fresh)
    return intents.submit(endpoint, active, fresh["intent"])
