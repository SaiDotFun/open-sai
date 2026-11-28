"""Check the projections between Python main and the native USDC fields."""

from __future__ import annotations

import pytest
from python_main_usdc import (
    FIXED_USDC_PRICE,
    as_native_usdc,
    fixed_usdc_price,
    usd,
    without_partial_close_usd,
    without_receipt_usd,
)

ORACLE_PRICE = {"price": "1", "source": "Sai Perp collateral oracle", "observedAt": "x"}


def partial_preview() -> dict:
    return {
        "addr": "0x1",
        "dryRun": True,
        "quote": {"collateralBefore": "2997450", "collateralPriceUsd": "1"},
        "intent": {
            "kind": "DECREASE_POSITION_SIZE",
            "body": {"collateral": {"amountUsdc": "1498725"}},
        },
        "estimates": {
            "fees": {"status": "estimate", "collateralPriceUsd": dict(ORACLE_PRICE)}
        },
    }


@pytest.mark.parametrize(
    ("units", "dollars"),
    [(0, "0"), (1, "0.000001"), (3_000_000, "3"), (2_997_450, "2.99745")],
)
def test_usd_formats_base_units(units: int, dollars: str) -> None:
    assert usd(units) == dollars


def test_only_a_one_dollar_oracle_rate_maps_to_the_fixed_price() -> None:
    assert fixed_usdc_price(partial_preview())["estimates"]["fees"] == {
        "status": "estimate",
        "collateralPriceUsd": FIXED_USDC_PRICE,
    }
    off_peg = partial_preview()
    off_peg["estimates"]["fees"]["collateralPriceUsd"]["price"] = "1.0009"
    with pytest.raises(AssertionError):
        fixed_usdc_price(off_peg)


def test_partial_close_rows_round_trip() -> None:
    native = as_native_usdc(partial_preview())
    assert list(native)[:3] == ["addr", "dryRun", "collateral"]
    assert native["collateral"] == {"beforeUsd": "2.99745", "afterUsd": "1.498725"}
    assert without_partial_close_usd(native) == fixed_usdc_price(partial_preview())
    assert "collateral" not in as_native_usdc(partial_preview(), collateral=False)


def test_receipt_rows_follow_their_base_units() -> None:
    reply = {
        "receipt": {
            "fill": {
                "price": None,
                "sizeCollateralBaseUnits": None,
                "oraclePrice": "1",
                "escrowedCollateralBaseUnits": 3_000_000,
            },
            "positionNow": {
                "collateralAmount": "1498725",
                "openCollateralAmount": 2_997_450,
                "perpBorrowing": {"collateralToken": {"symbol": "USDC"}},
            },
        }
    }
    native = as_native_usdc(reply)
    assert list(native["receipt"]["fill"]) == [
        "price",
        "sizeCollateralBaseUnits",
        "sizeCollateralUsd",
        "oraclePrice",
        "escrowedCollateralBaseUnits",
        "escrowedCollateralUsd",
    ]
    assert native["receipt"]["fill"]["escrowedCollateralUsd"] == "3"
    assert native["receipt"]["positionNow"]["collateralUsd"] == "1.498725"
    assert native["receipt"]["positionNow"]["openCollateralUsd"] == "2.99745"
    assert without_receipt_usd(native) == reply

    reply["receipt"]["positionNow"]["perpBorrowing"]["collateralToken"]["symbol"] = (
        "stNIBI"
    )
    assert as_native_usdc(reply)["receipt"]["fill"]["escrowedCollateralUsd"] is None
