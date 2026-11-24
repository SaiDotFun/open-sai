"""Trading session checks shared by the CLI and other front ends."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from . import log, storage
from .errors import Code, SessionError


def load_active_session(endpoint: str, addr: str | None = None) -> dict[str, str]:
    """Load the session for ADDR, or the active account, for ENDPOINT and
    refuse an expired one.

    Never prompts: sessions are encrypted with the CLI's built-in key.
    """
    try:
        return check_active(storage.load_session(addr), endpoint)
    except SessionError as exc:
        if exc.code == Code.SIGNED_OUT:
            flags = ("--addr", addr) if addr is not None else ()
            exc.hint = storage.keeper_command_hint(
                endpoint, "auth", "login", "NAME", *flags
            )
        raise
    except ValueError as exc:
        raise SessionError(str(exc)) from exc


def check_active(result: dict[str, str], endpoint: str) -> dict[str, str]:
    """Return RESULT if it belongs to ENDPOINT and has not expired."""
    if result.get("endpoint") != endpoint:
        raise SessionError("Stored session belongs to another Keeper endpoint")
    try:
        expiry = datetime.fromisoformat(result["expiresAt"])
        if expiry.tzinfo is None:
            raise ValueError("Expiry has no time zone")
    except (KeyError, ValueError) as exc:
        raise SessionError(
            f"Stored session has an invalid expiry; {storage.SIGN_IN_AGAIN}"
        ) from exc
    if expiry <= datetime.now(UTC):
        log.event(
            "session.expired",
            "warn",
            code=Code.SESSION_EXPIRED,
            addr=result.get("evmAddress"),
            expiresAt=result["expiresAt"],
        )
        raise SessionError(
            "Trading session expired; sign in again",
            Code.SESSION_EXPIRED,
            hint=storage.keeper_command_hint(
                endpoint, "auth", "login", "NAME", "--addr", result["evmAddress"]
            ),
        )
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
        ):
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid or expired Sai session token") from exc
    if expires <= datetime.now(UTC):
        raise SessionError("Invalid or expired Sai session token", Code.SESSION_EXPIRED)
    return {
        "token": token,
        "evmAddress": addr,
        "expiresAt": expires.isoformat(),
        "endpoint": endpoint,
    }


def summary(result: dict[str, str]) -> dict[str, str | None]:
    """Return the address, expiry, and Keeper origin for display."""
    endpoint = result.get("endpoint")
    return {
        "addr": result["evmAddress"],
        "expiresAt": result["expiresAt"],
        "endpoint": log.origin(endpoint) if endpoint is not None else None,
    }
