"""Read one account's trading state without reconciling intents or signing in."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from functools import cache
from typing import Any

from . import keeper, margin, session, storage, stream
from .settings import Settings

PAGE_SIZE = 100
MAX_PAGES = 20
BALANCE_KEYS = {
    "USDC on Sai": "balance",
    "USDC Credits on Sai": "credits",
    "USDC Unsettled on Sai": "unsettled",
}
# Sai's vaults denominate both USDC and stNIBI collateral in six decimal base units.
COLLATERAL_SCALE = {"USDC": 1_000_000, "stNIBI": 1_000_000}
# contracts/perp/src/constants.rs defines MAX_PNL_P as a fraction.
MAX_GROSS_PNL = Decimal(9)
USD_TOKEN_ID = 0


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
    except (KeyError, TypeError, ValueError, OverflowError, DecimalException):
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


@dataclass(frozen=True)
class ChainRead:
    lcd: str
    chain_id: str
    contract: str
    height: int
    time: datetime

    def query(self, message: dict[str, Any]) -> dict[str, Any]:
        return margin.smart(
            self.lcd, self.contract, message, height=self.height, timeout=3
        )

    def block_time(self, height: int) -> datetime | None:
        if height == self.height:
            return self.time
        try:
            header = margin.block_header(self.lcd, height)
            if header["chain_id"] == self.chain_id and int(header["height"]) == height:
                return _instant(header["time"])
        except (KeyError, TypeError, ValueError):
            pass
        return None


def _chain_read(settings: Settings) -> ChainRead | None:
    if (
        settings.endpoint != keeper.MAINNET_HTTP
        and settings.sources.get("lcd.endpoint") == "default"
    ):
        return None
    try:
        route = keeper.request(settings.endpoint, keeper.ROUTE, timeout=3)[
            "oneClickTrading"
        ]
        header = margin.block_header(settings.lcd_endpoint, "latest")
        at = _instant(header["time"])
        height = int(header["height"])
        chain_id = route["chainId"]
        contract = route["contractAddress"]
        if (
            isinstance(chain_id, str)
            and bool(chain_id)
            and header["chain_id"] == chain_id
            and at is not None
            and height > 0
            and isinstance(contract, str)
            and contract.startswith("nibi1")
            and contract.isalnum()
        ):
            return ChainRead(settings.lcd_endpoint, chain_id, contract, height, at)
    except (keeper.KeeperError, KeyError, TypeError, ValueError):
        pass
    return None


def _block_source(
    block: Mapping[str, Any],
    fetched: str,
    lookup: Callable[[int], datetime | None] | None,
) -> dict[str, Any]:
    height = block.get("block")
    at = _instant(block.get("block_ts"))
    if at is None and lookup is not None and isinstance(height, int) and height > 0:
        at = lookup(height)
    return {"sourceBlock": height, **_age(at, fetched)}


def _balances(settings: Settings, addr: str) -> dict[str, dict[str, Any]]:
    response = stream.snapshot(
        settings.ws_endpoint, stream.BALANCES_WITH_BLOCK, {"user": addr}
    )
    balances: dict[str, dict[str, Any]] = {}
    for row in response["data"]["userBalances"]:
        token = row["token_info"]
        if token["type"] == "sai" and token["name"] in BALANCE_KEYS:
            balances[BALANCE_KEYS[token["name"]]] = {
                "usd": _number(row["usd_value"]),
                "block": row["lastUpdatedBlock"],
            }
    return balances


@dataclass
class Pages:
    rows: list[dict[str, Any]]
    complete: bool
    paginated: bool


def _scan_pages(
    endpoint: str, query: str, field: str, id_field: str, addr: str
) -> Pages:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for page_num in range(MAX_PAGES):
        response = keeper.request(
            endpoint, query, {"trader": addr, "limit": PAGE_SIZE, "offset": len(rows)}
        )
        page = response["perp"][field]
        if not isinstance(page, list):
            raise TypeError("Expected a list")
        fetched = datetime.now(UTC).isoformat()
        for row in page:
            item_id = row[id_field]
            if item_id in seen:
                return Pages(rows, False, True)
            seen.add(item_id)
            rows.append({**row, "fetchedAt": fetched})
        if len(page) < PAGE_SIZE:
            return Pages(rows, True, page_num > 0)
    return Pages(rows, False, True)


def _pages(endpoint: str, query: str, field: str, id_field: str, addr: str) -> Pages:
    snapshot = _scan_pages(endpoint, query, field, id_field, addr)
    if not snapshot.paginated:
        return snapshot
    # The live Keeper query has offsets and no cursor. Confirm a paginated
    # list with a second scan so a close between pages cannot silently skip a row.
    for _ in range(2):
        current = _scan_pages(endpoint, query, field, id_field, addr)
        if (
            snapshot.complete
            and current.complete
            and [row[id_field] for row in snapshot.rows]
            == [row[id_field] for row in current.rows]
        ):
            return current
        snapshot = current
    return Pages(snapshot.rows, False, snapshot.paginated)


def _trades(endpoint: str, addr: str) -> Pages:
    snapshot = _pages(endpoint, keeper.OVERVIEW_TRADES, "trades", "id", addr)
    for row in snapshot.rows:
        if row["tradeType"] not in {"trade", "limit", "stop"}:
            raise ValueError("Unknown trade type")
    return snapshot


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


def _distance(mark: Decimal, level: Decimal | None, above: bool) -> float | None:
    if level is None:
        return None
    # Negative means the mark crossed onto the trigger side.
    return float((level - mark) / mark * (100 if above else -100))


def _decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Expected a finite decimal")
    return result


def _level(value: Any) -> Decimal | None:
    if value is None:
        return None
    result = _decimal(value)
    return result if result > 0 else None


def _usd(value: Decimal | None, price: Decimal | None) -> float | None:
    return float(value * price) if value is not None and price is not None else None


def _agrees(indexed: dict[str, Any], raw: dict[str, Any]) -> bool:
    market = indexed["perpBorrowing"]
    return (
        raw["user"] == indexed["trader"]
        and raw["user_trade_index"] == f"UserTradeIndex({indexed['id']})"
        and raw["market_index"] == f"MarketIndex({market['marketId']})"
        and raw["collateral_index"] == f"TokenIndex({market['collateralToken']['id']})"
        and raw["is_open"] is True
        and raw["trade_type"] == "trade"
        and raw["long"] is indexed["isLong"]
        and int(raw["collateral_amount"]) == indexed["collateralAmount"]
        and float(_decimal(raw["leverage"])) == indexed["leverage"]
        and float(_decimal(raw["open_price"])) == indexed["openPrice"]
        and [_usd(_level(raw[name]), Decimal(1)) for name in ("tp", "sl")]
        == [_usd(_level(indexed[name]), Decimal(1)) for name in ("tp", "sl")]
    )


def _oracle_time(value: Any, chain: ChainRead) -> datetime | None:
    at = _unix_instant(value)
    return at if at is not None and 0 < at.timestamp() and at <= chain.time else None


def _contract_risk(
    trade: dict[str, Any],
    chain: ChainRead | None,
    addr: str,
    get_market_price: Callable[[int, int], tuple[dict[str, Any], str]],
    conversion: Decimal | None,
) -> dict[str, Any]:
    if chain is None:
        return {"riskStatus": "unavailable", "riskReason": "No matching LCD block"}
    market = trade["perpBorrowing"]
    risk: dict[str, Any] = {"riskStatus": "unavailable"}
    try:
        result = chain.query({"get_trade": {"trader": addr, "index": trade["id"]}})
        fetched = datetime.now(UTC).isoformat()
        raw = result["trade"]
        if not _agrees(trade, raw):
            return {
                "riskStatus": "inconsistent",
                "riskReason": "Keeper and contract trade values differ",
            }
        if result.get("needs_after_hours_trigger"):
            return {
                "riskStatus": "unavailable",
                "riskReason": "The contract reports an after-hours leverage adjustment",
            }
        liquidation = _decimal(result["liquidation_price"])
        entry = _decimal(raw["open_price"])
        quote_id = market["quoteToken"]["id"]
        if liquidation < 0 or entry <= 0:
            raise ValueError("Invalid contract trade price")
        risk = {
            "riskStatus": "partial",
            "riskSource": {
                "fetchedAt": fetched,
                "sourceBlock": chain.height,
                **_age(chain.time, fetched),
            },
            "liquidationPriceQuote": float(liquidation),
            "liquidationPriceUsd": float(liquidation)
            if quote_id == USD_TOKEN_ID
            else None,
        }
        market_price, price_fetched = get_market_price(
            market["marketId"], market["collateralToken"]["id"]
        )
        mark = _decimal(market_price["base_per_quote"])
        quote_usd = _decimal(market_price["quote_price_usd"])
        base_at = _oracle_time(market_price.get("base_last_update_time"), chain)
        quote_at = _oracle_time(market_price.get("quote_last_update_time"), chain)
        if min(mark, quote_usd) <= 0:
            raise ValueError("Invalid contract oracle price")
        if quote_id == USD_TOKEN_ID and quote_usd != 1:
            return {
                "riskStatus": "inconsistent",
                "riskReason": "Keeper and contract disagree on the market quote",
            }
        if base_at is None or (quote_id != USD_TOKEN_ID and quote_at is None):
            return {
                **risk,
                "riskReason": "Contract oracle source time is unknown",
            }
        is_long = raw["long"]
        pnl = (
            ((mark - entry) if is_long else (entry - mark))
            / entry
            * _decimal(raw["leverage"])
        )
        pnl = max(Decimal(-1), min(MAX_GROSS_PNL, pnl))
        tp, sl = _level(raw["tp"]), _level(raw["sl"])
        return {
            **risk,
            "riskStatus": "verified",
            "markSource": {
                "fetchedAt": price_fetched,
                "sourceBlock": chain.height,
                **_age(
                    min(base_at, quote_at)
                    if quote_at and quote_id != USD_TOKEN_ID
                    else base_at,
                    price_fetched,
                ),
                "base": _age(base_at, price_fetched),
                "quote": _age(
                    quote_at if quote_id != USD_TOKEN_ID else None, price_fetched
                ),
            },
            "entryPriceUsd": _usd(entry, quote_usd),
            "markPriceUsd": _usd(mark, quote_usd),
            "takeProfitPriceUsd": _usd(tp, quote_usd),
            "stopLossPriceUsd": _usd(sl, quote_usd),
            "liquidationPriceUsd": _usd(liquidation, quote_usd),
            "unrealizedPnlUsd": _usd(
                pnl * Decimal(raw["collateral_amount"]), conversion
            ),
            "unrealizedPnlPercent": float(pnl * 100),
            "liquidationDistancePercent": _distance(mark, liquidation, not is_long),
            "takeProfitDistancePercent": _distance(mark, tp, is_long),
            "stopLossDistancePercent": _distance(mark, sl, not is_long),
        }
    except (KeyError, TypeError, ValueError, OSError, OverflowError, DecimalException):
        return {
            **risk,
            "riskReason": "Could not read matching contract risk data at one block",
        }


def _trade_row(
    trade: dict[str, Any],
    prices: Mapping[int, Any],
    read_risk: Callable[[dict[str, Any], Decimal | None], dict[str, Any]],
) -> dict[str, Any]:
    market = trade["perpBorrowing"]
    collateral, quote = market["collateralToken"], market["quoteToken"]
    scale = COLLATERAL_SCALE.get(collateral["symbol"])
    collateral_price = (
        1
        if collateral["symbol"] == "USDC"
        else prices.get(collateral["id"], {}).get("usd")
    )
    conversion = (
        _decimal(collateral_price) / scale
        if collateral_price is not None and scale
        else None
    )
    amount = _decimal(trade["collateralAmount"])
    leverage = _decimal(trade["leverage"])
    quote_price = (
        1 if quote["id"] == USD_TOKEN_ID else prices.get(quote["id"], {}).get("usd")
    )
    quote_usd = _decimal(quote_price) if quote_price is not None else None
    entry = _decimal(trade["openPrice"])
    tp, sl = _level(trade["tp"]), _level(trade["sl"])
    row = {
        "id": trade["id"],
        "marketId": market["marketId"],
        "market": market["baseToken"]["symbol"],
        "side": "long" if trade["isLong"] else "short",
        "collateralToken": collateral["symbol"],
        "collateralTokenId": collateral["id"],
        "collateralBaseUnits": trade["collateralAmount"],
        "collateralUsd": _usd(amount, conversion),
        "leverage": float(leverage),
        "sizeUsd": _usd(amount * leverage, conversion),
        "quoteToken": quote["symbol"] or quote["name"],
        "quoteTokenId": quote["id"],
        "takeProfitPriceUsd": _usd(tp, quote_usd),
        "stopLossPriceUsd": _usd(sl, quote_usd),
    }
    order_type = trade["tradeType"]
    if order_type != "trade":
        return {
            **row,
            "orderType": order_type,
            "triggerPriceUsd": _usd(entry, quote_usd),
        }
    risk = read_risk(trade, conversion)
    fallback: dict[str, Any] = {}
    if "riskSource" not in risk and trade["state"] is not None:
        # Keeper does not expose when its independently indexed state was calculated.
        fallback = {
            "keeperLiquidationEstimatePriceQuote": _number(
                trade["state"]["liquidationPrice"]
            ),
            "keeperLiquidationEstimateSource": {
                "fetchedAt": trade["fetchedAt"],
                **_age(None, trade["fetchedAt"]),
            },
        }
    return {
        **row,
        "entryPriceUsd": _usd(entry, quote_usd),
        "markPriceUsd": None,
        "unrealizedPnlUsd": None,
        "unrealizedPnlPercent": None,
        "unrealizedPnlBasis": "grossBeforeFees",
        "liquidationPriceUsd": None,
        "liquidationDistancePercent": None,
        "takeProfitDistancePercent": None,
        "stopLossDistancePercent": None,
        **risk,
        **fallback,
    }


def _mark_incomplete(section: dict[str, Any]) -> None:
    section["error"] = _failure(
        "INCOMPLETE_DATA",
        "The paginated list could not be confirmed and may be incomplete",
        retryable=True,
    )


def _trade_sections(
    settings: Settings,
    addr: str,
    get_chain: Callable[[], ChainRead | None],
    lookup: Callable[[int], datetime | None],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    snapshot = _read(lambda: _trades(settings.endpoint, addr))
    if "error" in snapshot:
        return snapshot, snapshot.copy(), {"fetchedAt": None, "data": []}
    trades = snapshot["data"].rows
    chain = (
        get_chain() if any(trade["tradeType"] == "trade" for trade in trades) else None
    )
    prices = (
        _read(lambda: _prices(settings.endpoint))
        if trades
        else {"fetchedAt": None, "data": {}}
    )
    quotes = prices.get("data", {})

    @cache
    def get_market_price(
        market_id: int, collateral_id: int
    ) -> tuple[dict[str, Any], str]:
        assert chain is not None
        data = chain.query(
            {
                "get_perp_prices": {
                    "market_index": f"MarketIndex({market_id})",
                    "collateral_index": f"TokenIndex({collateral_id})",
                }
            }
        )
        return data["market"], datetime.now(UTC).isoformat()

    def read_risk(trade: dict[str, Any], conversion: Decimal | None) -> dict[str, Any]:
        return _contract_risk(trade, chain, addr, get_market_price, conversion)

    def rows(opened: bool) -> list[dict[str, Any]]:
        return [
            {
                **_trade_row(trade, quotes, read_risk),
                "fetchedAt": trade["fetchedAt"],
                **_block_source(trade["lastUpdatedBlock"], trade["fetchedAt"], lookup),
            }
            for trade in trades
            if (trade["tradeType"] == "trade") == opened
        ]

    positions, orders = _read(lambda: rows(True)), _read(lambda: rows(False))
    complete = snapshot["data"].complete
    for section in (positions, orders):
        if "data" in section:
            section["fetchedAt"] = snapshot["fetchedAt"]
            if not complete:
                _mark_incomplete(section)
    items = positions.get("data", [])
    if "data" in positions:
        pnls = [row["unrealizedPnlUsd"] for row in items]
        positions["data"] = {
            "items": items,
            "unrealizedPnlUsd": sum(pnls, 0.0)
            if complete and None not in pnls
            else None,
            "unrealizedPnlBasis": "grossBeforeFees",
        }
    if "data" in prices:
        used_ids = set()
        for row in [*items, *orders.get("data", [])]:
            if row["collateralToken"] != "USDC":
                used_ids.add(row["collateralTokenId"])
            if (
                row["quoteTokenId"] != USD_TOKEN_ID
                and row.get("riskStatus") != "verified"
            ):
                used_ids.add(row["quoteTokenId"])
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


def _queued_rows(endpoint: str, addr: str) -> Pages:
    snapshot = _pages(
        endpoint, keeper.PENDING_MARKET_ORDERS, "marketOrders", "sequence", addr
    )
    snapshot.rows = [
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
            "fetchedAt": row["fetchedAt"],
        }
        for row in snapshot.rows
    ]
    return snapshot


def _queued_orders(
    endpoint: str, addr: str, lookup: Callable[[int], datetime | None]
) -> dict[str, Any]:
    section = _read(lambda: _queued_rows(endpoint, addr))
    if "data" in section:
        snapshot = section["data"]
        section["data"] = snapshot.rows
        if not snapshot.complete:
            _mark_incomplete(section)
        for row in section["data"]:
            row.update(_block_source(row, row["fetchedAt"], lookup))
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
    def get_chain() -> ChainRead | None:
        return _chain_read(settings)

    @cache
    def lookup(height: int) -> datetime | None:
        chain = get_chain()
        return chain.block_time(height) if chain is not None else None

    balances = _read(lambda: _balances(settings, addr))
    if "data" in balances:
        rows = balances["data"]
        # An independent WebSocket override may be served by another chain.
        balance_lookup = (
            lookup
            if settings.ws_endpoint == stream.derive_ws(settings.endpoint)
            else None
        )
        if not rows:
            balances["error"] = _failure(
                "INCOMPLETE_DATA",
                "Keeper returned no Sai balance rows. Funds are unknown.",
            )
        balances["data"] = {
            key: {
                "usd": rows.get(key, {}).get("usd"),
                **_block_source(
                    rows.get(key, {}), balances["fetchedAt"], balance_lookup
                ),
            }
            for key in BALANCE_KEYS.values()
        }
    positions, orders, prices = _trade_sections(settings, addr, get_chain, lookup)
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
