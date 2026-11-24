"""Read one account's trading state without reconciling intents or signing in."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import cache
from typing import Any

import httpx

from . import keeper, session, storage, stream
from .settings import Settings

PAGE_SIZE = 100
BALANCE_KEYS = {"USDC on Sai": "balance", "USDC Credits on Sai": "credits"}
# Sai's vaults denominate both USDC and stNIBI collateral in six decimal base units.
COLLATERAL_SCALE = {"USDC": 1_000_000, "stNIBI": 1_000_000}


class AccountUnavailable(ValueError):
    """The default account could not be resolved for this endpoint."""


def _failure(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": retryable}


def _read(action: Callable[[], Any]) -> dict[str, Any]:
    try:
        data = action()
    except (keeper.KeeperError, stream.StreamError) as exc:
        code = {
            keeper.NotSent: "NOT_SENT",
            stream.StreamError: "STREAM_ERROR",
        }.get(type(exc), "KEEPER_ERROR")
        return {"fetchedAt": None, "error": _failure(code, str(exc), retryable=True)}
    except (OSError, sqlite3.Error):
        return {
            "fetchedAt": None,
            "error": _failure("LOCAL_IO_ERROR", "Could not read local Sai data"),
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return {
            "fetchedAt": None,
            "error": _failure("INCOMPLETE_DATA", "The source returned invalid data"),
        }
    return {"fetchedAt": datetime.now(UTC).isoformat(), "data": data}


def _number(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("Expected a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Expected a finite number")
    return result


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Keeper sometimes sends the Unix epoch for a block with an unknown timestamp.
    return stamp if stamp.tzinfo is not None and stamp.timestamp() > 0 else None


def _unix_instant(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(_number(value), UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _age(at: datetime | None, fetched: str) -> dict[str, Any]:
    return {
        "sourceAt": at.isoformat() if at is not None else None,
        "sourceAgeSeconds": (
            round((datetime.fromisoformat(fetched) - at).total_seconds(), 3)
            if at is not None
            else None
        ),
    }


def _block_time(lcd: str, height: int) -> datetime | None:
    """Recover a missing block timestamp if the configured LCD retains it."""
    url = f"{lcd.rstrip('/')}/cosmos/base/tendermint/v1beta1/blocks/{height}"
    try:
        keeper.check_url(url, "LCD")
        response = httpx.get(url, timeout=3)
        response.raise_for_status()
        header = response.json()["block"]["header"]
        if int(header["height"]) == height:
            return _instant(header["time"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        pass
    return None


def _block_source(
    block: Mapping[str, Any], fetched: str, lookup: Callable[[int], datetime | None]
) -> dict[str, Any]:
    height = block.get("block")
    at = _instant(block.get("block_ts"))
    if at is None and isinstance(height, int) and height > 0:
        at = lookup(height)
    return {"sourceBlock": height, **_age(at, fetched)}


def _balances(settings: Settings, addr: str) -> dict[str, dict[str, Any]]:
    response = stream.snapshot(
        settings.ws_endpoint, stream.BALANCES_WITH_BLOCK, {"user": addr}
    )
    balances: dict[str, dict[str, Any]] = {
        key: {"usd": 0.0, "block": None} for key in BALANCE_KEYS.values()
    }
    for row in response["data"]["userBalances"]:
        token = row["token_info"]
        if token["type"] == "sai" and token["name"] in BALANCE_KEYS:
            balances[BALANCE_KEYS[token["name"]]] = {
                "usd": _number(row["usd_value"]),
                "block": row["lastUpdatedBlock"],
            }
    return balances


def _pages(
    endpoint: str, query: str, field: str, id_field: str, addr: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    while True:
        response = keeper.request(
            endpoint, query, {"trader": addr, "limit": PAGE_SIZE, "offset": len(rows)}
        )
        page = response["perp"][field]
        if not isinstance(page, list):
            raise TypeError("Expected a list")
        for row in page:
            item_id = row[id_field]
            if item_id in seen:
                raise keeper.KeeperError(
                    "Orders or positions changed during the read. Retry."
                )
            seen.add(item_id)
            rows.append(row)
        if len(page) < PAGE_SIZE:
            return rows


def _trades(endpoint: str, addr: str) -> list[dict[str, Any]]:
    rows = _pages(endpoint, keeper.OVERVIEW_TRADES, "trades", "id", addr)
    for row in rows:
        if row["tradeType"] not in {"trade", "limit", "stop"}:
            raise ValueError("Unknown trade type")
    return rows


def _prices(endpoint: str) -> dict[int, dict[str, Any]]:
    rows = keeper.request(endpoint, keeper.PRICES)["oracle"]["tokenPricesUsd"]
    prices = {}
    for row in rows:
        price = _number(row["priceUsd"])
        if price > 0:
            prices[row["token"]["id"]] = {
                "usd": price,
                "block": row["lastUpdatedBlock"],
                "token": row["token"]["symbol"] or row["token"]["name"],
            }
    return prices


def _distance(mark: float | None, level: float | None, above: bool) -> float | None:
    if mark is None or level is None:
        return None
    # A negative distance means the mark crossed onto the trigger side.
    return (level - mark) / mark * (100 if above else -100)


def _level(trade: dict[str, Any], name: str) -> float | None:
    value = trade[name]
    if value is None:
        return None
    number = _number(value)
    return number if number > 0 else None


def _trade_row(trade: dict[str, Any], prices: Mapping[int, Any]) -> dict[str, Any]:
    market = trade["perpBorrowing"]
    collateral = market["collateralToken"]
    base = market["baseToken"]
    scale = COLLATERAL_SCALE.get(collateral["symbol"])
    collateral_price = prices.get(collateral["id"], {}).get("usd")
    conversion = (
        collateral_price / scale if collateral_price is not None and scale else None
    )
    amount = _number(trade["collateralAmount"])
    collateral_usd = amount * conversion if conversion is not None else None
    leverage = _number(trade["leverage"])
    is_long = trade["isLong"]
    entry = _number(trade["openPrice"])
    tp, sl = _level(trade, "tp"), _level(trade, "sl")
    row = {
        "id": trade["id"],
        "marketId": market["marketId"],
        "market": base["symbol"],
        "side": "long" if is_long else "short",
        "collateralToken": collateral["symbol"],
        "collateralTokenId": collateral["id"],
        "collateralBaseUnits": trade["collateralAmount"],
        "collateralUsd": collateral_usd,
        "leverage": leverage,
        "sizeUsd": collateral_usd * leverage if collateral_usd is not None else None,
        "takeProfitPriceUsd": tp,
        "stopLossPriceUsd": sl,
    }
    order_type = trade["tradeType"]
    if order_type != "trade":
        return {**row, "orderType": order_type, "triggerPriceUsd": entry}
    mark = prices.get(base["id"], {}).get("usd")
    state = trade["state"]
    liquidation = _number(state["liquidationPrice"]) if state is not None else None
    return {
        **row,
        "markTokenId": base["id"],
        "entryPriceUsd": entry,
        "markPriceUsd": mark,
        "unrealizedPnlUsd": (
            _number(state["pnlCollateral"]) * conversion
            if state is not None and conversion is not None
            else None
        ),
        "unrealizedPnlPercent": _number(state["pnlPct"]) * 100
        if state is not None
        else None,
        "liquidationPriceUsd": liquidation,
        "liquidationDistancePercent": _distance(mark, liquidation, not is_long),
        "takeProfitDistancePercent": _distance(mark, tp, is_long),
        "stopLossDistancePercent": _distance(mark, sl, not is_long),
    }


def _trade_sections(
    endpoint: str, addr: str, lookup: Callable[[int], datetime | None]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    snapshot = _read(lambda: _trades(endpoint, addr))
    if "error" in snapshot:
        return snapshot, snapshot.copy(), {"fetchedAt": None, "data": []}
    trades = snapshot["data"]
    prices = (
        _read(lambda: _prices(endpoint)) if trades else {"fetchedAt": None, "data": {}}
    )
    quotes = prices.get("data", {})

    def rows(opened: bool) -> list[dict[str, Any]]:
        return [
            {
                **_trade_row(trade, quotes),
                **_block_source(
                    trade["lastUpdatedBlock"], snapshot["fetchedAt"], lookup
                ),
            }
            for trade in trades
            if (trade["tradeType"] == "trade") == opened
        ]

    positions, orders = _read(lambda: rows(True)), _read(lambda: rows(False))
    for section in (positions, orders):
        if "data" in section:
            section["fetchedAt"] = snapshot["fetchedAt"]
    if "data" in positions:
        items = positions["data"]
        pnls = [row["unrealizedPnlUsd"] for row in items]
        positions["data"] = {
            "items": items,
            "unrealizedPnlUsd": None if None in pnls else sum(pnls, 0.0),
        }
    if "data" in prices:
        used_ids = {row["collateralTokenId"] for row in orders.get("data", [])}
        for row in positions.get("data", {}).get("items", []):
            used_ids.update((row["collateralTokenId"], row["markTokenId"]))
        prices["data"] = [
            {
                "tokenId": token_id,
                "token": quotes[token_id]["token"],
                "usd": quotes[token_id]["usd"],
                **_block_source(quotes[token_id]["block"], prices["fetchedAt"], lookup),
            }
            for token_id in sorted(used_ids & quotes.keys())
        ]
        if used_ids - quotes.keys():
            prices["error"] = _failure(
                "INCOMPLETE_DATA",
                "Keeper has no positive USD price for one or more tokens",
            )
    return positions, orders, prices


def _queued_rows(endpoint: str, addr: str) -> list[dict[str, Any]]:
    rows = _pages(
        endpoint, keeper.PENDING_MARKET_ORDERS, "marketOrders", "sequence", addr
    )
    return [
        {
            "sequence": row["sequence"],
            "tradeId": row["tradeId"],
            "action": row["side"],
            "marketId": row["perpMarketId"],
            "status": row["status"],
            "collateralBaseUnits": row["collateralAmount"],
            "requestTxHash": row["orderReqTxHash"],
            "requestEvmTxHash": row["orderReqEvmTxHash"],
            "block": row["orderReqBlock"],
            "block_ts": row["orderReqBlockTs"],
        }
        for row in rows
    ]


def _queued_orders(
    endpoint: str, addr: str, lookup: Callable[[int], datetime | None]
) -> dict[str, Any]:
    section = _read(lambda: _queued_rows(endpoint, addr))
    if "data" in section:
        fetched = section["fetchedAt"]
        for row in section["data"]:
            row.update(_block_source(row, fetched, lookup))
            del row["block"], row["block_ts"]
    return section


def _blocking_intents(endpoint: str, addr: str) -> list[dict[str, Any]]:
    now = datetime.now(UTC).timestamp()
    blocking = []
    for row in storage.unresolved_intents(endpoint, addr):
        if row["status"] == "unknown":
            scope = "allTrades"
        elif row["entity"]:
            scope = "sameTrade"
        elif row["kind"] == "OPEN_TRADE" and storage.intent_is_recent(
            row["intentKey"], row["createdAt"], now, keeper.ABSENT_AFTER_SECONDS
        ):
            scope = "mcpOpen"
        else:
            continue
        blocking.append(
            {
                "intentKey": row["intentKey"],
                "kind": row["kind"],
                "status": row["status"],
                "scope": scope,
                "tradeId": row["entity"] or None,
                "createdAt": row["createdAt"],
            }
        )
    return blocking


def _issued_at(stored: dict[str, str]) -> datetime | None:
    try:
        return _unix_instant(session.token_claims(stored["token"]).get("iat"))
    except (ValueError, IndexError, TypeError):
        return None


def _session_info(
    stored: dict[str, str] | None, endpoint: str, now: datetime
) -> dict[str, Any]:
    if stored is None:
        return {"connected": False}
    matches = stored.get("endpoint") == endpoint
    return {
        "connected": True,
        "expiresAt": stored["expiresAt"],
        "expired": session.expiry_time(stored) <= now,
        "endpointMatches": matches,
        **_age(_issued_at(stored), now.isoformat()),
        **(session.expiry_fields(stored, now) if matches else {}),
    }


def account_overview(settings: Settings, addr: str | None = None) -> dict[str, Any]:
    """Read one account, keeping available sections when a source fails."""
    source = "argument" if addr is not None else "session"
    try:
        accounts = storage.load_accounts()
    except ValueError as exc:
        if addr is None:
            raise AccountUnavailable(str(exc)) from exc
        session_section: dict[str, Any] = {
            "fetchedAt": None,
            "error": _failure("INCOMPLETE_DATA", str(exc)),
        }
    else:
        key = addr.lower() if addr is not None else accounts["active"]
        stored = accounts["sessions"].get(key)
        if addr is None:
            if stored is None:
                raise AccountUnavailable("No active Sai account")
            if stored.get("endpoint") != settings.endpoint:
                raise AccountUnavailable(
                    "The active session belongs to another Keeper endpoint"
                )
            addr = stored["evmAddress"]
        session_read = datetime.now(UTC)
        session_section = _read(
            lambda: _session_info(stored, settings.endpoint, session_read)
        )
        if "data" in session_section:
            session_section["fetchedAt"] = session_read.isoformat()
    assert addr is not None

    @cache
    def lookup(height: int) -> datetime | None:
        return _block_time(settings.lcd_endpoint, height)

    balances = _read(lambda: _balances(settings, addr))
    if "data" in balances:
        balances["data"] = {
            key: {
                "usd": row["usd"],
                **_block_source(row, balances["fetchedAt"], lookup),
            }
            for key, row in balances["data"].items()
        }
    positions, orders, prices = _trade_sections(settings.endpoint, addr, lookup)
    queued = _queued_orders(settings.endpoint, addr, lookup)
    intents = _read(lambda: _blocking_intents(settings.endpoint, addr))
    for row in intents.get("data", []):
        row.update(_age(_unix_instant(row.pop("createdAt")), intents["fetchedAt"]))
    return {
        "addr": addr,
        "addrSource": source,
        "balances": balances,
        "positions": positions,
        "pendingOrders": orders,
        "queuedMarketOrders": queued,
        "prices": prices,
        "blockingIntents": intents,
        "session": session_section,
    }
