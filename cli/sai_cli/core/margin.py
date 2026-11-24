"""Exact on-chain margin quotes for the Keeper leverage intent."""

from __future__ import annotations

import base64
import json
import time
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from . import intents, keeper

MAINNET_LCD = "https://lcd.nibiru.fi"
BLOCK_PATH = "/cosmos/base/tendermint/v1beta1/blocks/"


def _get_json(
    url: str, timeout: float = 20, *, height: int | None = None, fresh: bool = False
) -> dict[str, Any]:
    keeper.check_url(url, "LCD")
    try:
        options: dict[str, Any] = {}
        if height is not None:
            # A proxy may cache a URL without varying on the request header.
            options["params"] = {"sai_height": height}
            options["headers"] = {"x-cosmos-block-height": str(height)}
        elif fresh:
            options["params"] = {"sai_at": time.time_ns()}
        response = httpx.get(url, timeout=timeout, **options)
        response.raise_for_status()
        if height is not None:
            served = response.headers.get("grpc-metadata-x-cosmos-block-height")
            if served is None:
                served = response.headers.get("x-cosmos-block-height")
            if served != str(height):
                raise ValueError("LCD did not serve the requested block height")
        result = response.json()
        if not isinstance(result, dict):
            raise TypeError
        return result
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError) as exc:
        raise ValueError("Unable to read contract state") from exc


def block_header(lcd: str, height: int | str, *, timeout: float = 3) -> dict[str, Any]:
    data = _get_json(
        f"{lcd.rstrip('/')}{BLOCK_PATH}{height}", timeout, fresh=height == "latest"
    )["block"]["header"]
    if not isinstance(data, dict):
        raise TypeError("Invalid LCD block header")
    return data


def smart(
    lcd: str,
    contract: str,
    message: dict[str, Any],
    *,
    height: int | None = None,
    timeout: float = 20,
) -> dict[str, Any]:
    data = smart_data(lcd, contract, message, height=height, timeout=timeout)
    if not isinstance(data, dict):
        raise TypeError("Contract returned invalid state")
    return data


def smart_data(
    lcd: str,
    contract: str,
    message: dict[str, Any],
    *,
    height: int | None = None,
    timeout: float = 20,
) -> Any:
    encoded = quote(
        base64.b64encode(json.dumps(message, separators=(",", ":")).encode()).decode(),
        safe="",
    )
    return _get_json(
        f"{lcd.rstrip('/')}/cosmwasm/wasm/v1/contract/{contract}/smart/{encoded}",
        timeout,
        height=height,
    ).get("data")


def snapshot(
    endpoint: str, lcd: str, addr: str, trade_id: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    route = keeper.request(endpoint, keeper.ROUTE)["oneClickTrading"]
    node = _get_json(f"{lcd.rstrip('/')}/cosmos/base/tendermint/v1beta1/node_info")
    if (
        not route["enabled"]
        or node.get("default_node_info", {}).get("network") != route["chainId"]
    ):
        raise ValueError("Keeper trading route and LCD network do not match")
    position = keeper.request(
        endpoint, keeper.POSITION, {"trader": addr, "id": trade_id}
    )["perp"]["trade"]
    if not position or not position["isOpen"] or position["tradeType"] != "trade":
        raise ValueError("Open position not found")
    borrowing = position["perpBorrowing"]
    collateral = borrowing["collateralToken"]
    if collateral["symbol"] != "USDC":
        raise ValueError("Only USDC positions are supported")
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
        raise ValueError("Open market data is unavailable")
    trader = position["trader"]
    token = f"TokenIndex({collateral['id']})"
    contract = route["contractAddress"]
    raw = smart(lcd, contract, {"get_trade_raw": {"trader": trader, "index": trade_id}})
    funds = smart(
        lcd,
        contract,
        {"get_exchange_balance": {"user": trader, "collateral_index": token}},
    )
    if (
        raw.get("user") != trader
        or raw.get("user_trade_index") != f"UserTradeIndex({trade_id})"
        or raw.get("market_index") != f"MarketIndex({borrowing['marketId']})"
        or raw.get("collateral_index") != token
        or funds.get("collateral_index") != token
        or raw.get("is_open") is not True
        or raw.get("trade_type") != "trade"
    ):
        raise ValueError("Position changed; review the margin change again")
    if str(raw.get("collateral_amount")) != str(position["collateralAmount"]) or float(
        str(raw.get("leverage"))
    ) != float(position["leverage"]):
        raise ValueError(
            "Indexed position differs from contract state; wait for synchronization and retry"
        )
    return raw, funds, market


def margin_quote(
    raw: dict[str, Any],
    funds: dict[str, Any],
    market: dict[str, Any],
    direction: str,
    amount: str,
) -> dict[str, str]:
    requested = intents.positive(amount, "Margin amount")
    scaled_amount = requested * 1_000_000
    if scaled_amount != scaled_amount.to_integral_value():
        raise ValueError("Margin amount must have at most six decimal places")
    delta = int(scaled_amount)
    collateral = int(raw["collateral_amount"])
    old = int(intents.positive(str(raw["leverage"]), "Current leverage") * 10**18)
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
    leverage = format(Decimal(next_leverage) / Decimal(10**18), "f")
    if "." in leverage:
        leverage = leverage.rstrip("0").rstrip(".")
    return {
        "requestedLeverage": leverage,
        "collateralBefore": str(collateral),
        "collateralAfter": str(after),
        "actualDeltaUsdc": str(actual),
    }
