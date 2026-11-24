"""Sai MCP server over stdio, started by `sai mcp serve`.

Tools return JSON strings and never raise. Read-only tools carry
`readOnlyHint=True`; trade tools carry `destructiveHint=True`. Every tool is
always listed, so agents see one stable tool set. Trading follows sign-in
per call instead: a trade_* call without a signed-in, unexpired session for
this server's endpoint returns SIGNED_OUT, SESSION_EXPIRED, or
SESSION_UNAVAILABLE before any preview is rebuilt, and otherwise still needs
the digest of the preview the user confirmed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from threading import BoundedSemaphore
from typing import Any, Literal

from anyio import BrokenResourceError
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ContentBlock, TextContent, ToolAnnotations
from pydantic import ConfigDict, ValidationError

from ..core import log as sai_log
from ..core import log_queries, wait
from ..core import settings as sai_settings
from ..core.errors import Code, ExitCode, McpStdioError, SaiError, describe
from ..core.settings import Settings
from . import info, tools

log = logging.getLogger("sai_cli.mcp_server")
BAD_SETTINGS = "BAD_SETTINGS"  # Legacy MCP result code.

INSTRUCTIONS = """\
Sai perpetuals trading on Nibiru through Sai Keeper.

- Call mcp_info first to learn the version, endpoints, and whether \
trading is available: trading.available is true only while an account is \
signed in for this server's endpoint.
- Every tool is always listed. Reads and previews work signed out. A \
trade_* call while signed out returns SIGNED_OUT (or SESSION_EXPIRED, or \
SESSION_UNAVAILABLE for another endpoint) and submits nothing: ask the user \
to run `sai auth login NAME` or `sai auth import` in a terminal, then retry.
- Before any trade, call the matching preview_* tool, show the user the \
preview and the account it names, and get their explicit confirmation. Only then call the trade_* \
tool with the same arguments plus the preview's previewDigest, and for a \
market open also its referencePrice.
- If a trade_* tool returns PREVIEW_MISMATCH or PRICE_MOVED, nothing was \
submitted. If the result includes a new preview, show it to the user and get \
confirmation before using its previewDigest (and referencePrice). Otherwise \
follow the error's instructions and request a new preview.
- "accepted" means Keeper admitted the intent. Use intent_wait with its key \
to check for a final outcome.
- If a trade returns code OUTCOME_UNKNOWN, call intent_wait with its \
intentKey. Never resubmit blindly; the first request may have gone through.
- After a timeout or error on any trade_* call, call intent_local before \
retrying. For RECENT_OPEN_PENDING, check the listed keys with intent_status. \
Wait for a final outcome on pending fills. For recent accepted opens, use \
allow_concurrent_open=true only after the user confirms another position.
- The user can connect several accounts; one is active. auth_list shows \
them. Preview and trade tools act on the active account unless given \
addr, which must be a connected account. Every preview and trade names \
its account; tell the user which account before they confirm.
- Call auth_use only when the user asks to switch accounts. It changes the \
active account for the user's terminal too.
- Account reads report the addr they read. With addrSource \
"session", the addr came from the active account; say so to the user, \
and pass addr explicitly to read any other account.
- Never ask the user for a session token. They sign in \
themselves by running `sai auth login NAME` or `sai auth import` in a terminal.
- Never read MCP client config files to learn about this server; call \
mcp_info instead.
"""

Side = Literal["long", "short"]
OrderType = Literal["market", "limit", "stop"]
Direction = Literal["add", "remove"]


def read_only(title: str, *, open_world: bool = True) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=open_world,
    )


def destructive(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )


# Resolved once, so endpoints cannot change mid-session. `serve` resolves at
# start; `create_server` clears it, and the first tool call resolves it then.
_settings: Settings | None = None


def server_settings() -> Settings:
    """Settings from the environment, then $SAI_HOME/config.toml, then
    mainnet defaults, resolved on first use and kept."""
    global _settings
    if _settings is None:
        _settings = sai_settings.load(warn=log.warning)
    return _settings


async def call(action: Callable[..., Any], /, *args: Any, **kwargs: Any) -> str:
    """Run a blocking tool implementation off the event loop."""
    try:
        config = server_settings()
    except sai_settings.SettingsError as exc:
        return json.dumps(tools.error(str(exc), BAD_SETTINGS))
    return await asyncio.to_thread(tools.run, partial(action, config, *args, **kwargs))


# Reads


async def auth_status(addr: str | None = None) -> str:
    """Show the active account and its session expiry, never the token.

    Sessions come from `sai auth login NAME` or `sai auth import`, run by the
    user in a terminal.

    Args:
        addr: A connected account to check instead of the active one.
    """
    return await call(tools.auth_status, addr)


async def auth_list() -> str:
    """List connected accounts: addr, expiry, whether each is usable on
    this server's endpoint, and which one is active. Never shows tokens."""
    return await call(tools.auth_list)


async def auth_use(addr: str) -> str:
    """Make a connected account the active one. Call only when the user
    asks to switch; the CLI shares the active account. Returns auth_list.

    Args:
        addr: EVM addr (0x...) of a connected account from auth_list.
    """
    return await call(tools.auth_use, addr)


async def market_list() -> str:
    """List Keeper borrowing markets: market id, price, open state, leverage
    limits, minimum position size, fees, and tokens."""
    return await call(tools.market_list)


async def market_prices() -> str:
    """List current indexed USD token prices."""
    return await call(tools.market_prices)


async def account_positions(addr: str | None = None) -> str:
    """List open positions and pending orders.

    The result names the `addr` read and its `addrSource`.

    Args:
        addr: EVM addr (0x...). Defaults to the signed-in account,
            unless its session is for another Keeper endpoint.
    """
    return await call(tools.account_positions, addr)


async def account_history(addr: str | None = None) -> str:
    """Read the latest 50 trade history rows.

    The result names the `addr` read and its `addrSource`.

    Args:
        addr: EVM addr (0x...). Defaults to the signed-in account,
            unless its session is for another Keeper endpoint.
    """
    return await call(tools.account_history, addr)


async def account_balances(addr: str | None = None) -> str:
    """Read one balance snapshot, including Sai deposits and credits.

    The result names the `addr` read and its `addrSource`.

    Args:
        addr: EVM addr (0x...). Defaults to the signed-in account,
            unless its session is for another Keeper endpoint.
    """
    return await call(tools.account_balances, addr)


async def account_overview(addr: str | None = None) -> str:
    """Read Sai funds, position risk, pending and queued orders, local blockers,
    and session expiry. Check each section for its fetch time and any error.

    Args:
        addr: EVM addr (0x...). Defaults to the active account for this endpoint.
    """
    return await call(tools.account_overview, addr)


async def intent_local(addr: str | None = None) -> str:
    """List intents this machine recorded, with their local status.

    Statuses `unknown` and `pending` block new trades until intent_status
    reconciles them.
    The result names the `addr` read and its `addrSource`.

    Args:
        addr: EVM addr (0x...). Defaults to the signed-in account,
            unless its session is for another Keeper endpoint.
    """
    return await call(tools.intent_local, addr)


async def intent_status(addr: str | None = None, key: str | None = None) -> str:
    """Read Keeper's latest intent records; with KEY, reconcile that intent.

    The account list records pending observations in the local ledger.
    With KEY this also checks for a final outcome. If the result includes
    outcome `pending`, follow its hint and check the same key again.

    Args:
        addr: EVM addr (0x...). Defaults to the signed-in account,
            unless its session is for another Keeper endpoint.
        key: Intent key from a trade result or an OUTCOME_UNKNOWN error.
    """
    return await call(tools.intent_status, addr, key)


LOG_WORKERS = 2
_log_slots = BoundedSemaphore(LOG_WORKERS)
_log_executor = ThreadPoolExecutor(
    max_workers=LOG_WORKERS, thread_name_prefix="sai-logs"
)


async def logs_intent(
    key: str, offline: bool = False, addr: str | None = None, before: str | None = None
) -> str:
    """Read one intent's local log trail and current Keeper record.

    Args:
        key: Intent key from a trade result or an OUTCOME_UNKNOWN error.
        offline: Read local logs only.
        addr: EVM address to select if the trail has several accounts.
        before: UTC timestamp from a truncated report's nextBefore field.
    """
    try:
        endpoint = None if offline else server_settings().endpoint
    except sai_settings.SettingsError:
        return json.dumps(tools.error("Could not read Sai settings.", BAD_SETTINGS))
    if not _log_slots.acquire(blocking=False):
        return json.dumps(
            tools.error("Log queries are busy. Retry shortly.", Code.INTERNAL_ERROR)
        )
    action = partial(
        log_queries.intent, key, endpoint=endpoint, addr=addr, before=before
    )
    context = copy_context()

    def execute() -> str:
        return context.run(tools.run, action)

    try:
        future = _log_executor.submit(execute)
    except RuntimeError:
        _log_slots.release()
        raise
    future.add_done_callback(lambda _future: _log_slots.release())
    return await asyncio.wrap_future(future)


async def intent_wait(
    key: str, addr: str | None = None, timeout: float = wait.MCP_MAX_TIMEOUT
) -> str:
    """Wait up to 10 seconds for a final outcome and return an intent receipt.

    If the outcome is unknown, call this tool again with the same key and
    address. Keep using the original key for follow-up.

    Args:
        key: Intent key from a trade result or an OUTCOME_UNKNOWN error.
        addr: EVM or Nibiru address. Defaults to the local intent account, then the active account.
        timeout: Wait in seconds, capped at 10.
    """
    started = time.monotonic()
    try:
        config = server_settings()
    except sai_settings.SettingsError as exc:
        return json.dumps(tools.error(str(exc), "BAD_SETTINGS"))
    try:
        result = await tools.intent_wait(config, key, addr, timeout, started)
    except Exception as exc:  # noqa: BLE001 - tools return structured errors
        result = tools.describe(exc)
        result["error"] = sai_log.redact(result["error"])
    return json.dumps(result, default=str)


# Previews and trades share one implementation per action.


async def preview_open(
    market_id: int,
    side: Side,
    order_type: OrderType,
    amount: str,
    leverage: str,
    slippage: str,
    price: str | None = None,
    take_profit: str | None = None,
    stop_loss: str | None = None,
    addr: str | None = None,
) -> str:
    """Preview opening a market, limit, or stop trade without submitting.

    Show the user the result and get confirmation; trade_open then needs its
    previewDigest. Use estimates for execution fees, liquidation price, and
    the worst-case fill. A market order reports Keeper's current price as
    referencePrice. Submit refreshes the price and checks it against the
    referencePrice from the preview.

    Args:
        market_id: Market id from market_list.
        side: long or short.
        order_type: market, limit, or stop.
        amount: Collateral in USD as a decimal string, at most 6 decimals.
        leverage: Leverage as a decimal string, such as "5".
        slippage: Maximum slippage percentage as a decimal string.
        price: Trigger price, required for limit and stop. A market order
            submits Keeper's current price instead.
        take_profit: Optional take-profit price.
        stop_loss: Optional stop-loss price.
        addr: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.open_trade,
        dry_run=True,
        market_id=market_id,
        side=side,
        order_type=order_type,
        amount=amount,
        leverage=leverage,
        slippage=slippage,
        price=price,
        take_profit=take_profit,
        stop_loss=stop_loss,
        addr=addr,
    )


async def trade_open(
    market_id: int,
    side: Side,
    order_type: OrderType,
    amount: str,
    leverage: str,
    slippage: str,
    preview_digest: str,
    price: str | None = None,
    take_profit: str | None = None,
    stop_loss: str | None = None,
    reference_price: str | None = None,
    allow_concurrent_open: bool = False,
    addr: str | None = None,
) -> str:
    """Submit an open-trade intent. Call preview_open with the same
    arguments, show the user the preview, and get their explicit
    confirmation first. `accepted` means Keeper admitted the intent, not
    that it executed.

    Refuses without submitting: PREVIEW_MISMATCH when the intent differs
    from the confirmed preview, and, for a market order, PRICE_MOVED when
    Keeper's current price moved from reference_price by more than the
    slippage percent. Both return a new preview to show the user. Also
    RECENT_OPEN_PENDING when an earlier open is still unknown, accepted, or
    pending. Check its key with intent_status before retrying.

    Args:
        market_id: Market id from market_list.
        side: long or short.
        order_type: market, limit, or stop.
        amount: Collateral in USD as a decimal string, at most 6 decimals.
        leverage: Leverage as a decimal string, such as "5".
        slippage: Maximum slippage percentage as a decimal string.
        price: Trigger price, required for limit and stop. A market order
            submits Keeper's current price instead.
        take_profit: Optional take-profit price.
        stop_loss: Optional stop-loss price.
        preview_digest: previewDigest from the confirmed preview_open.
        reference_price: referencePrice from the confirmed preview_open;
            required for market orders only.
        allow_concurrent_open: True to submit while an earlier open is unknown
            or accepted, after checking its status and getting confirmation for
            another position. Any locally pending open on the same account and
            Keeper still blocks submission, including a late acknowledgement
            or an outcome that is still waiting to be indexed.
        addr: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.open_trade,
        dry_run=False,
        preview_digest=preview_digest,
        reference_price=reference_price,
        allow_concurrent_open=allow_concurrent_open,
        market_id=market_id,
        side=side,
        order_type=order_type,
        amount=amount,
        leverage=leverage,
        slippage=slippage,
        price=price,
        take_profit=take_profit,
        stop_loss=stop_loss,
        addr=addr,
    )


async def preview_close(
    trade_id: int, cancel_order: bool = False, addr: str | None = None
) -> str:
    """Preview closing a position or cancelling a pending order. Show the
    user the result; trade_close needs its previewDigest.

    Args:
        trade_id: Trade id from account_positions.
        cancel_order: True to cancel a pending order instead of closing.
        addr: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.close_trade,
        dry_run=True,
        trade_id=trade_id,
        cancel_order=cancel_order,
        addr=addr,
    )


async def trade_close(
    trade_id: int,
    preview_digest: str,
    cancel_order: bool = False,
    addr: str | None = None,
) -> str:
    """Submit a close or cancel intent. Call preview_close, show the user the
    preview, and get confirmation first. Refuses with PREVIEW_MISMATCH and a
    new preview if the intent differs from the confirmed one.

    Args:
        trade_id: Trade id from account_positions.
        preview_digest: previewDigest from the confirmed preview_close.
        cancel_order: True to cancel a pending order instead of closing.
        addr: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.close_trade,
        dry_run=False,
        trade_id=trade_id,
        cancel_order=cancel_order,
        preview_digest=preview_digest,
        addr=addr,
    )


async def preview_tpsl(
    trade_id: int,
    take_profit: str | None = None,
    stop_loss: str | None = None,
    clear_take_profit: bool = False,
    clear_stop_loss: bool = False,
    addr: str | None = None,
) -> str:
    """Preview setting or clearing take-profit and stop-loss prices. Show the
    user the result; trade_tpsl needs its previewDigest.

    Args:
        trade_id: Trade id from account_positions.
        take_profit: New take-profit price as a decimal string.
        stop_loss: New stop-loss price as a decimal string.
        clear_take_profit: True to remove the take-profit.
        clear_stop_loss: True to remove the stop-loss.
        addr: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.set_tpsl,
        dry_run=True,
        trade_id=trade_id,
        take_profit=take_profit,
        stop_loss=stop_loss,
        clear_take_profit=clear_take_profit,
        clear_stop_loss=clear_stop_loss,
        addr=addr,
    )


async def trade_tpsl(
    trade_id: int,
    preview_digest: str,
    take_profit: str | None = None,
    stop_loss: str | None = None,
    clear_take_profit: bool = False,
    clear_stop_loss: bool = False,
    addr: str | None = None,
) -> str:
    """Submit a take-profit and stop-loss change. Call preview_tpsl, show the
    user the preview, and get confirmation first. Refuses with
    PREVIEW_MISMATCH and a new preview if the intent differs.

    Args:
        trade_id: Trade id from account_positions.
        preview_digest: previewDigest from the confirmed preview_tpsl.
        take_profit: New take-profit price as a decimal string.
        stop_loss: New stop-loss price as a decimal string.
        clear_take_profit: True to remove the take-profit.
        clear_stop_loss: True to remove the stop-loss.
        addr: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.set_tpsl,
        dry_run=False,
        preview_digest=preview_digest,
        trade_id=trade_id,
        take_profit=take_profit,
        stop_loss=stop_loss,
        clear_take_profit=clear_take_profit,
        clear_stop_loss=clear_stop_loss,
        addr=addr,
    )


async def preview_leverage(
    trade_id: int, leverage: str, addr: str | None = None
) -> str:
    """Preview setting target leverage without changing TP or SL. Show the
    user the result; trade_leverage needs its previewDigest.

    Args:
        trade_id: Trade id from account_positions.
        leverage: Target leverage as a decimal string.
        addr: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.set_leverage,
        dry_run=True,
        trade_id=trade_id,
        leverage=leverage,
        addr=addr,
    )


async def trade_leverage(
    trade_id: int, leverage: str, preview_digest: str, addr: str | None = None
) -> str:
    """Submit a target-leverage change. Call preview_leverage, show the user
    the preview, and get confirmation first. Refuses with PREVIEW_MISMATCH
    and a new preview if the intent differs.

    Args:
        trade_id: Trade id from account_positions.
        leverage: Target leverage as a decimal string.
        preview_digest: previewDigest from the confirmed preview_leverage.
        addr: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.set_leverage,
        dry_run=False,
        trade_id=trade_id,
        leverage=leverage,
        preview_digest=preview_digest,
        addr=addr,
    )


async def preview_margin(
    trade_id: int, direction: Direction, amount: str, addr: str | None = None
) -> str:
    """Quote adding or removing margin from exact contract state.

    The change is a target-leverage intent: the amount is a quote, not a
    cap, and the actual change may differ at execution. Uses effective
    leverage after any applicable after-hours cap. If
    quote.marginChangeIsEstimate is true, tell the user that the guard can
    deduct fees first. A signed-in session is required. trade_margin needs
    the previewDigest the user confirms.

    Args:
        trade_id: Trade id from account_positions.
        direction: add or remove.
        amount: USDC amount to quote, as a decimal string.
        addr: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.change_margin,
        dry_run=True,
        trade_id=trade_id,
        direction=direction,
        amount=amount,
        addr=addr,
    )


async def trade_margin(
    trade_id: int,
    direction: Direction,
    amount: str,
    preview_digest: str,
    addr: str | None = None,
) -> str:
    """Submit a margin change. Call preview_margin, show the user the quote,
    and get confirmation first.

    The digest covers the intent and the quote, so it refuses with
    PREVIEW_MISMATCH and a new preview if the position changed since the
    confirmed quote. The amount is a quote, not a cap.

    Args:
        trade_id: Trade id from account_positions.
        direction: add or remove.
        amount: USDC amount to quote, as a decimal string.
        preview_digest: previewDigest from the confirmed preview_margin.
        addr: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.change_margin,
        dry_run=False,
        trade_id=trade_id,
        direction=direction,
        amount=amount,
        preview_digest=preview_digest,
        addr=addr,
    )


async def preview_partial_close(
    trade_id: int, percent: int, addr: str | None = None
) -> str:
    """Preview reducing an open USDC position by a whole percentage.

    Sizes the reduction from contract state, checks the market minimum, and
    quotes the closed notional. Needs a signed-in session to read the
    position. Show the user the quoted notional and collateral reduction.
    When quote.closedNotionalIsEstimate is true, say that the notional is
    an estimate. trade_partial_close needs the previewDigest they confirm.

    Args:
        trade_id: Trade id from account_positions.
        percent: Whole percentage to close, 1 to 99.
        addr: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.partial_close,
        dry_run=True,
        trade_id=trade_id,
        percent=percent,
        addr=addr,
    )


async def trade_partial_close(
    trade_id: int, percent: int, preview_digest: str, addr: str | None = None
) -> str:
    """Submit a partial close. Call preview_partial_close, show the user the
    preview, and get confirmation first.

    Refuses with PREVIEW_MISMATCH and a new preview if the position changed
    since the confirmed reduction was sized.

    Args:
        trade_id: Trade id from account_positions.
        percent: Whole percentage to close, 1 to 99.
        preview_digest: previewDigest from the confirmed preview_partial_close.
        addr: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.partial_close,
        dry_run=False,
        trade_id=trade_id,
        percent=percent,
        preview_digest=preview_digest,
        addr=addr,
    )


# Registration

READ_TOOLS: tuple[tuple[Callable[..., Any], ToolAnnotations], ...] = (
    (auth_status, read_only("Auth Status", open_world=False)),
    (auth_list, read_only("Connected Accounts", open_world=False)),
    (
        auth_use,
        ToolAnnotations(
            title="Switch Account",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    ),
    (market_list, read_only("Market List")),
    (market_prices, read_only("Market Prices")),
    (account_positions, read_only("Account Positions")),
    (account_history, read_only("Account History")),
    (account_balances, read_only("Account Balances")),
    (intent_local, read_only("Local Intents", open_world=False)),
    (intent_status, read_only("Intent Status")),
    (preview_open, read_only("Preview Open Trade")),
    (preview_close, read_only("Preview Close Trade")),
    (preview_tpsl, read_only("Preview TP/SL")),
    (preview_leverage, read_only("Preview Leverage")),
    (preview_margin, read_only("Preview Margin Change")),
    (preview_partial_close, read_only("Preview Partial Close")),
    (logs_intent, read_only("Intent Log Trail")),
    (account_overview, read_only("Account Overview")),
    (intent_wait, read_only("Wait for Intent")),
)

TRADE_TOOLS: tuple[tuple[Callable[..., Any], ToolAnnotations], ...] = (
    (trade_open, destructive("Open Trade")),
    (trade_close, destructive("Close Trade")),
    (trade_tpsl, destructive("Set TP/SL")),
    (trade_leverage, destructive("Set Leverage")),
    (trade_margin, destructive("Change Margin")),
    (trade_partial_close, destructive("Partial Close")),
)
TRADE_TOOL_NAMES = frozenset(function.__name__ for function, _ in TRADE_TOOLS)


class SaiMCP(FastMCP):
    """FastMCP that checks the session before a trade tool's arguments.

    FastMCP validates arguments before the tool runs, so without this a
    signed-out call with missing or bad arguments would get a validation
    error instead of SIGNED_OUT and the sign-in guidance.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        """Run the tool, logging `mcp.tool_call` before and
        `mcp.tool_result` after. Events the tool writes meanwhile, such as
        Keeper requests, carry the same `tool` and `callId`."""
        started = time.monotonic()
        with sai_log.bind(surface="mcp"):
            logging = sai_log.active()
        if not logging:
            return await self.checked_call(name, arguments)
        known, unknown = self.loggable(name, arguments)
        tool = name if known is not None else "unknown"
        with sai_log.bind(surface="mcp", tool=tool, callId=uuid.uuid4().hex[:12]):
            sai_log.event(
                "mcp.tool_call", arguments=known or {}, unknownArguments=unknown or None
            )
            try:
                result = await self.checked_call(name, arguments)
            except Exception as exc:
                # The message can quote the rejected input, so only its
                # first line is kept, as untrusted text, and nothing for an
                # unknown tool, whose message is the name the client sent.
                first = str(exc).partition("\n")[0] if known is not None else None
                cause = exc.__cause__
                failure = describe(cause if isinstance(cause, Exception) else exc)
                sai_log.event(
                    "mcp.tool_result",
                    "error",
                    ok=False,
                    code=Code.INVALID_REQUEST if known is None else failure.code,
                    intentKey=failure.intent_key,
                    errorType=type(exc).__name__,
                    error=sai_log.untrusted(first),
                    durationMs=round((time.monotonic() - started) * 1000),
                )
                raise
            # A large result is decoded off the event loop.
            text = result_text(result)
            if text is not None and len(text) > INLINE_RESULT_CHARS:
                summary = await asyncio.to_thread(result_summary, text)
            else:
                summary = result_summary(text)
            sai_log.event(
                "mcp.tool_result",
                "info" if summary["ok"] else "warn",
                durationMs=round((time.monotonic() - started) * 1000),
                ok=summary["ok"],
                code=summary.get("code"),
                error=summary.get("error"),
                account=summary.get("account"),
                status=summary.get("status"),
                intentKey=summary.get("intentKey"),
                previewDigest=summary.get("previewDigest"),
            )
            return result

    def loggable(self, name: str, arguments: Any) -> tuple[dict[str, Any] | None, int]:
        """The arguments to log for tool NAME, and how many were left out.

        Arguments are logged before the tool validates them, so only the
        parameters the tool defines are kept, and a call to a tool this
        server does not have logs none (the first value is then None). A
        string is marked as one whole value, so a URL in it is cut to its
        origin through to its end. Every parameter is a single value, so a
        list or object in its place is logged as its type and length only.
        """
        try:
            tool = self._tool_manager.get_tool(name)
            given = arguments if isinstance(arguments, dict) else {}
            if tool is None:
                return None, len(given)
            names = tool.parameters.get("properties", {})
            if name == "logs_intent":
                return {}, sum(key not in names for key in given)
            known = {key: loggable_value(given[key]) for key in names if key in given}
            return known, len(given) - len(known)
        except Exception:  # noqa: BLE001 - logging must not fail a call
            return None, 0

    async def checked_call(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        if name in TRADE_TOOL_NAMES:
            addr = arguments.get("addr")
            refusal = await asyncio.to_thread(
                session_refusal, addr if isinstance(addr, str) else None
            )
            if refusal is not None:
                # Shaped like FastMCP's own result for a `-> str` tool: content
                # plus structured output. FastMCP's annotation omits the tuple.
                content: Sequence[ContentBlock] = [
                    TextContent(type="text", text=refusal)
                ]
                result = (content, {"result": refusal})
                return result  # ty: ignore[invalid-explicit-return]
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            cause = exc.__cause__
            if name != "logs_intent" or not isinstance(cause, ValidationError):
                raise
            # SDK validation renders the rejected input in its exception.
            # Use known fields and type names only, including for extra keys.
            types = {
                "key": "a string",
                "offline": "a boolean",
                "addr": "a string or null",
                "before": "a string or null",
            }
            detail = cause.errors(
                include_input=False, include_context=False, include_url=False
            )[0]
            field = detail["loc"][0] if detail["loc"] else None
            expected = types.get(field) if isinstance(field, str) else None
            if expected is not None:
                message = f"Argument '{field}' must be {expected}."
            else:
                accepted = ", ".join(f"'{key}' ({kind})" for key, kind in types.items())
                message = f"Expected audit arguments {accepted}."
            raise ToolError(message) from SaiError(message, Code.INVALID_REQUEST)


def loggable_value(value: Any) -> Any:
    """One tool argument for the log: a string as one whole value, a
    number or flag as it is, and anything else as its type and length."""
    if isinstance(value, str):
        return sai_log.Arg(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    size = len(value) if isinstance(value, (dict, list)) else 1
    return f"[{type(value).__name__} of {size}]"


# Result fields copied into `mcp.tool_result`: enough to follow a call
# without storing what the tool returned.
RESULT_FIELDS = ("code", "error", "status", "intentKey", "previewDigest")


# A result longer than this is decoded on a worker thread.
INLINE_RESULT_CHARS = 16 * 1024


def result_text(result: Any) -> str | None:
    """The JSON text a tool returned, or None."""
    content = result[0] if isinstance(result, tuple) else result
    return next(
        (
            block.text
            for block in (content if isinstance(content, Sequence) else ())
            if isinstance(block, TextContent)
        ),
        None,
    )


def result_summary(text: str | None) -> dict[str, Any]:
    """What to log about a tool's result TEXT: whether it worked, its error
    `code`, the account it acted on, and an intent key or status."""
    try:
        value = json.loads(text) if text is not None else None
    except ValueError:
        value = None
    if not isinstance(value, dict):
        return {"ok": True}
    summary: dict[str, Any] = {"ok": "error" not in value}
    # A result can echo an argument, so each string is one whole value to
    # the logger, and the error text is untrusted.
    for name in RESULT_FIELDS:
        if isinstance(value.get(name), str):
            summary[name] = sai_log.Arg(value[name])
    if isinstance(summary.get("error"), str):
        summary["error"] = sai_log.untrusted(summary["error"])
    if not summary["ok"]:
        if summary.get("code") == BAD_SETTINGS:
            summary["code"] = Code.INVALID_REQUEST
        else:
            try:
                summary["code"] = Code(summary.get("code"))
            except ValueError:
                summary["code"] = Code.INTERNAL_ERROR
    account = value.get("account", value.get("addr"))
    if isinstance(account, str):
        summary["account"] = sai_log.Arg(account)
    return summary


def session_refusal(addr: str | None = None) -> str | None:
    """Return the JSON error for a trade without a usable session for
    ADDR, or the active account, or None."""
    try:
        tools.require_session(server_settings(), addr)
    except sai_settings.SettingsError as exc:
        return json.dumps(tools.error(str(exc), BAD_SETTINGS))
    except tools.SessionUnavailable as exc:
        return json.dumps(tools.describe(exc))
    return None


def create_server(settings: Settings | None = None) -> FastMCP:
    """Build a server that lists every read, preview, and trade tool.

    Trade tools check sign-in on each call, so registering them while
    signed out offers nothing that can submit. SETTINGS, when None, resolve
    on the first tool call.
    """
    global _settings
    _settings = settings
    server = SaiMCP("sai", instructions=INSTRUCTIONS)

    async def mcp_info() -> str:
        """Report this server's versions, provenance, endpoints, tools,
        session state, log directory, and whether trading is available. Call
        this first.

        `logs.dir` holds JSON-lines logs of every tool call, Keeper request,
        and intent status change; `sai logs tail` in a terminal reads them.

        `restartNeeded` is true when a newer sai was installed after this
        server started; restart the MCP client to load it.

        Never read MCP client config files to learn about this server; they
        can hold secrets. The report never includes the session token.
        """

        def report(config: Settings) -> dict[str, Any]:
            names = sorted(tool.name for tool in server._tool_manager.list_tools())
            return {
                "name": "sai",
                "package": info.PACKAGE,
                **info.versions(),
                "mcpVersion": info.version("mcp"),
                "provenance": info.provenance(),
                "endpoints": {
                    "keeper": sai_log.origin(config.endpoint),
                    "websocket": sai_log.origin(config.ws_endpoint),
                    "lcd": sai_log.origin(config.lcd_endpoint),
                },
                "configFile": str(sai_settings.config_path(os.environ)),
                "logs": {
                    "dir": str(sai_log.directory()),
                    "enabled": sai_log.enabled(),
                },
                "tools": names,
                "trading": tools.trading_state(config),
                "session": tools.session_state(config),
            }

        return await call(report)

    server.add_tool(mcp_info, annotations=read_only("MCP Info", open_world=False))
    for function, hints in READ_TOOLS + TRADE_TOOLS:
        server.add_tool(function, annotations=hints)
    forbid_unknown_arguments(server)
    return server


def forbid_unknown_arguments(server: FastMCP) -> None:
    """Make every tool reject arguments it does not define.

    FastMCP ignores unknown arguments by default. A client still sending a
    renamed argument, such as `address` before it became `addr`, would then
    act on the active account without any error.
    """
    for tool in server._tool_manager.list_tools():
        base = tool.fn_metadata.arg_model
        config = ConfigDict(**{**base.model_config, "extra": "forbid"})
        strict = type(base.__name__, (base,), {"model_config": config})
        tool.fn_metadata.arg_model = strict
        tool.parameters["additionalProperties"] = False


def serve(flags: Mapping[str, str | None] | None = None) -> None:
    """Serve over stdio; stdout carries only protocol.

    FLAGS maps setting keys to the `sai mcp serve` flags given, the top
    layer over SAI_* variables, $SAI_HOME/config.toml, and mainnet
    defaults. Settings resolve once, here, so endpoints cannot change
    mid-session.
    """
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        settings = sai_settings.load(flags, warn=log.warning)
    except sai_settings.SettingsError as exc:
        raise SaiError(
            f"sai mcp serve: {exc}",
            Code.INVALID_REQUEST,
            exit_code=ExitCode.USAGE
            if isinstance(exc, sai_settings.InvalidSetting)
            else None,
        ) from exc
    with sai_log.bind(surface="mcp"):
        sai_log.event(
            "mcp.server_start",
            keeper=sai_log.endpoint(settings.endpoint),
            websocket=sai_log.endpoint(settings.ws_endpoint),
            lcd=sai_log.endpoint(settings.lcd_endpoint),
        )
        server = create_server(settings)
        try:
            server.run()
        except* (KeyboardInterrupt, BrokenPipeError, BrokenResourceError):
            pass
        except* Exception as exc:
            raise McpStdioError("Sai MCP stdio session failed") from exc
