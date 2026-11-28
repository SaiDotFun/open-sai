"""Render the CLI account overview."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..core.overview import is_usdc_balance
from ..core.session import expiry_notice_text
from .repl_skin import ReplSkin

UNKNOWN = "unknown"


def _value(value: Any, suffix: str = "") -> str:
    return UNKNOWN if value is None else f"{value:,.8g}{suffix}"


def _source(row: dict[str, Any]) -> str:
    if row.get("sourceAt") is not None and row.get("sourceAtBasis") != "fetchedAt":
        return f"{row['sourceAt']} ({_value(row['sourceAgeSeconds'], ' s')})"
    if row.get("sourceBlock") is not None:
        return f"block {row['sourceBlock']} (time unknown)"
    return UNKNOWN


def _balance_source(row: dict[str, Any]) -> str:
    height = row["sourceBlock"]
    source = f"block {height}" if height is not None else "block unknown"
    age = row["sourceAgeSeconds"]
    if age is None:
        return f"{source} (age unknown)"
    minutes, seconds = divmod(int(age), 60)
    hours, minutes = divmod(minutes, 60)
    elapsed = f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s"
    return f"{source} ({elapsed} old{' · stale' if row['stale'] else ''})"


def _token_quantity(row: dict[str, Any]) -> str:
    token = row["token_info"]
    amount = str(row.get("amount"))
    decimals = token.get("decimals")
    symbol = token.get("symbol")
    if not (
        amount.isascii()
        and amount.isdecimal()
        and type(decimals) is int
        and 0 <= decimals <= 255
        and isinstance(symbol, str)
        and symbol
    ):
        return UNKNOWN
    quantity = format(Decimal(f"{amount}e-{decimals}"), "f")
    if "." in quantity:
        quantity = quantity.rstrip("0").rstrip(".")
    return f"{quantity} {symbol}"


def show_balances(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No balance rows returned.")
        return
    ReplSkin("sai").table(
        ["Balance", "Quantity", "USD", "Source (Age)"],
        [
            [
                row["token_info"]["name"],
                "-" if is_usdc_balance(row) else _token_quantity(row),
                _value(row.get("usd_value")),
                _balance_source(row),
            ]
            for row in rows
        ],
        max_col_width=72,
    )


def _collateral(row: dict[str, Any]) -> str:
    if row["collateralUsd"] is not None:
        return _value(row["collateralUsd"])
    token = row["collateralToken"] or row["collateralTokenId"]
    return f"{row['collateralBaseUnits']} {token} base units"


def _trigger(row: dict[str, Any], prefix: str) -> str:
    price = row[f"{prefix}PriceUsd"]
    if prefix == "liquidation" and (warning := row.get("liquidationWarning")):
        return f"{_value(price)} {warning}"
    distance = row[f"{prefix}DistancePercent"]
    if price is None:
        return "-"
    return f"{_value(price)} ({_value(distance, '%')})"


def show(result: dict[str, Any]) -> None:
    skin = ReplSkin("sai")
    print(f"Sai account {result['addr']} ({result['addrSource']})")

    def section(key: str, title: str) -> Any:
        snapshot = result[key]
        skin.section(title)
        if snapshot["fetchedAt"]:
            print(f"  Fetched at {snapshot['fetchedAt']}")
        if source := snapshot.get("source"):
            print(f"  Source {source['endpoint']}")
        if warning := snapshot.get("warning"):
            print(f"  {warning}")
        if membership := snapshot.get("membership"):
            if membership["sourceBlock"] is None:
                print("  Live position membership unavailable")
            else:
                print(
                    f"  Live position membership {membership['status']}. "
                    f"Block {membership['sourceBlock']}, {_source(membership)}"
                )
        if error := snapshot.get("error"):
            print(f"  {error['message']} ({error['code']})")
        if snapshot.get("status") == "skipped":
            print(f"  Skipped. {snapshot['skipReason']}")
            return None
        return snapshot.get("data")

    def table(headers: list[str], rows: list[list[str]]) -> None:
        if rows:
            skin.table(headers, rows, max_col_width=72)
        else:
            print("  None")

    balances = section("balances", "Sai Funds")
    if balances is not None:
        table(
            ["Funds", "USD", "Source (Age)"],
            [
                [
                    "Unsettled Teller" if key == "unsettled" else key.title(),
                    _value(row["usd"]),
                    _balance_source(row),
                ]
                for key, row in balances.items()
            ],
        )
    positions = section("positions", "Open Positions")
    if positions is not None:
        table(
            [
                "ID",
                "Market",
                "Side",
                "Size USD",
                "Collateral USD",
                "Leverage",
                "Gross PnL USD",
                "Gross PnL %",
            ],
            [
                [
                    str(row["id"]),
                    str(row["market"] or row["marketId"]),
                    row["side"],
                    _value(row["sizeUsd"]),
                    _collateral(row),
                    _value(row["leverage"], "x"),
                    _value(row["unrealizedPnlUsd"]),
                    _value(row["unrealizedPnlPercent"], "%"),
                ]
                for row in positions["items"]
            ],
        )
        if positions["items"]:
            skin.table(
                [
                    "ID",
                    "Entry USD",
                    "Mark USD",
                    "Liquidation USD (%)",
                    "TP USD (%)",
                    "SL USD (%)",
                    "Trade Source (Age)",
                ],
                [
                    [
                        str(row["id"]),
                        _value(row["entryPriceUsd"]),
                        _value(row["markPriceUsd"]),
                        _trigger(row, "liquidation"),
                        _trigger(row, "takeProfit"),
                        _trigger(row, "stopLoss"),
                        _source(row),
                    ]
                    for row in positions["items"]
                ],
                max_col_width=72,
            )
            for row in positions["items"]:
                if risk := row.get("riskSource"):
                    print(
                        f"  Trade {row['id']} contract risk {_source(risk)}. Fetched at {risk['fetchedAt']}"
                    )
                    if row["liquidationPriceUsd"] is None:
                        price = _value(row["liquidationPriceQuote"])
                        print(
                            f"    Contract liquidation price {price} {row['quoteToken'] or row['quoteTokenId']}"
                        )
                if mark := row.get("markSource"):
                    block = (
                        mark["sourceBlock"]
                        if mark["sourceBlock"] is not None
                        else UNKNOWN
                    )
                    print(
                        f"  Trade {row['id']} oracle mark {_source(mark)}. Publish block {block}. "
                        f"Read at block {mark['queryBlock']}, fetched at {mark['fetchedAt']}"
                    )
                    if row["quoteTokenId"] != 0:
                        print(
                            f"    Base price {_source(mark['base'])}. Quote price {_source(mark['quote'])}"
                        )
                if collateral := row.get("collateralPriceSource"):
                    print(
                        f"  Trade {row['id']} pinned collateral price {_source(collateral)}"
                    )
                if row["riskStatus"] != "verified":
                    print(
                        f"  Trade {row['id']} risk {row['riskStatus']}. {row['riskReason']}"
                    )
                    if "keeperLiquidationEstimatePriceQuote" in row:
                        estimate = _value(row["keeperLiquidationEstimatePriceQuote"])
                        print(
                            f"    Keeper liquidation estimate {estimate} {row['quoteToken'] or row['quoteTokenId']}. Source time unknown."
                        )
            print(
                f"  Total gross unrealized PnL before fees {_value(positions['unrealizedPnlUsd'], ' USD')}"
            )

    orders = section("pendingOrders", "Pending Limit and Stop Orders")
    if orders is not None:
        table(
            [
                "ID",
                "Market",
                "Side",
                "Type",
                "Trigger USD",
                "Size USD",
                "Collateral USD",
                "Leverage",
                "TP USD",
                "SL USD",
                "Source (Age)",
            ],
            [
                [
                    str(row["id"]),
                    str(row["market"] or row["marketId"]),
                    row["side"],
                    row["orderType"],
                    _value(row["triggerPriceUsd"]),
                    _value(row["sizeUsd"]),
                    _collateral(row),
                    _value(row["leverage"], "x"),
                    _value(row["takeProfitPriceUsd"]),
                    _value(row["stopLossPriceUsd"]),
                    _source(row),
                ]
                for row in orders
            ],
        )

    queued = section("queuedMarketOrders", "Queued Market Orders")
    if queued is not None:
        table(
            [
                "Sequence",
                "Trade ID",
                "Market ID",
                "Action",
                "Status",
                "Collateral Base Units",
                "Source (Age)",
            ],
            [
                [
                    str(row["sequence"]),
                    str(row["tradeId"]),
                    str(row["marketId"]),
                    row["action"],
                    row["status"],
                    _value(row["collateralBaseUnits"]),
                    _source(row),
                ]
                for row in queued
            ],
        )
        for row in queued:
            for key, network in (
                ("requestTxHash", "Cosmos"),
                ("requestEvmTxHash", "EVM"),
            ):
                if tx_hash := row[key]:
                    print(f"  Order {row['sequence']} {network} request tx {tx_hash}")

    prices = section("prices", "Oracle Prices")
    if prices is not None:
        table(
            ["Token", "USD", "Source (Age)"],
            [
                [str(row["token"] or row["tokenId"]), _value(row["usd"]), _source(row)]
                for row in prices
            ],
        )

    intents = section("blockingIntents", "Local Intents Blocking Trades")
    if intents is not None:
        table(
            ["Intent Key", "Kind", "Status", "Scope", "Trade ID", "Reserved (Age)"],
            [
                [
                    row["intentKey"],
                    row["kind"],
                    row["status"],
                    row["scope"],
                    str(row["tradeId"] or "-"),
                    _source(row),
                ]
                for row in intents
            ],
        )

    session = section("session", "Trading Session")
    if session is not None:
        if session["connected"]:
            state = (
                "belongs to another endpoint"
                if not session["endpointMatches"]
                else "expired"
                if session["expired"]
                else "connected"
            )
            print(f"  {state.capitalize()} until {session['expiresAt']}")
            print(f"  Issued at {_source(session)}")
            if session.get("expiryWarning"):
                print(f"  {expiry_notice_text(session)}")
        else:
            print("  No local session for this account")
