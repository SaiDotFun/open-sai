"""Tool implementations for the Sai MCP server.

Each function takes the server's `Settings` and returns a JSON-ready value,
raising the same errors as the CLI's core. `run` turns a call into a JSON
string and never raises: known errors become `{"error", "code"}` objects,
and anything else becomes a generic error with the traceback logged to
stderr.

The server never takes a session token. It reads the session that `sai auth
login` or `sai auth import` stored, the same way the CLI does, without a
prompt, and never returns the token.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any

from ..core import (
    confirmation,
    intents,
    keeper,
    overview,
    reconcile,
    storage,
    stream,
    trading,
    wait,
)
from ..core import log as sai_log
from ..core import preview as estimates
from ..core import session as sessions
from ..core.errors import Code, SaiError, SessionError
from ..core.errors import describe as describe_error
from ..core.settings import Settings

log = logging.getLogger("sai_cli.mcp_server")

NO_ADDR = (
    "No addr given and no stored Sai session. Pass addr, or sign in with"
    " `sai auth login NAME` or `sai auth import` in a terminal."
)
OTHER_ENDPOINT = (
    "No addr given, and the stored session belongs to a different Keeper"
    " endpoint than this server's keeper.endpoint, so its addr may not be the"
    " account meant here. Pass addr explicitly."
)
SIGN_IN = (
    " Ask the user to sign in by running `sai auth login NAME` or `sai auth"
    " import` in a terminal, then retry. Reads and most previews work without it."
)
NOT_SIGNED_IN = {
    "signed-out": ("No Sai account is signed in." + SIGN_IN, Code.SIGNED_OUT),
    "expired": ("The Sai trading session expired." + SIGN_IN, Code.SESSION_EXPIRED),
    "other-endpoint": (
        "The signed-in Sai session belongs to a different Keeper endpoint than"
        " this server's." + SIGN_IN,
        Code.SESSION_UNAVAILABLE,
    ),
}
INTERNAL = "Unexpected error in the Sai MCP server; see its stderr log"
NEEDS_DIGEST = (
    "preview_digest is required: call the matching preview_* tool, show the"
    " user the preview, get confirmation, then pass its previewDigest"
)
NEEDS_REFERENCE = (
    "reference_price is required for a market order: pass the referencePrice"
    " from the preview_open result the user confirmed"
)
MARKET_ONLY_REFERENCE = "reference_price applies only to market orders"
MISMATCH = (
    "The trade no longer matches the preview the user confirmed, so nothing"
    " was submitted. Show the user this new preview, get their confirmation"
    " again, then retry with its previewDigest."
)
PRICE_MOVED = (
    "The market price moved {moved}% from the confirmed referencePrice, more"
    " than the {slippage}% slippage, so nothing was submitted. Show the user"
    " this new preview, get their confirmation again, then retry with its"
    " previewDigest and referencePrice."
)


class SessionUnavailable(SessionError):
    """The stored session is missing, expired, or unusable by the server.

    CODE is SIGNED_OUT, SESSION_EXPIRED, or SESSION_UNAVAILABLE.
    """


class Refused(SaiError, trading.PreviewRefused):
    """A trade was refused before submission; CODE says why.

    PREVIEW, when set, is the fresh preview with its own previewDigest for
    the agent to show the user before any retry.
    """

    def __init__(
        self,
        message: str,
        code: Code,
        preview: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(message, code)
        self.preview = preview
        self.extra = extra


# Errors


def error(message: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"error": message, "code": code, **extra}


def describe(exc: Exception) -> dict[str, Any]:
    """Format the shared classification using the MCP response fields."""
    failure = describe_error(exc)
    if failure.code == Code.OUTCOME_UNKNOWN:
        key = failure.intent_key
        return error(
            failure.message,
            failure.code,
            intentKey=key,
            next=(
                f"Call intent_status with key {key} to reconcile. Do not"
                " resubmit until it reports a final outcome."
            ),
        )
    if isinstance(exc, storage.RecentOpenPending):
        return error(
            f"Earlier open intent {failure.intent_key} may still execute. Call"
            " intent_status with each key in intentKeys before retrying.",
            failure.code,
            intentKeys=exc.keys,
        )
    if isinstance(exc, Refused):
        extra = {} if exc.preview is None else {"preview": exc.preview}
        return error(failure.message, failure.code, **extra, **exc.extra)
    if isinstance(exc, trading.PositionChanged):
        if exc.preview is None:
            return error(failure.message, Code.PREVIEW_MISMATCH)
        return error(
            "The position, quote, or route changed during confirmation, so nothing was"
            " submitted. Show the user the latest preview, get their confirmation"
            " again, then retry with its previewDigest.",
            Code.PREVIEW_MISMATCH,
            preview=confirmation.with_digest(exc.preview),
        )
    if isinstance(exc, storage.IntentReservationError):
        if key := failure.intent_key:
            return error(
                failure.message,
                failure.code,
                intentKey=key,
                next=(
                    f"Call intent_status with key {key} and addr {exc.account}"
                    " to check the local reservation."
                ),
            )
        return error(failure.message, failure.code)
    if failure.code == Code.INTERNAL_ERROR:
        trace = "".join(traceback.format_exception(exc))
        log.error("Unexpected error in Sai MCP tool\n%s", redact(trace))
        return error(INTERNAL, failure.code)
    return error(failure.message, failure.code)


REDACTED = sai_log.REDACTED


def redact(text: str) -> str:
    """Remove every connected account's session token from TEXT."""
    return sai_log.redact(text)


def run(action: Callable[[], Any]) -> str:
    """Call ACTION and return its result or error as a JSON string."""
    try:
        value = action()
    except Exception as exc:  # noqa: BLE001 - tools must not raise
        value = describe(exc)
        value["error"] = redact(value["error"])
    return json.dumps(value, default=str)


# Session


def trading_state(settings: Settings, addr: str | None = None) -> dict[str, Any]:
    """Report whether trade tools can submit right now for ADDR, or the
    active account, and why not.

    `reason` is signed-in, signed-out, expired, or other-endpoint. A session
    that cannot be read, or an ADDR that is not connected, counts as
    signed-out, with the reason in `error`.
    """
    try:
        if not storage.session_exists():
            return {"available": False, "reason": "signed-out", "addr": None}
        stored = storage.load_session(addr)
        expiry = sessions.expiry_time(stored)
    except (ValueError, KeyError) as exc:
        detail = (
            f"Stored session is malformed; {storage.SIGN_IN_AGAIN}"
            if isinstance(exc, KeyError)
            else redact(str(exc))
        )
        return {
            "available": False,
            "reason": "signed-out",
            "addr": None,
            "error": detail,
        }
    account = stored.get("evmAddress")
    if stored.get("endpoint") != settings.endpoint:
        reason = "other-endpoint"
    elif expiry <= datetime.now(UTC):
        reason = "expired"
    else:
        reason = "signed-in"
    return {"available": reason == "signed-in", "reason": reason, "addr": account}


def require_session(settings: Settings, addr: str | None = None) -> None:
    """Refuse with SIGNED_OUT, SESSION_EXPIRED, or SESSION_UNAVAILABLE unless
    ADDR, or the active account, is connected, signed in for this
    server's endpoint, and unexpired."""
    state = trading_state(settings, addr)
    if state["available"]:
        return
    if "error" in state:
        raise SessionUnavailable(f"{state['error']}, in a terminal.")
    message, code = NOT_SIGNED_IN[state["reason"]]
    raise SessionUnavailable(message, code)


class AccountSession:
    """Load one connected account's session for the length of a tool call.

    With ADDR, that connected account; without it, the active account.
    The first load pins the addr, so switching the active account during
    a call cannot move a checked trade to another account. Never prompts.
    """

    def __init__(self, settings: Settings, addr: str | None = None) -> None:
        self.settings = settings
        self.requested = addr
        self.pinned: str | None = None

    def require(self) -> None:
        """Refuse unless this call's account can trade now; see
        `require_session`."""
        require_session(self.settings, self.pinned or self.requested)

    def __call__(self) -> dict[str, str]:
        self.require()
        try:
            result = sessions.load_active_session(
                self.settings.endpoint, self.pinned or self.requested
            )
        except ValueError as exc:
            raise SessionUnavailable(f"{exc}.{SIGN_IN}") from exc
        self.pinned = result["evmAddress"]
        return result

    def account(self) -> str | None:
        """Return the addr a trade would use, or None when the active
        account is unusable. A requested account must be usable."""
        if self.requested:
            return self()["evmAddress"]
        try:
            return self()["evmAddress"]
        except SessionUnavailable:
            return None


def session_loader(settings: Settings, addr: str | None = None) -> AccountSession:
    """Return a loader for ADDR or the active account; it never prompts."""
    return AccountSession(settings, addr)


def resolve_addr(settings: Settings, addr: str | None) -> dict[str, Any]:
    """Return ADDR, or the stored session's account when it is omitted,
    with where it came from.

    Reads need only the addr, so an expired session still supplies it,
    flagged with `sessionExpired`. A session for another endpoint does not.
    """
    if addr:
        return {"addr": addr, "addrSource": "argument"}
    try:
        if not storage.session_exists():
            raise SessionUnavailable(NO_ADDR)
        stored = storage.load_session()
        expiry = sessions.expiry_time(stored)
    except SessionUnavailable:
        raise
    except ValueError as exc:
        raise SessionUnavailable(f"{exc}. Or pass addr.") from exc
    if stored.get("endpoint") != settings.endpoint:
        raise SessionUnavailable(OTHER_ENDPOINT)
    return {
        "addr": stored["evmAddress"],
        "addrSource": "session",
        "sessionExpired": expiry <= datetime.now(UTC),
        "endpointMatches": True,
    }


def session_state(settings: Settings) -> dict[str, Any]:
    """Describe the stored session without its token."""
    try:
        if not storage.session_exists():
            return {"signedIn": False}
        stored = storage.load_session()
        expiry = sessions.expiry_time(stored)
    except ValueError as exc:
        return {"signedIn": False, "error": str(exc)}
    now = datetime.now(UTC)
    expired = expiry <= now
    matches = stored.get("endpoint") == settings.endpoint
    try:
        connected = len(storage.load_accounts()["sessions"])
    except ValueError:
        connected = 1
    return {
        "signedIn": True,
        "connectedAccounts": connected,
        "usable": matches and not expired,
        **sessions.summary(stored),
        "expired": expired,
        "endpointMatches": matches,
        **({"renewalCommand": sessions.renewal_command(stored)} if expired else {}),
        **(sessions.expiry_fields(stored, now) if matches else {}),
    }


def auth_status(settings: Settings, addr: str | None = None) -> dict[str, Any]:
    stored = session_loader(settings, addr)()
    return {
        **sessions.summary(stored),
        **sessions.expiry_fields(stored),
    }


def account_rows(settings: Settings, accounts: dict[str, Any]) -> dict[str, Any]:
    """Describe every connected account without its token."""
    now = datetime.now(UTC)
    rows = []
    for key, result in sorted(accounts["sessions"].items()):
        expired = sessions.expiry_time(result) <= now
        matches = result.get("endpoint") == settings.endpoint
        rows.append(
            {
                **sessions.summary(result, account_key=key),
                "active": key == accounts["active"],
                "expired": expired,
                "endpointMatches": matches,
                "usable": matches and not expired,
                **sessions.expiry_fields(result, now),
            }
        )
    return {"accounts": rows}


def auth_list(settings: Settings) -> dict[str, Any]:
    return account_rows(settings, storage.load_accounts())


def auth_use(settings: Settings, addr: str) -> dict[str, Any]:
    return storage.use_session(addr, lambda accounts: account_rows(settings, accounts))


# Reads


def market_list(settings: Settings) -> dict[str, Any]:
    return keeper.request(settings.endpoint, keeper.MARKETS)


def market_prices(settings: Settings) -> dict[str, Any]:
    return keeper.request(settings.endpoint, keeper.PRICES)


def account_positions(settings: Settings, addr: str | None) -> dict[str, Any]:
    account = resolve_addr(settings, addr)
    variables = {"trader": account["addr"]}
    return {**account, **keeper.request(settings.endpoint, keeper.POSITIONS, variables)}


def account_history(settings: Settings, addr: str | None) -> dict[str, Any]:
    account = resolve_addr(settings, addr)
    variables = {"trader": account["addr"]}
    data = keeper.request(settings.endpoint, keeper.HISTORY, variables)
    return {**account, **keeper.safe_history_response(data)}


def account_balances(settings: Settings, addr: str | None) -> dict[str, Any]:
    account = resolve_addr(settings, addr)
    variables = {"user": account["addr"]}
    return {
        **account,
        **stream.snapshot(settings.ws_endpoint, stream.BALANCES, variables),
    }


def account_overview(settings: Settings, addr: str | None) -> dict[str, Any]:
    try:
        return overview.account_overview(settings, addr)
    except overview.AccountUnavailable as exc:
        raise SessionUnavailable(f"{exc}. Pass addr explicitly.") from exc


def intent_local(settings: Settings, addr: str | None) -> dict[str, Any]:
    account = resolve_addr(settings, addr)
    pending = storage.list_pending(settings.endpoint, account["addr"])
    return {**account, "intents": pending}


def intent_status(
    settings: Settings, addr: str | None, key: str | None
) -> dict[str, Any]:
    account = resolve_addr(settings, addr)
    data = reconcile.reconcile(settings.endpoint, account["addr"], key)
    return {**account, **reconcile.status_report(data)}


async def intent_wait(
    settings: Settings, key: str, addr: str | None, timeout: float, started: float
) -> dict[str, Any]:
    timeout = min(wait.validate_timeout(timeout), wait.MCP_MAX_TIMEOUT)
    try:
        with storage.busy_timeout(0):
            local = (
                storage.intent_record(settings.endpoint, key) if addr is None else None
            )
        account = (
            {"addr": local[0], "addrSource": "ledger"}
            if local
            else resolve_addr(settings, addr)
        )
        result = await wait.for_intent_async(
            settings.endpoint,
            account["addr"],
            key,
            timeout=timeout,
            started=started,
        )
    except sqlite3.OperationalError as exc:
        if not wait.ledger_busy(exc):
            raise
        account = {}
        result = wait.unknown_result(key, addr)
    if result["outcome"] == "unknown":
        result["hint"] = (
            "Call intent_wait again with the same key and address. Do not resubmit."
        )
    return {**account, **result}


# Preview binding


def bound(
    load: AccountSession,
    digest: str | None,
    reference_price: str | None = None,
    slippage: str | None = None,
) -> trading.SubmitCheck:
    """Return a check that refuses any intent but the confirmed one.

    The check runs right before the Keeper submit, after any price refresh.
    It loads the trade's pinned account strictly, so a logout or expiry
    since the trade started refuses with the sign-in error instead of
    falling back to no account. The digest is then recomputed for that
    account and, for a market open, with the caller's REFERENCE_PRICE, so a
    changed account or reference needs a new confirmation, and a preview
    confirmed with no account can never match. Last, it refuses a market
    open whose refreshed price moved from REFERENCE_PRICE by more than
    SLIPPAGE percent.
    """
    if not digest:
        raise ValueError(NEEDS_DIGEST)
    reference = (
        intents.positive(reference_price, "Reference price")
        if reference_price is not None
        else None
    )

    def check(preview: dict[str, Any]) -> None:
        # Strict: never None, and SessionUnavailable propagates.
        account = load()["evmAddress"]
        fresh = confirmation.with_digest({**preview, "addr": account})
        confirmed = (
            fresh
            if reference is None
            else {**fresh, "referencePrice": format(reference, "f")}
        )
        if confirmation.preview_digest(confirmed) != digest:
            raise Refused(MISMATCH, Code.PREVIEW_MISMATCH, fresh)
        if reference is None or slippage is None:
            return
        current = Decimal(str(preview["referencePrice"]))
        fraction = Decimal(slippage).scaleb(-2)
        lower = estimates.slippage_price(reference, fraction, False)
        upper = estimates.slippage_price(reference, fraction, True)
        if not lower <= current <= upper:
            with localcontext() as context:
                context.prec = 90
                moved = abs(current - reference) / reference * 100
                message = PRICE_MOVED.format(
                    moved=format(moved.quantize(Decimal("0.0001")), "f"),
                    slippage=slippage,
                )
            raise Refused(message, Code.PRICE_MOVED, fresh)

    return check


def submit_check(
    load: AccountSession,
    dry_run: bool,
    digest: str | None,
    reference_price: str | None = None,
    slippage: str | None = None,
) -> trading.SubmitCheck:
    """Return the pre-submit check for a trade; a dry run never calls it.

    A submission first needs a usable session, checked here before any
    preview is rebuilt, so a signed-out trade does no Keeper work at all.
    """
    if dry_run:
        return lambda _preview: None
    load.require()
    return bound(load, digest, reference_price, slippage)


def finish(
    load: AccountSession, dry_run: bool, result: dict[str, Any]
) -> dict[str, Any]:
    """Add the account and issue a digest only for a confirmable preview."""
    if not dry_run:
        return {**result, "addr": load.pinned}
    preview = {**result, "addr": load.account()}
    if preview.get("positionOwnerVerified") is False:
        return preview
    return confirmation.with_digest(preview)


# Trading: each action previews with dry_run and submits without it. A
# submission rebuilds the intent and goes through only if its digest matches
# the preview the user confirmed; the check runs on the exact intent object
# that is then submitted.


def open_trade(
    settings: Settings,
    *,
    dry_run: bool,
    market_id: int,
    side: str,
    order_type: str,
    amount: str,
    leverage: str,
    slippage: str,
    price: str | None,
    take_profit: str | None,
    stop_loss: str | None,
    preview_digest: str | None = None,
    addr: str | None = None,
    reference_price: str | None = None,
    allow_concurrent_open: bool = False,
) -> dict[str, Any]:
    market = order_type == "market"
    if not dry_run:
        require_session(settings, addr)
    if not dry_run and reference_price is not None and not market:
        raise ValueError(MARKET_ONLY_REFERENCE)
    if not dry_run and market and reference_price is None:
        raise ValueError(NEEDS_REFERENCE)
    load = session_loader(settings, addr)
    check = submit_check(
        load, dry_run, preview_digest, reference_price, slippage if market else None
    )
    if not dry_run:
        storage.check_open_pending(
            settings.endpoint,
            load()["evmAddress"],
            allow_concurrent_open=allow_concurrent_open,
        )
    notices: list[str] = []
    result = trading.open_trade(
        settings.endpoint,
        load,
        market_id=market_id,
        side=side,
        order_type=order_type,
        amount=amount,
        leverage=leverage,
        price=price,
        slippage=slippage,
        take_profit=take_profit,
        stop_loss=stop_loss,
        dry_run=dry_run,
        lcd_endpoint=settings.lcd_endpoint,
        mcp_market=True,
        notify=notices.append,
        before_submit=check,
        allow_concurrent_open=allow_concurrent_open,
    )
    if notices:
        result["notices"] = notices
    return finish(load, dry_run, result)


def close_trade(
    settings: Settings,
    *,
    dry_run: bool,
    trade_id: int,
    cancel_order: bool,
    preview_digest: str | None = None,
    addr: str | None = None,
) -> dict[str, Any]:
    load = session_loader(settings, addr)
    check = submit_check(load, dry_run, preview_digest)
    result = trading.close_trade(
        settings.endpoint,
        load,
        trade_id=trade_id,
        cancel_order=cancel_order,
        dry_run=dry_run,
        lcd_endpoint=settings.lcd_endpoint,
        before_submit=check,
    )
    return finish(load, dry_run, result)


def set_tpsl(
    settings: Settings,
    *,
    dry_run: bool,
    trade_id: int,
    take_profit: str | None,
    stop_loss: str | None,
    clear_take_profit: bool,
    clear_stop_loss: bool,
    preview_digest: str | None = None,
    addr: str | None = None,
) -> dict[str, Any]:
    load = session_loader(settings, addr)
    check = submit_check(load, dry_run, preview_digest)
    result = trading.set_tpsl(
        settings.endpoint,
        load,
        trade_id=trade_id,
        take_profit=take_profit,
        stop_loss=stop_loss,
        clear_take_profit=clear_take_profit,
        clear_stop_loss=clear_stop_loss,
        dry_run=dry_run,
        lcd_endpoint=settings.lcd_endpoint,
        before_submit=check,
    )
    return finish(load, dry_run, result)


def set_leverage(
    settings: Settings,
    *,
    dry_run: bool,
    trade_id: int,
    leverage: str,
    preview_digest: str | None = None,
    addr: str | None = None,
) -> dict[str, Any]:
    load = session_loader(settings, addr)
    check = submit_check(load, dry_run, preview_digest)
    result = trading.set_leverage(
        settings.endpoint,
        load,
        trade_id=trade_id,
        leverage=leverage,
        dry_run=dry_run,
        lcd_endpoint=settings.lcd_endpoint,
        before_submit=check,
    )
    return finish(load, dry_run, result)


def change_margin(
    settings: Settings,
    *,
    dry_run: bool,
    trade_id: int,
    direction: str,
    amount: str,
    preview_digest: str | None = None,
    addr: str | None = None,
) -> dict[str, Any]:
    load = session_loader(settings, addr)
    check = submit_check(load, dry_run, preview_digest)
    result = trading.change_margin(
        settings.endpoint,
        load,
        trade_id=trade_id,
        direction=direction,
        amount=amount,
        lcd_endpoint=settings.lcd_endpoint,
        dry_run=dry_run,
        before_submit=check,
    )
    return finish(load, dry_run, result)


def partial_close(
    settings: Settings,
    *,
    dry_run: bool,
    trade_id: int,
    percent: int,
    preview_digest: str | None = None,
    addr: str | None = None,
) -> dict[str, Any]:
    load = session_loader(settings, addr)
    check = submit_check(load, dry_run, preview_digest)
    result = trading.partial_close(
        settings.endpoint,
        load,
        trade_id=trade_id,
        percent=percent,
        lcd_endpoint=settings.lcd_endpoint,
        dry_run=dry_run,
        before_submit=check,
    )
    return finish(load, dry_run, result)
