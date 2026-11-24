"""Size partial closes from contract state, not a lagging index."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner
from sai_cli.core import keeper, margin, storage
from sai_cli.sai_cli import main

ADDR = "0x" + "a" * 40


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))


def session_for(endpoint: str) -> dict[str, str]:
    return {
        "token": "fake-token",
        "evmAddress": ADDR,
        "endpoint": endpoint,
        "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }


def partial_close(monkeypatch, indexed: int, contract: int, *args: str):
    submitted = []

    def gql(endpoint, query, variables=None, token=None, timeout=20):
        if query == keeper.ROUTE:
            return {
                "oneClickTrading": {
                    "enabled": True,
                    "chainId": "cataclysm-1",
                    "contractAddress": "nibi1contract",
                }
            }
        if query == keeper.POSITION:
            return {
                "perp": {
                    "trade": {
                        "id": 7,
                        "trader": "nibi1trader",
                        "isOpen": True,
                        "tradeType": "trade",
                        "collateralAmount": indexed,
                        "leverage": 2.0,
                        "perpBorrowing": {
                            "marketId": 0,
                            "collateralToken": {"id": 1, "symbol": "USDC"},
                        },
                    }
                }
            }
        if query == keeper.MARKET:
            return {
                "perp": {
                    "borrowing": {
                        "marketId": 0,
                        "isOpen": True,
                        "minLeverage": 1,
                        "maxLeverage": 100,
                        "collateralToken": {"id": 1},
                    }
                }
            }
        if query == keeper.MARKETS:
            return {
                "perp": {
                    "borrowings": [
                        {
                            "marketId": 0,
                            "isOpen": True,
                            "minPositionSizeUSD": 1,
                            "collateralToken": {"symbol": "USDC"},
                        }
                    ]
                }
            }
        submitted.append(query)
        raise AssertionError("unexpected Keeper request")

    raw = {
        "user": "nibi1trader",
        "user_trade_index": "UserTradeIndex(7)",
        "market_index": "MarketIndex(0)",
        "collateral_index": "TokenIndex(1)",
        "is_open": True,
        "trade_type": "trade",
        "collateral_amount": str(contract),
        "leverage": "2",
    }
    funds = {"collateral_index": "TokenIndex(1)", "available_to_trade": "0"}
    monkeypatch.setattr(keeper, "request", gql)
    monkeypatch.setattr(
        margin,
        "_get_json",
        lambda url: {"default_node_info": {"network": "cataclysm-1"}},
    )
    monkeypatch.setattr(
        margin,
        "smart",
        lambda lcd, contract, message, **kwargs: (
            raw if "get_trade_raw" in message else funds
        ),
    )
    storage.save_session(session_for(keeper.MAINNET_HTTP))
    result = CliRunner().invoke(
        main,
        ["--json", "trade", "partial-close", "7", "--percent", "50", *args],
    )
    return result, submitted


def test_partial_close_refuses_index_lag(monkeypatch):
    # Margin removal made the contract $1.50 while Keeper still shows $2.
    result, submitted = partial_close(monkeypatch, 2_000_000, 1_500_000)
    assert result.exit_code != 0
    assert "Retry after Keeper synchronizes" in result.output
    assert submitted == []


def test_partial_close_sizes_from_contract_state(monkeypatch):
    result, _ = partial_close(monkeypatch, 3_000_000, 3_000_000, "--dry-run")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["intent"]["body"]["collateral"]["amountUsdc"] == "1500000"
