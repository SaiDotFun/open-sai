"""Map the USDC pricing differences between Python main and the native CLI.

Python is frozen. Python main still reads Perp's USDC collateral oracle
rate and its combined market and collateral price query. The native CLI
values one USDC at exactly $1 and reads the market rate from the configured
oracle (#245). It also shows collateral in dollars beside base units in
partial-close previews and trade receipts (#231). The README section "Fixed
USDC Price" lists these differences.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

FIXED_USDC_PRICE = {"price": "1", "source": "Fixed USDC price", "observedAt": None}
PYTHON_USDC_READS = frozenset({"get_collateral_price", "get_perp_prices"})
NATIVE_MARKET_READS = frozenset({"get_oracle_address", "get_exchange_rate"})
CLOSES = frozenset({"CLOSE_POSITION", "DECREASE_POSITION_SIZE"})
PYTHON_FEE_UNAVAILABLE = "Perp fee schedule or collateral oracle price is unavailable."
CLOSE_FEE_UNAVAILABLE = "Perp fee schedule is unavailable."


def fixed_usdc_price(python: dict[str, Any]) -> dict[str, Any]:
    """Replace a $1 oracle quote in a Python fee estimate with the fixed price."""
    result = deepcopy(python)
    fees = result.get("estimates", {}).get("fees", {})
    price = fees.get("collateralPriceUsd")
    if isinstance(price, dict):
        # Only a $1 oracle rate yields the same amounts as the fixed price.
        assert price["price"] == "1", price
        assert price["source"] == "Sai Perp collateral oracle", price
        fees["collateralPriceUsd"] = FIXED_USDC_PRICE
    closing = result.get("intent", {}).get("kind") in CLOSES
    if closing and fees.get("reason") == PYTHON_FEE_UNAVAILABLE:
        fees["reason"] = CLOSE_FEE_UNAVAILABLE
    return result


def without_partial_close_usd(native: dict[str, Any]) -> dict[str, Any]:
    """Check and drop the dollar collateral row Python lacks for partial closes."""
    result = deepcopy(native)
    if result.get("intent", {}).get("kind") != "DECREASE_POSITION_SIZE":
        return result
    collateral = result.pop("collateral", None)
    quote = result.get("quote")
    if quote is not None:
        before = int(quote["collateralBefore"])
        after = before - int(result["intent"]["body"]["collateral"]["amountUsdc"])
        assert collateral == {"beforeUsd": usd(before), "afterUsd": usd(after)}
    return result


def usd(units: int) -> str:
    """Format USDC base units as dollars."""
    whole, fraction = divmod(units, 1_000_000)
    return f"{whole}.{fraction:06d}".rstrip("0").rstrip(".")


def without_receipt_usd(native: dict[str, Any]) -> dict[str, Any]:
    """Drop the dollar fields the native receipt adds beside base units."""
    result = deepcopy(native)
    receipt = result.get("receipt") or {}
    for section, fields in (
        ("fill", ("sizeCollateralUsd", "escrowedCollateralUsd")),
        ("positionNow", ("collateralUsd", "openCollateralUsd")),
    ):
        for field in fields:
            (receipt.get(section) or {}).pop(field, None)
    return result
