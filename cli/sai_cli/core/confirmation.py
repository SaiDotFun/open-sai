"""Canonical trade preview digests for the CLI and MCP."""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from typing import Any

from .preview import rejection_reason


def canonical_price(value: object) -> str:
    """Write numerically equal prices as equal text."""
    price = format(Decimal(str(value)), "f")
    return price.rstrip("0").rstrip(".") if "." in price else price


def digest_material(preview: dict[str, Any]) -> dict[str, Any]:
    """Bind the account, intent, and any position route and quote.

    For a market open, bind the confirmed reference price. The intent price
    is refreshed before submission.
    """
    intent = copy.deepcopy(preview["intent"])
    body = intent.get("body", {})
    account = preview.get("addr")
    material: dict[str, Any] = {
        "addr": account.lower() if isinstance(account, str) else None,
        "intent": intent,
    }
    if intent.get("kind") == "OPEN_TRADE" and body.get("orderType") == "market":
        body.pop("intentPrice", None)
        reference = preview.get("referencePrice")
        material["referencePrice"] = (
            canonical_price(reference) if reference is not None else None
        )
    if "quote" in preview:
        material["quote"] = preview["quote"]
    if "positionRoute" in preview:
        material["positionRoute"] = preview["positionRoute"]
    return material


def preview_digest(preview: dict[str, Any]) -> str:
    """Hash the confirmed parts of the preview as canonical JSON."""
    canonical = json.dumps(
        digest_material(preview), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def is_confirmable(preview: dict[str, Any]) -> bool:
    """Require a verified route and a reference price for market opens."""
    return (
        "positionRoute" in preview
        and preview.get("positionOwnerVerified") is not False
        and (
            not preview.get("requiresMarketPriceRefresh")
            or preview.get("referencePrice") is not None
        )
    )


def with_digest(preview: dict[str, Any]) -> dict[str, Any]:
    if rejection_reason(preview.get("estimates", {})) is not None:
        return {key: value for key, value in preview.items() if key != "previewDigest"}
    return {**preview, "previewDigest": preview_digest(preview)}
