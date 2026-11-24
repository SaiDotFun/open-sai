"""Exact on-chain margin quotes for the Keeper leverage intent."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import socket
import time
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from secrets import token_hex
from threading import BoundedSemaphore, Thread
from typing import Any, Literal
from urllib.parse import quote

import httpx

from . import intents, keeper
from .errors import Code, SaiError

MAINNET_LCD = "https://lcd.nibiru.fi"
BLOCK_HEIGHT_HEADER = "x-cosmos-block-height"
BLOCK_PATH = "/cosmos/base/tendermint/v1beta1/blocks/"
INDEX_RETRY_DELAYS = (0.25, 0.5)
INDEX_RETRY_BUDGET = 2.0
INDEX_LAG = (
    "Indexed position differs from contract state. Retry after Keeper"
    " synchronizes. No intent was submitted."
)
_LCD_DNS_SLOTS = BoundedSemaphore(8)
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


class IndexLag(ValueError):
    """Keeper's position did not catch up within the re-read budget."""


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


class QueryHeightMismatch(SaiError, ValueError):
    """The LCD answered a pinned query at an unverified height."""

    code = Code.PREVIEW_MISMATCH


class LCDTimeout(ValueError):
    """An overview LCD request ran out of time."""


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
    url: str,
    deadline: float,
    headers: dict[str, str] | None,
    params: dict[str, str] | None,
) -> httpx.Response:
    async def request() -> httpx.Response:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("LCD read deadline exceeded")
        async with asyncio.timeout(remaining):
            async with httpx.AsyncClient(timeout=None) as client:
                return await client.get(url, headers=headers, params=params)

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
        params = {"_sai_query": token_hex(16)} if fresh or height is not None else None
        if deadline is None:
            response = httpx.get(url, timeout=timeout, headers=headers, params=params)
        else:
            response = _get_before_deadline(url, deadline, headers, params)
        response.raise_for_status()
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
            raise LCDTimeout("LCD read timed out") from exc
        if height is not None or fresh:
            raise QueryHeightMismatch(
                "Unable to verify contract state during confirmation."
                " Retry the preview and confirmation."
            ) from exc
        raise ValueError("Unable to read contract state") from exc
    query_height = response.headers.get(f"grpc-metadata-{BLOCK_HEIGHT_HEADER}")
    if query_height is None and allow_unprefixed_height:
        query_height = response.headers.get(BLOCK_HEIGHT_HEADER)
    if height is not None and query_height != height:
        raise QueryHeightMismatch(
            "LCD did not serve the requested contract query height."
            " Retry the preview and confirmation."
        )
    return result, query_height


def _smart_url(lcd: str, contract: str, message: dict[str, Any]) -> str:
    encoded = quote(
        base64.b64encode(json.dumps(message, separators=(",", ":")).encode()).decode(),
        safe="",
    )
    return f"{lcd.rstrip('/')}/cosmwasm/wasm/v1/contract/{contract}/smart/{encoded}"


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
        f"{lcd.rstrip('/')}{BLOCK_PATH}{height}",
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
        if isinstance(failure, httpx.HTTPStatusError) and re.search(
            r"type: cosmwasm_std::math::decimal::Decimal; key: \[[^\]]*\] not found",
            failure.response.text,
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
        if isinstance(failure, httpx.HTTPStatusError) and re.search(
            rf"Trade not found for trader \S+ with index {trade_id}\b",
            failure.response.text,
        ):
            raise ValueError(
                f"Trade {trade_id} was not found for this account."
            ) from exc
        raise ValueError(f"Unable to read pending order {trade_id} from Perp.") from exc


@dataclass(frozen=True)
class PositionSnapshot:
    raw: dict[str, Any]
    funds: dict[str, Any]
    market: dict[str, Any]
    contract: str
    observed_at: str | None = None


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
        "user",
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
        height is None
        or not _same_position(state.raw, latest)
        or latest.get("long") is not state.raw.get("long")
        or latest.get("open_price") != state.raw.get("open_price")
    ):
        raise ValueError("Cannot verify the quoted position at the LCD query height")
    return height


def position_unchanged(
    lcd: str, state: PositionSnapshot, trade_id: int, quoted_inputs: dict[str, Any]
) -> bool:
    """Recheck the on-chain quote inputs at the final position's block."""
    latest, height = _latest_position(lcd, state, trade_id)
    if not _same_position(state.raw, latest):
        return False
    _, inputs = leverage_inputs(lcd, state, trade_id, height=height)
    quoted_leverage = {
        key: value
        for key, value in quoted_inputs.items()
        if key != "collateralPriceUsd"
    }
    if inputs != quoted_leverage:
        return False
    if height is None:
        raise ValueError(
            "Cannot confirm the position and leverage at the same block."
            " Retry with an LCD that reports the contract query height."
        )
    if "collateralPriceUsd" not in quoted_inputs:
        return True
    return (
        str(collateral_price(lcd, state, height=height))
        == quoted_inputs["collateralPriceUsd"]
    )


def block_timestamp(lcd: str, height: str) -> int:
    """Read the UTC timestamp of a specific LCD block."""
    result = _get_json(
        f"{lcd.rstrip('/')}/cosmos/base/tendermint/v1beta1/blocks/{height}"
    )
    block = result.get("sdk_block") or result.get("block")
    if not isinstance(block, dict):
        raise TypeError("LCD block header is unavailable")
    header = block["header"]
    time = datetime.fromisoformat(header["time"])
    if header["height"] != height or time.tzinfo is None:
        raise ValueError("Cannot verify the timestamp of the quoted LCD block")
    return int(time.timestamp())


def trading_contract(endpoint: str, lcd: str) -> str:
    route = keeper.request(endpoint, keeper.ROUTE)["oneClickTrading"]
    node = _get_json(f"{lcd.rstrip('/')}/cosmos/base/tendermint/v1beta1/node_info")
    info = node.get("default_node_info")
    if (
        not route["enabled"]
        or not isinstance(info, dict)
        or info.get("network") != route["chainId"]
    ):
        raise ValueError("Keeper trading route and LCD network do not match")
    return route["contractAddress"]


def snapshot(endpoint: str, lcd: str, addr: str, trade_id: int) -> PositionSnapshot:
    contract = trading_contract(endpoint, lcd)
    position = _position(endpoint, addr, trade_id, timeout=20)
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
        or not market["isOpen"]
    ):
        raise MarketUnavailable("Open market data is unavailable")

    def raw_trade(timeout: float) -> dict[str, Any]:
        raw = _read_position(lcd, contract, trader, trade_id, timeout)
        if (
            raw.get("user") != trader
            or raw.get("user_trade_index") != f"UserTradeIndex({trade_id})"
            or raw.get("market_index") != f"MarketIndex({borrowing['marketId']})"
            or raw.get("collateral_index") != token
            or raw.get("is_open") is not True
            or raw.get("trade_type") != "trade"
        ):
            raise PositionChanged("Position changed. Review the margin change again.")
        return raw

    raw = raw_trade(20)
    if not _same_margin(raw, position):
        end = time.monotonic() + INDEX_RETRY_BUDGET
        for delay in INDEX_RETRY_DELAYS:
            if end - time.monotonic() <= delay:
                break
            time.sleep(delay)
            left = end - time.monotonic()
            if left <= 0:
                break
            position = _position(endpoint, addr, trade_id, timeout=left)
            if position["trader"] != trader or position["perpBorrowing"] != borrowing:
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
    funds = smart(
        lcd,
        contract,
        {"get_exchange_balance": {"user": trader, "collateral_index": token}},
    )
    if funds.get("collateral_index") != token:
        raise PositionChanged("Position changed. Review the margin change again.")
    return PositionSnapshot(raw, funds, market, contract, observed_at)


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


def _position(
    endpoint: str, addr: str, trade_id: int, *, timeout: float
) -> dict[str, Any]:
    position = keeper.request(
        endpoint, keeper.POSITION, {"trader": addr, "id": trade_id}, timeout=timeout
    )["perp"]["trade"]
    if not position or not position["isOpen"] or position["tradeType"] != "trade":
        raise PositionUnavailable("Open position not found")
    account = keeper.canonical_account(addr)
    if account is None or keeper.canonical_account(position.get("trader")) != account:
        raise PositionOwnerMismatch("Keeper position belongs to a different account")
    return position


def _same_margin(raw: dict[str, Any], position: dict[str, Any]) -> bool:
    return str(raw.get("collateral_amount")) == str(
        position["collateralAmount"]
    ) and float(str(raw.get("leverage"))) == float(position["leverage"])


def margin_quote(
    raw: dict[str, Any],
    funds: dict[str, Any],
    market: dict[str, Any],
    direction: str,
    amount: str,
    *,
    effective_leverage: Decimal,
) -> dict[str, str]:
    requested = intents.positive(amount, "Margin amount")
    scaled_amount = requested * 1_000_000
    if scaled_amount != scaled_amount.to_integral_value():
        raise ValueError("Margin amount must have at most six decimal places")
    delta = int(scaled_amount)
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
    if direction == "add" and actual > int(funds["available_to_trade"]):
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
