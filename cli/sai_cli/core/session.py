"""Trading session checks shared by the CLI and other front ends."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from . import log, storage

EXPIRY_WARNING = timedelta(days=3)


def expiry_time(result: dict[str, str]) -> datetime:
    expiry = datetime.fromisoformat(result["expiresAt"])
    if expiry.tzinfo is None:
        raise ValueError("Stored session is invalid: expiry has no time zone")
    return expiry


def renewal_command(result: dict[str, str]) -> str:
    """Return the command appropriate for this stored session's signer."""
    if result.get("keyName"):
        return f"sai auth renew {result['evmAddress']}"
    if result.get("source") == "imported":
        return "sai auth import"
    return "sai auth login NAME or sai auth import"


def expiry_warning(result: dict[str, str], now: datetime) -> str | None:
    left = expiry_time(result) - now
    if not timedelta(0) < left < EXPIRY_WARNING:
        return None
    minutes = int(left.total_seconds() // 60)
    when = (
        "under 1 min"
        if minutes == 0
        else f"{minutes} min"
        if minutes < 120
        else f"{minutes // 60} h"
    )
    return f"Sai session for {result['evmAddress']} expires in {when}."


def load_active_session(endpoint: str, addr: str | None = None) -> dict[str, str]:
    """Load the session for ADDR, or the active account, for ENDPOINT and
    refuse an expired one.

    Never prompts: sessions are encrypted with the CLI's built-in key.
    """
    return check_active(storage.load_session(addr), endpoint)


def check_active(result: dict[str, str], endpoint: str) -> dict[str, str]:
    """Return RESULT if it belongs to ENDPOINT and has not expired."""
    if result.get("endpoint") != endpoint:
        raise ValueError("Stored session belongs to another Keeper endpoint")
    expiry = expiry_time(result)
    if expiry <= datetime.now(UTC):
        log.event(
            "session.expired",
            "warn",
            addr=result.get("evmAddress"),
            expiresAt=result["expiresAt"],
        )
        raise ValueError("Trading session expired; sign in again")
    return result


def import_session(token: str, endpoint: str) -> dict[str, str]:
    """Validate a Sai Web session TOKEN and build the session to store."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        addr = data["sub"]
        expires = datetime.fromtimestamp(data["exp"], UTC)
        if (
            not isinstance(addr, str)
            or not addr.startswith("0x")
            or len(addr) != 42
            or data.get("purpose") != "user_intent"
            or expires <= datetime.now(UTC)
        ):
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid or expired Sai session token") from exc
    return {
        "token": token,
        "evmAddress": addr,
        "expiresAt": expires.isoformat(),
        "endpoint": endpoint,
        "source": "imported",
    }


def summary(result: dict[str, str]) -> dict[str, str]:
    """Return the fields safe to show: addr, expiry, endpoint; no token."""
    return {
        "addr": result["evmAddress"],
        "expiresAt": result["expiresAt"],
        "endpoint": result["endpoint"],
    }


def expiry_fields(
    result: dict[str, str], now: datetime | None = None
) -> dict[str, str]:
    """Report a near-expiry warning without exposing the stored signer."""
    warning = expiry_warning(result, now or datetime.now(UTC))
    if warning is None:
        return {}
    return {"expiryWarning": warning, "renewalCommand": renewal_command(result)}
