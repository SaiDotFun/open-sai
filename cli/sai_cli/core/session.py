"""Trading session checks shared by the CLI and other front ends."""

from __future__ import annotations

import base64
import json
import shlex
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from . import log, storage
from .settings import public_origin, public_url

EXPIRY_WARNING = timedelta(days=3)


def expiry_time(result: dict[str, str]) -> datetime:
    expiry = datetime.fromisoformat(result["expiresAt"])
    if expiry.tzinfo is None:
        raise ValueError("Stored session is invalid: expiry has no time zone")
    return expiry


def renewal_command(result: dict[str, str]) -> str:
    """Return recovery instructions scoped to the session's Keeper."""
    endpoint = result.get("endpoint")
    public = public_url(endpoint)
    origin = ""
    prefix = ["sai"]
    if public:
        parsed = urlsplit(public)
        origin = " at " + parsed._replace(path="").geturl()
        if public == endpoint and parsed.path in ("", "/", "/query", "/graphql"):
            prefix += ["--endpoint", public]
    suffix = (
        f" (reuse the Keeper endpoint{origin} configured when this session was stored)"
        if len(prefix) == 1
        else ""
    )
    if result.get("keyName"):
        return shlex.join([*prefix, "auth", "renew", result["evmAddress"]]) + suffix
    browser = shlex.join([*prefix, "auth", "import"])
    if result.get("source") == "imported":
        return browser + suffix
    local = shlex.join([*prefix, "auth", "login", "NAME"])
    return f"{local} or {browser}{suffix}"


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
        raise ValueError(f"Trading session expired. Run {renewal_command(result)}.")
    return result


def token_claims(token: str) -> dict[str, Any]:
    """Decode the public claims without verifying the token's signature."""
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if not isinstance(claims, dict):
        raise TypeError("Expected session claims")
    return claims


def import_session(token: str, endpoint: str) -> dict[str, str]:
    """Validate a Sai Web session TOKEN and build the session to store."""
    try:
        data = token_claims(token)
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


def summary(result: dict[str, str], *, origin_only: bool = False) -> dict[str, str]:
    """Return the fields safe to show: addr, expiry, endpoint; no token."""
    return {
        "addr": result["evmAddress"],
        "expiresAt": result["expiresAt"],
        "endpoint": (public_origin if origin_only else public_url)(result["endpoint"])
        or "",
    }


def expiry_fields(
    result: dict[str, str], now: datetime | None = None
) -> dict[str, str]:
    """Report a near-expiry warning without exposing the stored signer."""
    warning = expiry_warning(result, now or datetime.now(UTC))
    if warning is None:
        return {}
    return {"expiryWarning": warning, "renewalCommand": renewal_command(result)}
