"""Exact on-chain margin quotes for the Keeper leverage intent."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import socket
import time
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import closing, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from secrets import token_hex
from threading import BoundedSemaphore, Thread
from typing import Any, Literal, TypedDict
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from . import intents, keeper
from .errors import Code, SaiError
from .log import origin

MAINNET_LCD = "https://lcd.nibiru.fi"
BLOCK_HEIGHT_HEADER = "x-cosmos-block-height"
BLOCK_PATH = "/cosmos/base/tendermint/v1beta1/blocks/"
NODE_INFO_PATH = "/cosmos/base/tendermint/v1beta1/node_info"
INDEX_RETRY_DELAYS = (0.25, 0.5)
INDEX_RETRY_BUDGET = 2.0
INDEX_LAG = (
    "Indexed position differs from contract state. Retry after Keeper"
    " synchronizes. No intent was submitted."
)
_LCD_DNS_SLOTS = BoundedSemaphore(8)
_LOG = logging.getLogger(__name__)
_LCD_REQUEST: ContextVar[bool] = ContextVar("sai_lcd_request", default=False)
# HTTPX logs the complete URL at INFO. Filter at its logger before host
# handlers receive the record, with the filter scoped to each LCD request.
logging.getLogger("httpx").addFilter(lambda _: not _LCD_REQUEST.get())
_AddressInfo = (
    tuple[Literal[socket.AF_INET], socket.SocketKind, int, str, tuple[str, int]]
    | tuple[
        Literal[socket.AF_INET6],
        socket.SocketKind,
        int,
        str,
        tuple[str, int, int, int] | tuple[int, bytes],
    ]
)


class IndexLag(SaiError, ValueError):
    """Keeper's position did not catch up within the re-read budget."""

    code = Code.INVALID_REQUEST

    @property
    def retryable(self) -> bool:
        return True


class PositionChanged(SaiError, ValueError):
    """The current position no longer matches the quote."""

    code = Code.PREVIEW_MISMATCH


class PositionUnavailable(ValueError):
    """Keeper has no open position for this ID."""


class PositionOwnerMismatch(SaiError):
    """The position could not be attributed to the selected account."""

    code = Code.INVALID_REQUEST


class MarketUnavailable(ValueError):
    """Keeper has no open market for the position."""


class TradingNetworkMismatch(ValueError):
    """Keeper and the LCD reported different valid chain IDs."""


class QueryHeightMismatch(SaiError, ValueError):
    """The LCD answered a pinned query at an unverified height."""

    code = Code.PREVIEW_MISMATCH


class LCDTimeout(ValueError):
    """An overview LCD request ran out of time."""


class _LCDStatusError(ValueError):
    def __init__(self, endpoint: str, response: httpx.Response) -> None:
        super().__init__(f"LCD at {endpoint} returned HTTP {response.status_code}")
        self.body = response.text
        self.query_heights = (
            response.headers.get(f"grpc-metadata-{BLOCK_HEIGHT_HEADER}"),
            response.headers.get(BLOCK_HEIGHT_HEADER),
        )


async def _resolve_lcd_host(
    host: str | bytes | None,
    port: str | bytes | int | None,
    *,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[_AddressInfo]:
    if not _LCD_DNS_SLOTS.acquire(blocking=False):
        raise TimeoutError("LCD DNS slots are busy")
    result: Future[list[_AddressInfo]] = Future()

    def resolve() -> None:
        try:
            if result.set_running_or_notify_cancel():
                result.set_result(
                    socket.getaddrinfo(host, port, family, type, proto, flags)
                )
        except Exception as exc:  # noqa: BLE001 - forward the resolver's exception
            result.set_exception(exc)
        finally:
            _LCD_DNS_SLOTS.release()

    # System DNS can outlive the HTTP request. Keep its permit until the lookup
    # returns, and let the CLI exit even if a resolver is still blocked.
    try:
        Thread(target=resolve, name="sai-lcd-dns", daemon=True).start()
    except RuntimeError as exc:
        _LCD_DNS_SLOTS.release()
        raise TimeoutError("Could not start LCD DNS lookup") from exc
    return await asyncio.wrap_future(result)


def _get_before_deadline(
    url: httpx.URL,
    deadline: float,
    headers: dict[str, str] | None,
) -> httpx.Response:
    async def request() -> httpx.Response:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("LCD read deadline exceeded")
        async with asyncio.timeout(remaining):
            async with httpx.AsyncClient(timeout=None) as client:
                return await client.get(url, headers=headers)

    with closing(asyncio.new_event_loop()) as loop:
        loop.getaddrinfo = _resolve_lcd_host
        task = loop.create_task(request())
        try:
            return loop.run_until_complete(task)
        except KeyboardInterrupt:
            task.cancel()
            with suppress(asyncio.CancelledError):
                loop.run_until_complete(task)
            raise


def _get_json(url: str, timeout: float = 20, *, fresh: bool = False) -> dict[str, Any]:
    return _get_json_at_height(url, timeout, fresh=fresh)[0]


def _get_json_at_height(
    url: str,
    timeout: float = 20,
    *,
    height: str | None = None,
    fresh: bool = False,
    allow_unprefixed_height: bool = False,
    deadline: float | None = None,
) -> tuple[dict[str, Any], str | None]:
    keeper.check_url(url, "LCD")
    try:
        headers = {BLOCK_HEIGHT_HEADER: height} if height is not None else None
        # The mainnet LCD caches by URI and can omit metadata on a hit.
        # Give every confirmation read a fresh URI, including the height anchor.
        request_url = httpx.URL(url)
        if fresh or height is not None:
            query = request_url.query
            separator = b"&" if query else b""
            nonce = token_hex(16).encode("ascii")
            request_url = request_url.copy_with(
                query=query + separator + b"_sai_query=" + nonce
            )
        request_token = _LCD_REQUEST.set(True)
        try:
            if deadline is None:
                response = httpx.get(request_url, timeout=timeout, headers=headers)
            else:
                response = _get_before_deadline(request_url, deadline, headers)
        finally:
            _LCD_REQUEST.reset(request_token)
        site = origin(url)
        _LOG.info("LCD HTTP response from %s (status %d)", site, response.status_code)
        if not 200 <= response.status_code < 300:
            raise _LCDStatusError(site, response)
        result = response.json()
        if not isinstance(result, dict):
            raise TypeError
    except (
        httpx.HTTPError,
        httpx.InvalidURL,
        TimeoutError,
        ValueError,
        TypeError,
    ) as exc:
        if deadline is not None and isinstance(
            exc, (TimeoutError, httpx.TimeoutException)
        ):
            raise LCDTimeout("LCD read timed out") from None
        cause = exc if isinstance(exc, _LCDStatusError) else None
        if height is not None or fresh:
            raise QueryHeightMismatch(
                "Unable to verify contract state during confirmation."
                " Retry the preview and confirmation."
            ) from cause
        raise ValueError("Unable to read contract state") from cause
    query_height = response.headers.get(f"grpc-metadata-{BLOCK_HEIGHT_HEADER}")
    if query_height is None and allow_unprefixed_height:
        query_height = response.headers.get(BLOCK_HEIGHT_HEADER)
    if height is not None and query_height != height:
        raise QueryHeightMismatch(
            "LCD did not serve the requested contract query height."
            " Retry the preview and confirmation."
        )
    return result, query_height


def lcd_url(lcd: str, route: str) -> str:
    """Append a REST route to the LCD path and keep its query parameters."""
    parts = urlsplit(lcd)
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") + route))


def _smart_url(lcd: str, contract: str, message: dict[str, Any]) -> str:
    encoded = quote(
        base64.b64encode(json.dumps(message, separators=(",", ":")).encode()).decode(),
        safe="",
    )
    return lcd_url(lcd, f"/cosmwasm/wasm/v1/contract/{contract}/smart/{encoded}")


def smart_value(
    lcd: str,
    contract: str,
    message: dict[str, Any],
    *,
    height: str | None = None,
    fresh: bool = False,
    timeout: float = 20,
) -> Any:
    """Read a CosmWasm smart query, including scalar and array responses."""
    url = _smart_url(lcd, contract, message)
    if height is None:
        return _get_json(url, timeout, fresh=fresh).get("data")
    return _get_json_at_height(url, timeout, height=height)[0].get("data")


def block_header(lcd: str, height: int | str, *, deadline: float) -> dict[str, Any]:
    result, _ = _get_json_at_height(
        lcd_url(lcd, f"{BLOCK_PATH}{height}"),
        fresh=height == "latest",
        deadline=deadline,
    )
    data = result["block"]["header"]
    if not isinstance(data, dict):
        raise TypeError("Invalid LCD block header")
    return data


def smart_data(
    lcd: str,
    contract: str,
    message: dict[str, Any],
    *,
    height: int,
    deadline: float,
) -> Any:
    """Read overview state at the chosen block before the request deadline."""
    data, _ = _get_json_at_height(
        _smart_url(lcd, contract, message),
        height=str(height),
        allow_unprefixed_height=True,
        deadline=deadline,
    )
    return data.get("data")


def smart(
    lcd: str, contract: str, message: dict[str, Any], *, timeout: float = 20
) -> dict[str, Any]:
    data = smart_value(lcd, contract, message, timeout=timeout)
    if not isinstance(data, dict):
        raise TypeError("Contract returned invalid state")
    return data


def pair_leverage_cap(
    lcd: str, contract: str, market_id: int, *, height: str | None = None
) -> Decimal:
    """Read Perp's custom cap, where an absent map entry means no override."""
    try:
        value = smart_value(
            lcd,
            contract,
            {"get_pair_custom_max_leverage": {"index": market_id}},
            height=height,
        )
    except ValueError as exc:
        failure = exc.__cause__
        if isinstance(failure, _LCDStatusError) and re.search(
            r"type: cosmwasm_std::math::decimal::Decimal; key: \[[^\]]*\] not found",
            failure.body,
        ):
            return Decimal(0)
        raise
    return contract_decimal(value)


def read_cancel_target(lcd: str, contract: str, trader: str, trade_id: int) -> Any:
    """Read the live order and distinguish Perp's missing-target error from a quote mismatch."""
    try:
        return smart_value(
            lcd,
            contract,
            {"get_trade_raw": {"trader": trader, "index": trade_id}},
            fresh=True,
        )
    except QueryHeightMismatch as exc:
        failure = exc.__cause__
        # The wasm gRPC gateway can wrap Perp query errors in HTTP 500.
        if isinstance(failure, _LCDStatusError) and re.search(
            rf"Trade not found for trader \S+ with index {trade_id}\b",
            failure.body,
        ):
            raise ValueError(
                f"Trade {trade_id} was not found for this account."
            ) from exc
        raise ValueError(f"Unable to read pending order {trade_id} from Perp.") from exc


class PositionRoute(TypedDict):
    chainId: str
    contractAddress: str


@dataclass(frozen=True)
class PositionSnapshot:
    raw: dict[str, Any]
    funds: dict[str, Any]
    market: dict[str, Any]
    route: PositionRoute
    observed_at: str | None = None

    @property
    def contract(self) -> str:
        return self.route["contractAddress"]

    @property
    def usdc_collateral(self) -> bool:
        return self.market.get("collateralToken", {}).get("symbol") == "USDC"


def quote_leverage_liquidates(
    lcd: str, state: PositionSnapshot, target: Decimal, *, height: str
) -> bool:
    """Ask Perp to run a leverage update against discarded query storage."""
    try:
        smart_value(
            lcd,
            state.contract,
            {
                "quote_trade": {
                    "trader": state.raw["user"],
                    "funds": [],
                    "action": {
                        "update_leverage": {
                            "trade_index": state.raw["user_trade_index"],
                            "new_leverage": format(target, "f"),
                        }
                    },
                }
            },
            height=height,
        )
    except QueryHeightMismatch as exc:
        failure = exc.__cause__
        if not isinstance(failure, _LCDStatusError):
            return False
        # The gateway may omit block metadata on contract errors. The request
        # pins the height and bypasses the LCD cache.
        if any(value not in (None, height) for value in failure.query_heights):
            return False
        try:
            body = json.loads(failure.body)
        except ValueError:
            return False
        message = body.get("message") if isinstance(body, dict) else None
        return (
            isinstance(message, str)
            and re.search(
                r"\bliquidation price reached\. Liq price \d+(?:\.\d+)?, "
                r"current price \d+(?:\.\d+)?\b",
                message,
            )
            is not None
        )
    return False


def _read_position(
    lcd: str, contract: str, trader: str, trade_id: int, timeout: float
) -> dict[str, Any]:
    return smart(
        lcd,
        contract,
        {"get_trade_raw": {"trader": trader, "index": trade_id}},
        timeout=timeout,
    )


def _latest_position(
    lcd: str, state: PositionSnapshot, trade_id: int
) -> tuple[dict[str, Any], str | None]:
    message = {"get_trade_raw": {"trader": state.raw["user"], "index": trade_id}}
    result, height = _get_json_at_height(
        _smart_url(lcd, state.contract, message), fresh=True
    )
    raw = result.get("data")
    if not isinstance(raw, dict):
        raise TypeError("Contract returned invalid state")
    if not keeper.same_account(state.raw["user"], raw.get("user")):
        raise PositionOwnerMismatch("Contract position belongs to a different account")
    if height is not None and (
        not height.isascii() or not height.isdecimal() or not height.lstrip("0")
    ):
        raise QueryHeightMismatch(
            "LCD returned an invalid contract query height."
            " Retry the preview and confirmation."
        )
    return raw, height


def _same_position(prior: dict[str, Any], latest: dict[str, Any]) -> bool:
    fields = (
        "user_trade_index",
        "market_index",
        "collateral_index",
        "collateral_amount",
        "leverage",
        "is_open",
        "trade_type",
    )
    return all(latest.get(key) == prior[key] for key in fields)


def verified_position_height(lcd: str, state: PositionSnapshot, trade_id: int) -> str:
    latest, height = _latest_position(lcd, state, trade_id)
    if (
        not _same_position(state.raw, latest)
        or latest.get("long") is not state.raw.get("long")
        or latest.get("open_price") != state.raw.get("open_price")
    ):
        raise PositionChanged("Position changed during the final preview check.")
    if height is None:
        raise ValueError("Cannot verify the quoted position at the LCD query height")
    return height


@dataclass(frozen=True)
class RecheckedPosition:
    height: str
    effective_leverage: Decimal


def recheck_position(
    lcd: str,
    state: PositionSnapshot,
    trade_id: int,
    quoted_inputs: dict[str, Any],
    *,
    partial_close: bool,
) -> RecheckedPosition | None:
    """Recheck the on-chain quote inputs at the final position's block."""
    latest, height = _latest_position(lcd, state, trade_id)
    if not _same_position(state.raw, latest):
        return None
    if partial_close and (
        latest.get("long") is not state.raw.get("long")
        or latest.get("open_price") != state.raw.get("open_price")
    ):
        return None
    effective_leverage, inputs = leverage_inputs(lcd, state, trade_id, height=height)
    if inputs != quoted_inputs:
        return None
    if height is None:
        raise ValueError(
            "Cannot confirm the position and leverage at the same block."
            " Retry with an LCD that reports the contract query height."
        )
    if effective_leverage is None:
        raise PositionChanged(
            "Cannot verify effective leverage at the position's block."
            " Request a new preview once the leverage inputs are readable."
        )
    return RecheckedPosition(height, effective_leverage)


def block_timestamp(lcd: str, height: str) -> int:
    """Read the UTC timestamp of a specific LCD block."""
    result = _get_json(lcd_url(lcd, f"{BLOCK_PATH}{height}"))
    block = result.get("sdk_block") or result.get("block")
    if not isinstance(block, dict):
        raise TypeError("LCD block header is unavailable")
    header = block["header"]
    time = datetime.fromisoformat(header["time"])
    if header["height"] != height or time.tzinfo is None:
        raise ValueError("Cannot verify the timestamp of the quoted LCD block")
    return int(time.timestamp())


def trading_route(endpoint: str, lcd: str) -> PositionRoute:
    route = keeper.request(endpoint, keeper.ROUTE)["oneClickTrading"]
    node = _get_json(lcd_url(lcd, NODE_INFO_PATH))
    info = node.get("default_node_info")
    chain_id = route.get("chainId")
    network = info.get("network") if isinstance(info, dict) else None
    if (
        route["enabled"] is not True
        or not isinstance(chain_id, str)
        or not chain_id
        or not isinstance(network, str)
        or not network
    ):
        raise ValueError("Keeper trading route and LCD network do not match")
    contract = route.get("contractAddress")
    if not isinstance(contract, str) or not contract:
        raise ValueError("Keeper trading route has no contract address")
    if network != chain_id:
        raise TradingNetworkMismatch(
            "Keeper trading route and LCD network do not match"
        )
    return {"chainId": chain_id, "contractAddress": contract}


@dataclass(frozen=True)
class VerifiedPosition:
    route: PositionRoute
    raw: dict[str, Any]


def verify_position_owner(
    endpoint: str,
    lcd: str,
    addr: str,
    trade_id: int,
    *,
    route: PositionRoute | None = None,
) -> VerifiedPosition:
    """Read the live target and check its trade index and owner."""
    if route is None:
        route = trading_route(endpoint, lcd)
    raw = smart_value(
        lcd,
        route["contractAddress"],
        {"get_trade_raw": {"trader": addr, "index": trade_id}},
        fresh=True,
    )
    if (
        not isinstance(raw, dict)
        or raw.get("user_trade_index") != f"UserTradeIndex({trade_id})"
    ):
        raise ValueError(f"Unable to verify contract position {trade_id}")
    if not keeper.same_account(addr, raw.get("user")):
        raise PositionOwnerMismatch("Contract position belongs to a different account")
    return VerifiedPosition(route, raw)


def snapshot(
    endpoint: str,
    lcd: str,
    addr: str,
    trade_id: int,
    *,
    indexed: dict[str, Any] | None = None,
    verified: VerifiedPosition | None = None,
    require_open_market: bool = True,
) -> PositionSnapshot:
    """Read contract state, reusing any position data checked by the caller."""
    route = trading_route(endpoint, lcd) if verified is None else verified.route
    contract = route["contractAddress"]
    position = (
        indexed if indexed is not None else indexed_position(endpoint, addr, trade_id)
    )
    _require_open_position(position)
    borrowing = position["perpBorrowing"]
    collateral = borrowing["collateralToken"]
    if collateral["symbol"] != "USDC":
        raise ValueError("Only USDC positions are supported")
    trader = position["trader"]
    token = f"TokenIndex({collateral['id']})"

    market = keeper.request(
        endpoint,
        keeper.MARKET,
        {"market": borrowing["marketId"], "collateral": collateral["id"]},
    )["perp"]["borrowing"]
    if (
        not market
        or market["marketId"] != borrowing["marketId"]
        or market["collateralToken"]["id"] != collateral["id"]
        or (require_open_market and not market["isOpen"])
    ):
        raise MarketUnavailable("Open market data is unavailable")

    def raw_trade(timeout: float, raw: dict[str, Any] | None = None) -> dict[str, Any]:
        if raw is None:
            raw = _read_position(lcd, contract, trader, trade_id, timeout)
        if not keeper.same_account(addr, raw.get("user")):
            raise PositionOwnerMismatch(
                "Contract position belongs to a different account"
            )
        if (
            raw.get("user_trade_index") != f"UserTradeIndex({trade_id})"
            or raw.get("market_index") != f"MarketIndex({borrowing['marketId']})"
            or raw.get("collateral_index") != token
            or raw.get("is_open") is not True
            or raw.get("trade_type") != "trade"
        ):
            raise PositionChanged("Position changed. Review the margin change again.")
        return raw

    raw = raw_trade(20, None if verified is None else verified.raw)
    if not _same_margin(raw, position):
        end = time.monotonic() + INDEX_RETRY_BUDGET
        for delay in INDEX_RETRY_DELAYS:
            if end - time.monotonic() <= delay:
                break
            time.sleep(delay)
            left = end - time.monotonic()
            if left <= 0:
                break
            position = indexed_position(endpoint, addr, trade_id, timeout=left)
            _require_open_position(position)
            if position["perpBorrowing"] != borrowing:
                raise PositionChanged(
                    "Position changed. Review the margin change again."
                )
            left = end - time.monotonic()
            if left <= 0:
                break
            raw = raw_trade(left)
            if _same_margin(raw, position) and time.monotonic() < end:
                break
        if time.monotonic() >= end or not _same_margin(raw, position):
            raise IndexLag(INDEX_LAG)
    observed_at = intents.now()
    funds = exchange_balance(lcd, contract, trader, token)
    return PositionSnapshot(raw, funds, market, route, observed_at)


def exchange_balance(
    lcd: str, contract: str, trader: str, token: str, *, height: str | None = None
) -> dict[str, Any]:
    message = {"get_exchange_balance": {"user": trader, "collateral_index": token}}
    funds = (
        smart(lcd, contract, message)
        if height is None
        else smart_value(lcd, contract, message, height=height)
    )
    if not isinstance(funds, dict):
        raise TypeError("Contract returned invalid exchange balance")
    if funds.get("collateral_index") != token:
        raise PositionChanged("Position changed. Review the margin change again.")
    return funds


def contract_decimal(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise TypeError("Expected a contract decimal string")
    return (
        Decimal(0) if value == "0" else intents.positive(value, "Contract quote input")
    )


def collateral_price(
    lcd: str, state: PositionSnapshot, *, height: str | None = None
) -> Decimal:
    query = {
        "get_collateral_price": {"collateral_index": state.raw["collateral_index"]}
    }
    # A replacement preview must see the new price even after a cached quote.
    return contract_decimal(
        smart_value(lcd, state.contract, query, height=height, fresh=True)
    )


def optional_collateral_price(
    lcd: str, state: PositionSnapshot, *, height: str | None = None
) -> Decimal | None:
    try:
        price = collateral_price(lcd, state, height=height)
        return price if price > 0 else None
    except (ValueError, TypeError, KeyError):
        return None


def collateral_amounts(
    before: int, after: int, price: Decimal | None
) -> dict[str, str | None]:
    def units(amount: int) -> Decimal:
        return Decimal(f"{amount}e-6")

    def usd(amount: int) -> str | None:
        if price is None:
            return None
        # Preserve the precision of both the base-unit amount and oracle price.
        with localcontext() as context:
            context.prec = len(str(amount)) + len(price.as_tuple().digits)
            return format((units(amount) * price).normalize(), "f")

    return {
        "beforeUsdc": format(units(before), "f"),
        "afterUsdc": format(units(after), "f"),
        "beforeUsd": usd(before),
        "afterUsd": usd(after),
    }


def market_oracle_price(
    lcd: str, contract: str, pair: dict[str, Any], height: str
) -> dict[str, Any]:
    """Read Perp's market rate at the pinned block, independent of collateral."""
    indexes = {}
    for side in ("base", "quote"):
        index = re.fullmatch(r"TokenIndex\(([0-9]{1,5})\)", pair[side])
        if index is None or (value := int(index[1])) > 65535:
            raise ValueError("Invalid Perp market token index")
        indexes[side] = value
    address = smart_value(lcd, contract, {"get_oracle_address": {}}, height=height)
    if not isinstance(address, str) or not re.fullmatch(r"nibi1[a-zA-Z0-9]+", address):
        raise ValueError("Perp oracle contract is unavailable")
    rate = smart_value(lcd, address, {"get_exchange_rate": indexes}, height=height)
    if not isinstance(rate, dict):
        raise TypeError("Invalid Perp market oracle price")
    return rate


def perp_prices(lcd: str, state: PositionSnapshot, height: str) -> Any:
    return smart_value(
        lcd,
        state.contract,
        {
            "get_perp_prices": {
                "market_index": state.raw["market_index"],
                "collateral_index": state.raw["collateral_index"],
            }
        },
        height=height,
    )


def leverage_inputs(
    lcd: str, state: PositionSnapshot, trade_id: int, *, height: str | None = None
) -> tuple[Decimal | None, dict[str, Any]]:
    """Read the execution leverage and the contract inputs that determine it."""
    raw = state.raw
    inputs: dict[str, Any] = {
        "marketIndex": raw["market_index"],
        "collateralIndex": raw["collateral_index"],
    }
    stored = contract_decimal(raw["leverage"])

    def query(message: dict[str, Any]) -> Any:
        if height is None:
            return smart_value(lcd, state.contract, message)
        return smart_value(lcd, state.contract, message, height=height)

    try:
        effective = _effective_leverage(query, raw["user"], trade_id, stored, inputs)
    except QueryHeightMismatch:
        raise
    except (ValueError, TypeError, KeyError):
        # An unavailable query does not establish that the cap is inactive.
        return None, {**inputs, "effectiveLeverage": None}
    return effective, {**inputs, "effectiveLeverage": str(effective)}


def _effective_leverage(
    query: Callable[[dict[str, Any]], Any],
    trader: str,
    trade_id: int,
    stored: Decimal,
    inputs: dict[str, Any],
) -> Decimal:
    """Mirror Perp's get_effective_leverage for an open, filled position."""
    market_index = inputs["marketIndex"]
    enabled = query({"is_after_hours_market": {"market_index": market_index}})
    if not isinstance(enabled, bool):
        raise TypeError("Expected the contract's after-hours flag")
    inputs["afterHoursMarket"] = enabled
    if not enabled:
        return stored

    market = query({"get_market": {"index": market_index}})
    if not isinstance(market, dict):
        raise TypeError("Expected the contract market")
    opened, closed = market["block_last_opened"], market["block_last_closed"]
    if any(value is not None and type(value) is not int for value in (opened, closed)):
        raise ValueError("Expected the market's session blocks")
    inputs.update(marketLastOpenedBlock=opened, marketLastClosedBlock=closed)
    if opened is None or closed is None:
        return stored
    if opened > closed:
        infos = query(
            {
                "get_trade_infos": {
                    "trader": trader,
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

    cap = contract_decimal(query({"get_after_hours_leverage": {}}))
    inputs["afterHoursLeverageCap"] = str(cap)
    return min(stored, cap)


def indexed_position(
    endpoint: str, addr: str, trade_id: int, *, timeout: float = 20
) -> dict[str, Any]:
    """Look up a position and verify that its trader and index match."""
    position = keeper.request(
        endpoint, keeper.POSITION, {"trader": addr, "id": trade_id}, timeout=timeout
    )["perp"]["trade"]
    if not position:
        raise PositionUnavailable("Position not found for this account")
    if not keeper.same_account(addr, position.get("trader")):
        raise PositionOwnerMismatch("Keeper position belongs to a different account")
    if type(position.get("id")) is not int or position["id"] != trade_id:
        raise PositionUnavailable("Keeper returned a different position index")
    return position


def _require_open_position(position: dict[str, Any]) -> None:
    if not position["isOpen"] or position["tradeType"] != "trade":
        raise PositionUnavailable("Open position not found")


def _same_margin(raw: dict[str, Any], position: dict[str, Any]) -> bool:
    return str(raw.get("collateral_amount")) == str(
        position["collateralAmount"]
    ) and float(str(raw.get("leverage"))) == float(position["leverage"])


def margin_amount_usdc(amount: str) -> int:
    requested = intents.positive(amount, "Margin amount")
    scaled_amount = requested * 1_000_000
    if scaled_amount != scaled_amount.to_integral_value():
        raise ValueError("Margin amount must have at most six decimal places")
    return int(scaled_amount)


def margin_quote(
    raw: dict[str, Any],
    funds: dict[str, Any] | None,
    market: dict[str, Any],
    direction: str,
    delta: int,
    *,
    effective_leverage: Decimal,
) -> dict[str, str]:
    collateral = int(raw["collateral_amount"])
    leverage_before = intents.positive(str(raw["leverage"]), "Current leverage")
    if effective_leverage <= 0:
        raise ValueError("Cannot quote a margin change with zero effective leverage")
    numerator, denominator = effective_leverage.as_integer_ratio()
    old = numerator * 10**18 // denominator
    if collateral <= 0 or direction not in ("add", "remove"):
        raise ValueError("Invalid margin input")
    if direction == "remove" and delta >= collateral:
        raise ValueError("Keep some margin in the position; close the position to exit")
    target = collateral + delta if direction == "add" else collateral - delta
    numerator = collateral * old
    next_leverage = (
        (numerator + target - 1) // target
        if direction == "add"
        else numerator // target
    )
    if (direction == "add" and next_leverage > old) or (
        direction == "remove" and next_leverage < old
    ):
        raise ValueError("Target leverage would reverse the requested margin direction")
    minimum = Decimal(str(market["minLeverage"])) * 10**18
    maximum = Decimal(str(market["maxLeverage"])) * 10**18
    if (
        next_leverage <= 0
        or next_leverage > maximum
        or (direction == "add" and next_leverage < minimum)
    ):
        raise ValueError("Resulting leverage is outside market limits")
    after = numerator // next_leverage
    actual = after - collateral if direction == "add" else collateral - after
    if next_leverage == old or actual <= 0 or actual > delta:
        raise ValueError("Margin change is too small after rounding")
    if (
        funds is not None
        and direction == "add"
        and actual > int(funds["available_to_trade"])
    ):
        raise ValueError("Insufficient balance on Sai")
    whole, fraction = divmod(next_leverage, 10**18)
    leverage = f"{whole}.{fraction:018d}".rstrip("0").rstrip(".")
    return {
        "requestedLeverage": leverage,
        "collateralBefore": str(collateral),
        "leverageBefore": format(leverage_before, "f"),
        "collateralAfter": str(after),
        "actualDeltaUsdc": str(actual),
    }
