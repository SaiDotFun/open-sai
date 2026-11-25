"""Read one account's trading state without reconciling intents or signing in."""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from functools import cache
from threading import BoundedSemaphore
from typing import Any

from . import keeper, margin, reconcile, session, storage, stream
from .errors import Code, SaiError
from .log import origin
from .settings import Settings

PAGE_SIZE = 100
MAX_PAGES = 20
LCD_BUDGET_SECONDS = 8.0
LCD_REQUEST_SECONDS = 2.0
MEMBERSHIP_MAX_AGE_SECONDS = 60
BALANCE_STALE_SECONDS = 300
_LCD_SLOTS = BoundedSemaphore(8)
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


def _failure(code: Code, message: str) -> dict[str, Any]:
    return SaiError(message, code).as_dict()


def _read(action: Callable[[], Any]) -> dict[str, Any]:
    try:
        data = action()
    except SaiError as exc:
        return {"fetchedAt": None, "error": exc.as_dict()}
    except (OSError, sqlite3.Error):
        return {
            "fetchedAt": None,
            "error": _failure(Code.INTERNAL_ERROR, "Could not read local Sai data"),
        }
    except (KeyError, TypeError, ValueError, OverflowError, DecimalException):
        return {
            "fetchedAt": None,
            "error": _failure(Code.INCOMPLETE_DATA, "The source returned invalid data"),
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
            max(0.0, round((datetime.fromisoformat(fetched) - at).total_seconds(), 3))
            if at is not None
            else None
        ),
    }


def _lcd_read(read: Callable[[float], Any], deadline: float) -> Any:
    """Hold a shared slot until the HTTP request finishes or is cancelled."""
    now = time.monotonic()
    if now >= deadline:
        raise margin.LCDTimeout("LCD read deadline exceeded")
    if not _LCD_SLOTS.acquire(blocking=False):
        raise ValueError("LCD reads are busy")
    try:
        return read(min(deadline, now + LCD_REQUEST_SECONDS))
    finally:
        _LCD_SLOTS.release()


@dataclass(frozen=True)
class ChainRead:
    lcd: str
    chain_id: str
    contract: str
    height: int
    time: datetime
    deadline: float

    def query(self, message: dict[str, Any], *, contract: str | None = None) -> Any:
        return _lcd_read(
            lambda deadline: margin.smart_data(
                self.lcd,
                contract or self.contract,
                message,
                height=self.height,
                deadline=deadline,
            ),
            self.deadline,
        )

    def block_time(self, height: int) -> datetime | None:
        if height == self.height:
            return self.time
        try:
            header = _lcd_read(
                lambda deadline: margin.block_header(
                    self.lcd, height, deadline=deadline
                ),
                self.deadline,
            )
            if header["chain_id"] == self.chain_id and int(header["height"]) == height:
                return _instant(header["time"])
        except (KeyError, TypeError, ValueError):
            pass
        return None


def _is_contract(address: Any) -> bool:
    return (
        isinstance(address, str) and address.startswith("nibi1") and address.isalnum()
    )


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
        deadline = time.monotonic() + LCD_BUDGET_SECONDS
        header = _lcd_read(
            lambda deadline: margin.block_header(
                settings.lcd_endpoint, "latest", deadline=deadline
            ),
            deadline,
        )
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
            and _is_contract(contract)
        ):
            return ChainRead(
                settings.lcd_endpoint, chain_id, contract, height, at, deadline
            )
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


def balance_stale(age_seconds: float | None) -> bool | None:
    return age_seconds > BALANCE_STALE_SECONDS if age_seconds is not None else None


def balance_snapshot(
    settings: Settings, ws: str, addr: str, *, notify: Callable[[str], None]
) -> dict[str, Any]:
    response = stream.snapshot(
        ws, stream.BALANCES_WITH_BLOCK, {"user": addr}, notify=notify
    )
    fetched = datetime.now(UTC).isoformat()

    @cache
    def chain() -> ChainRead | None:
        return _chain_read(settings)

    @cache
    def lookup(height: int) -> datetime | None:
        source = chain()
        return source.block_time(height) if source is not None else None

    # A separately configured WebSocket may serve another chain.
    balance_lookup = lookup if ws == stream.derive_ws(settings.endpoint) else None
    for row in response["data"]["userBalances"]:
        stamp = _block_source(
            {"block": row.get("lastUpdatedBlock")}, fetched, balance_lookup
        )
        row.update(stamp, stale=balance_stale(stamp["sourceAgeSeconds"]))
    return response


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
                "block": row.get("lastUpdatedBlock"),
            }
    return balances


@dataclass
class Pages:
    rows: list[dict[str, Any]]
    complete: bool
    paginated: bool
    read_failed: bool = False


def _scan_pages(
    endpoint: str, query: str, field: str, id_field: str, addr: str
) -> Pages:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for page_num in range(MAX_PAGES):
        try:
            response = keeper.request(
                endpoint,
                query,
                {"trader": addr, "limit": PAGE_SIZE, "offset": len(rows)},
            )
            page = response["perp"][field]
            if not isinstance(page, list):
                raise TypeError("Expected a list")
            entries = [(row[id_field], row) for row in page]
        except (keeper.KeeperError, KeyError, TypeError):
            if not rows:
                raise
            return Pages(rows, False, True, read_failed=True)
        fetched = datetime.now(UTC).isoformat()
        for item_id, row in entries:
            if item_id in seen:
                return Pages(rows, False, True)
            seen.add(item_id)
            rows.append({**row, "fetchedAt": fetched})
        if len(page) < PAGE_SIZE:
            return Pages(rows, True, page_num > 0)
    return Pages(rows, False, True)


def _pages(endpoint: str, query: str, field: str, id_field: str, addr: str) -> Pages:
    snapshot = _scan_pages(endpoint, query, field, id_field, addr)
    if not snapshot.paginated or snapshot.read_failed:
        return snapshot
    # The live Keeper query has offsets and no cursor. Confirm a paginated
    # list with a second scan so a close between pages cannot silently skip a row.
    best = snapshot
    for _ in range(2):
        try:
            current = _scan_pages(endpoint, query, field, id_field, addr)
        except (keeper.KeeperError, KeyError, TypeError):
            break
        if current.complete or (
            not best.complete and len(current.rows) > len(best.rows)
        ):
            best = current
        if current.read_failed:
            break
        if (
            snapshot.complete
            and current.complete
            and [row[id_field] for row in snapshot.rows]
            == [row[id_field] for row in current.rows]
        ):
            return current
        snapshot = current
    return Pages(best.rows, False, True)


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


def _oracle_source(
    stamp: dict[str, Any], published: datetime | None, chain: ChainRead, fetched: str
) -> dict[str, Any]:
    written_ms = stamp.get("price_block_time_ms")
    written = (
        _oracle_time(written_ms / 1000, chain) if isinstance(written_ms, int) else None
    )
    height = stamp.get("price_block")
    return {
        "sourceBlock": height
        if isinstance(height, int) and 0 < height <= chain.height
        else None,
        **_age(written or published, fetched),
    }


class ContractReads:
    """Cache verified calls for the lifetime of one pinned overview."""

    def __init__(self, chain: ChainRead) -> None:
        self.chain = chain
        self.timed_out = False
        self.groups: dict[str, int | None] = {}
        self.rates: dict[tuple[int, int], tuple[dict[str, Any], str] | None] = {}
        self.markets: dict[int, dict[str, Any] | None] = {}
        self.oracle: str | None = None

    def query(self, message: dict[str, Any], *, contract: str | None = None) -> Any:
        try:
            return self.chain.query(message, contract=contract)
        except margin.LCDTimeout:
            self.timed_out = True
            raise

    def group_expiration(self, index: str) -> int | None:
        if index not in self.groups:
            try:
                value = self.query({"get_group": {"index": index}})
                expiration = value["stale_price_expiration_secs"]
                self.groups[index] = (
                    expiration
                    if isinstance(expiration, int) and expiration >= 0
                    else None
                )
            except (KeyError, TypeError, ValueError):
                self.groups[index] = None
        return self.groups[index]

    def market(self, market_id: int) -> dict[str, Any] | None:
        if market_id not in self.markets:
            try:
                self.markets[market_id] = self.query(
                    {"get_market": {"index": f"MarketIndex({market_id})"}}
                )
            except (TypeError, ValueError):
                self.markets[market_id] = None
        return self.markets[market_id]

    def rate(self, base: int, quote: int) -> tuple[dict[str, Any], str] | None:
        key = base, quote
        if key not in self.rates:
            try:
                if self.oracle is None:
                    address = self.query({"get_oracle_address": {}})
                    self.oracle = address if _is_contract(address) else ""
                if not self.oracle:
                    raise ValueError("Oracle contract is unavailable")
                data = self.query(
                    {"get_exchange_rate_stamped": {"base": base, "quote": quote}},
                    contract=self.oracle,
                )
                self.rates[key] = data, datetime.now(UTC).isoformat()
            except (TypeError, ValueError):
                self.oracle = self.oracle or ""
                self.rates[key] = None
        return self.rates[key]


def _contract_risk(
    trade: dict[str, Any], reads: ContractReads | None, addr: str
) -> dict[str, Any]:
    if reads is None:
        return {"riskStatus": "unavailable", "riskReason": "No matching LCD block"}
    chain = reads.chain
    market = trade["perpBorrowing"]
    risk: dict[str, Any] = {"riskStatus": "unavailable"}
    try:
        result = reads.query({"get_trade": {"trader": addr, "index": trade["id"]}})
        fetched = datetime.now(UTC).isoformat()
        raw = result["trade"]
        if not _agrees(trade, raw):
            return {
                "riskStatus": "inconsistent",
                "riskReason": "Keeper and contract trade values differ",
            }
        liquidation = _decimal(result["liquidation_price"])
        entry = _decimal(raw["open_price"])
        base_id, quote_id = market["baseToken"]["id"], market["quoteToken"]["id"]
        adjustment = bool(result.get("needs_after_hours_trigger"))
        if liquidation < 0 or entry <= 0:
            raise ValueError("Invalid contract trade price")
        risk = {
            "riskStatus": "partial",
            "afterHoursLeverageAdjustmentPending": adjustment,
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
        contract_market = reads.market(market["marketId"])
        if contract_market is None:
            raise ValueError("Contract market is unavailable")
        if (
            contract_market["base"] != f"TokenIndex({base_id})"
            or contract_market["quote"] != f"TokenIndex({quote_id})"
        ):
            return {
                "riskStatus": "inconsistent",
                "riskReason": "Keeper and contract disagree on the market pair",
            }
        stamped = reads.rate(base_id, quote_id)
        if stamped is None:
            raise ValueError("Contract oracle is unavailable")
        oracle, price_fetched = stamped
        rate = oracle["rate"]
        mark, quote_usd = (
            _decimal(rate["base_per_quote"]),
            _decimal(rate["quote_price_usd"]),
        )
        if min(mark, quote_usd) <= 0 or (quote_id == USD_TOKEN_ID and quote_usd != 1):
            raise ValueError("Invalid contract oracle price")
        base_at = _oracle_time(rate.get("base_last_update_time"), chain)
        quote_at = (
            _oracle_time(rate.get("quote_last_update_time"), chain)
            if quote_id != USD_TOKEN_ID
            else None
        )
        base_source = _oracle_source(oracle["base"], base_at, chain, price_fetched)
        quote_source = (
            _oracle_source(oracle["quote"], quote_at, chain, price_fetched)
            if quote_id != USD_TOKEN_ID
            else None
        )
        if quote_source is not None and (
            base_source["sourceAt"] is None or quote_source["sourceAt"] is None
        ):
            mark_source = {"sourceBlock": None, **_age(None, price_fetched)}
        else:
            mark_source = (
                min((base_source, quote_source), key=lambda leg: leg["sourceAt"])
                if quote_source is not None
                else base_source
            )
        risk.update(
            markSource={
                **mark_source,
                "queryBlock": chain.height,
                "fetchedAt": price_fetched,
                "base": base_source,
                "quote": quote_source,
            },
            markPriceUsd=_usd(mark, quote_usd),
            liquidationPriceUsd=_usd(liquidation, quote_usd),
        )
        expiration = reads.group_expiration(contract_market["group_index"])
        if expiration is None:
            return {**risk, "riskReason": "Contract oracle staleness limit is unknown"}
        if base_at is None or (quote_id != USD_TOKEN_ID and quote_at is None):
            return {**risk, "riskReason": "Contract oracle publish time is unknown"}
        oldest = min(base_at, quote_at) if quote_at is not None else base_at
        is_long = raw["long"]
        if (
            expiration
            and int(chain.time.timestamp()) - int(oldest.timestamp()) > expiration
        ):
            # Catalyst liquidation can use a crossed mark even when it is stale.
            crossed = mark <= liquidation if is_long else mark >= liquidation
            if crossed:
                risk["liquidationWarning"] = "at or past liquidation (stale mark)"
            return {**risk, "riskReason": "Contract oracle mark is stale"}
        tp, sl = _level(raw["tp"]), _level(raw["sl"])
        risk.update(
            entryPriceUsd=_usd(entry, quote_usd),
            takeProfitPriceUsd=_usd(tp, quote_usd),
            stopLossPriceUsd=_usd(sl, quote_usd),
            liquidationDistancePercent=_distance(mark, liquidation, not is_long),
            takeProfitDistancePercent=_distance(mark, tp, is_long),
            stopLossDistancePercent=_distance(mark, sl, not is_long),
        )
        if adjustment:
            return {
                **risk,
                "takeProfitDistancePercent": None,
                "stopLossDistancePercent": None,
                "riskReason": "Gross PnL and TP/SL distance are unavailable while the after-hours leverage adjustment is pending",
            }

        collateral = market["collateralToken"]
        scale = COLLATERAL_SCALE.get(collateral["symbol"])
        if collateral["symbol"] == "USDC":
            conversion = Decimal(1) / COLLATERAL_SCALE["USDC"]
        else:
            pinned = reads.rate(collateral["id"], USD_TOKEN_ID) if scale else None
            if pinned is None or scale is None:
                return {
                    **risk,
                    "riskReason": "Pinned collateral USD price is unavailable",
                }
            collateral_oracle, collateral_fetched = pinned
            collateral_rate = collateral_oracle["rate"]
            price = _decimal(collateral_rate["base_per_quote"])
            if price <= 0:
                return {
                    **risk,
                    "riskReason": "Pinned collateral USD price is unavailable",
                }
            conversion = price / scale
            risk["collateralPriceSource"] = {
                "fetchedAt": collateral_fetched,
                "queryBlock": chain.height,
                **_oracle_source(
                    collateral_oracle["base"],
                    _oracle_time(collateral_rate.get("base_last_update_time"), chain),
                    chain,
                    collateral_fetched,
                ),
            }
        leverage = _decimal(raw["leverage"])
        amount = Decimal(raw["collateral_amount"])
        pnl = ((mark - entry) if is_long else (entry - mark)) / entry * leverage
        pnl = max(Decimal(-1), min(MAX_GROSS_PNL, pnl))
        return {
            **risk,
            "riskStatus": "verified",
            "collateralUsd": _usd(amount, conversion),
            "sizeUsd": _usd(amount * leverage, conversion),
            "unrealizedPnlUsd": _usd(pnl * amount, conversion),
            "unrealizedPnlPercent": float(pnl * 100),
        }
    except (KeyError, TypeError, ValueError, OSError, OverflowError, DecimalException):
        return {
            **risk,
            "riskReason": "Could not read matching contract risk data at one block",
        }


def _trade_row(
    trade: dict[str, Any],
    prices: Mapping[int, Any],
    read_risk: Callable[[dict[str, Any]], dict[str, Any]],
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
    risk = read_risk(trade)
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
        Code.INCOMPLETE_DATA,
        "The paginated list could not be confirmed and may be incomplete",
    )


def _live_trade_ids(chain: ChainRead | None, addr: str) -> set[int] | None:
    if chain is None:
        return None
    try:
        # This index can omit older live trades until the contract finishes seeding it.
        if chain.query({"get_base_size_seed_status": {}}) != "complete":
            return None
        ids: set[int] = set()
        last: int | None = None
        for _ in range(MAX_PAGES):
            page = chain.query(
                {
                    "live_trades_by_user": {
                        "trader": addr,
                        "start_after": last,
                        "limit": PAGE_SIZE,
                    }
                }
            )
            if not isinstance(page, list):
                return None
            for row in page:
                index = row["trade_index"]
                if (
                    not isinstance(index, int)
                    or index < 0
                    or (last is not None and index <= last)
                ):
                    return None
                ids.add(index)
                last = index
            if len(page) < PAGE_SIZE:
                return ids
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _fill_awaits_indexing(endpoint: str, addr: str) -> bool:
    """Read Keeper's provisional outcome without reconciling local intent state."""
    try:
        rows = keeper.request(endpoint, keeper.INTENTS, {"user": addr}, timeout=3)[
            "userIntents"
        ]
        return any(
            row.get("errorCode") == reconcile.OUTCOME_NOT_INDEXED
            and reconcile.classify_outcome(row) == "pending"
            for row in rows
        )
    except (keeper.KeeperError, KeyError, TypeError, AttributeError):
        return False


def _membership_check(
    chain: ChainRead | None, addr: str, indexed_ids: set[int]
) -> dict[str, Any]:
    live_ids = _live_trade_ids(chain, addr)
    if chain is None or live_ids is None:
        return {
            "status": "unavailable",
            "fetchedAt": None,
            "sourceBlock": None,
            "sourceAt": None,
            "sourceAgeSeconds": None,
        }
    fetched = datetime.now(UTC)
    age = (fetched - chain.time).total_seconds()
    if not 0 <= age <= MEMBERSHIP_MAX_AGE_SECONDS:
        status = "stale"
    else:
        status = "verified" if live_ids == indexed_ids else "mismatch"
    return {
        "status": status,
        "fetchedAt": fetched.isoformat(),
        "sourceBlock": chain.height,
        **_age(chain.time, fetched.isoformat()),
    }


def _total_pnl(values: list[float | None]) -> float | None:
    """Use Neumaier summation on every supported Python version."""
    total = correction = 0.0
    for value in values:
        if value is None:
            return None
        next_total = total + value
        if abs(total) >= abs(value):
            correction += (total - next_total) + value
        else:
            correction += (value - next_total) + total
        total = next_total
    return total + correction if correction and math.isfinite(correction) else total


def _trade_sections(
    settings: Settings,
    addr: str,
    get_chain: Callable[[], ChainRead | None],
    lookup: Callable[[int], datetime | None],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    snapshot = _read(lambda: _trades(settings.endpoint, addr))
    if "error" in snapshot:
        return (
            snapshot,
            snapshot.copy(),
            _skipped_prices("Positions and pending orders could not be loaded."),
        )
    trades = snapshot["data"].rows
    unindexed_fill = _fill_awaits_indexing(settings.endpoint, addr)
    prices = (
        _read(lambda: _prices(settings.endpoint))
        if trades
        else _skipped_prices("No positions or pending orders were available to price.")
    )
    quotes = prices.get("data") or {}
    chain = get_chain()
    indexed_ids = {trade["id"] for trade in trades if trade["tradeType"] == "trade"}
    membership = _membership_check(chain, addr, indexed_ids)
    contract_reads = ContractReads(chain) if chain is not None else None

    def read_risk(trade: dict[str, Any]) -> dict[str, Any]:
        return _contract_risk(trade, contract_reads, addr)

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
    positions["membership"] = membership
    complete = snapshot["data"].complete
    for section in (positions, orders):
        if "data" in section:
            section["fetchedAt"] = snapshot["fetchedAt"]
            if not complete:
                _mark_incomplete(section)
    items = positions.get("data", [])
    if "data" in positions:
        membership_complete = membership["status"] == "verified" and not unindexed_fill
        if complete and not membership_complete:
            if unindexed_fill:
                message = "Keeper reports a fill awaiting indexing. Positions may be incomplete."
            elif membership["status"] == "stale":
                message = "The live position check used a stale LCD block. Positions may be incomplete."
            else:
                message = "The indexed positions could not be confirmed against the live contract list."
            positions["error"] = _failure(Code.INCOMPLETE_DATA, message)
        elif (
            "error" not in positions
            and contract_reads is not None
            and (
                contract_reads.timed_out
                or time.monotonic() >= contract_reads.chain.deadline
            )
            and any(row["riskStatus"] != "verified" for row in items)
        ):
            positions["error"] = _failure(
                Code.INCOMPLETE_DATA,
                "An LCD deadline expired. Position risk is partial.",
            )
        pnls = [row["unrealizedPnlUsd"] for row in items]
        positions["data"] = {
            "items": items,
            "unrealizedPnlUsd": _total_pnl(pnls)
            if complete and membership_complete
            else None,
            "unrealizedPnlBasis": "grossBeforeFees",
        }
    if prices["fetchedAt"] is not None:
        used_ids = set()
        for row in [*items, *orders.get("data", [])]:
            if row["collateralToken"] != "USDC" and "collateralPriceSource" not in row:
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
                Code.INCOMPLETE_DATA,
                "Keeper has no positive USD price for one or more tokens",
            )
    return positions, orders, prices


def _skipped_prices(reason: str) -> dict[str, Any]:
    return {"fetchedAt": None, "data": [], "status": "skipped", "skipReason": reason}


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
    blocking = []
    for row in storage.unresolved_intents(endpoint, addr):
        if row["status"] in ("unknown", "pending"):
            scope = "allTrades"
        elif row["entity"]:
            scope = "sameTrade"
        elif row["kind"] == "OPEN_TRADE":
            scope = "openTrades"
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
            "error": _failure(Code.INCOMPLETE_DATA, str(exc)),
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
    same_keeper = settings.ws_endpoint == stream.derive_ws(settings.endpoint)
    balances["source"] = {
        "endpoint": origin(settings.ws_endpoint),
        "matchesKeeper": same_keeper,
    }
    if not same_keeper:
        balances["warning"] = (
            "Balance WebSocket is independent. These funds may be on a different chain."
        )
    positions, orders, prices = _trade_sections(settings, addr, get_chain, lookup)
    if "data" in balances:
        rows = balances["data"]
        fetched = balances["fetchedAt"]
        # An independent WebSocket override may be served by another chain.
        balance_lookup = lookup if same_keeper else None
        if not rows:
            balances["error"] = _failure(
                Code.INCOMPLETE_DATA,
                "Keeper returned no Sai balance rows. Funds are unknown.",
            )
        balances["data"] = {}
        for key in BALANCE_KEYS.values():
            row = rows.get(key, {})
            stamp = _block_source(row, fetched, balance_lookup)
            if row and stamp["sourceAt"] is None:
                stamp.update(sourceAt=fetched, sourceAtBasis="fetchedAt")
            balances["data"][key] = {"usd": row.get("usd"), **stamp}
    queued = _queued_orders(settings.endpoint, addr, lookup)
    intents = _read(lambda: _blocking_intents(settings.endpoint, addr))
    for row in intents.get("data", []):
        row.update(_age(_unix_instant(row.pop("createdAt")), intents["fetchedAt"]))
    sections = {
        "balances": balances,
        "positions": positions,
        "pendingOrders": orders,
        "queuedMarketOrders": queued,
        "prices": prices,
        "blockingIntents": intents,
        "session": session_section,
    }
    for section in sections.values():
        if section["fetchedAt"] is not None:
            section["sourceAt"] = section["fetchedAt"]
    return {"addr": addr, "addrSource": source, **sections}
