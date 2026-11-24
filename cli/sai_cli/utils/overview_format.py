"""Render the CLI account overview."""

from __future__ import annotations

from typing import Any

from .repl_skin import ReplSkin

UNKNOWN = "unknown"


def _value(value: Any, suffix: str = "") -> str:
    return UNKNOWN if value is None else f"{value:,.8g}{suffix}"


def _source(row: dict[str, Any]) -> str:
    if row.get("sourceAt") is not None:
        return f"{row['sourceAt']} ({_value(row['sourceAgeSeconds'], ' s')})"
    if row.get("sourceBlock") is not None:
        return f"block {row['sourceBlock']} (time unknown)"
    return UNKNOWN


def _collateral(row: dict[str, Any]) -> str:
    if row["collateralUsd"] is not None:
        return _value(row["collateralUsd"])
    return f"{row['collateralBaseUnits']} base units"


def _trigger(row: dict[str, Any], prefix: str) -> str:
    price = row[f"{prefix}PriceUsd"]
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
        if error := snapshot.get("error"):
            print(f"  {error['message']} ({error['code']})")
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
                [key.title(), _value(row["usd"]), _source(row)]
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
                "PnL USD",
                "PnL %",
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
                    "Source (Age)",
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
            print(
                f"  Total unrealized PnL {_value(positions['unrealizedPnlUsd'], ' USD')}"
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
            [[row["token"], _value(row["usd"]), _source(row)] for row in prices],
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
            if warning := session.get("expiryWarning"):
                print(f"  {warning} Run {session['renewalCommand']}.")
        else:
            print("  No local session for this account")
