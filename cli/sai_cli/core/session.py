"""Trading session checks shared by the CLI and other front ends."""

from __future__ import annotations

import base64
import json
import shlex
from datetime import UTC, datetime, timedelta
from typing import Any

from . import keeper, log, storage
from .errors import Code, SessionError

EXPIRY_WARNING = timedelta(days=3)


def _redact_field(result: dict[str, str], value: str) -> str:
    secrets = (result.get("token"), *log.known_tokens())
    return (
        log.REDACTED if any(secret and secret in value for secret in secrets) else value
    )


def _display_origin(result: dict[str, str], endpoint: str) -> str:
    # Compare the stored value before parsing, which can change its spelling.
    if _redact_field(result, endpoint) == log.REDACTED:
        return log.REDACTED
    return _redact_field(result, log.origin(endpoint))


def expiry_time(result: dict[str, str]) -> datetime:
    try:
        expiry = datetime.fromisoformat(result["expiresAt"])
        if expiry.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SessionError(storage.INVALID_EXPIRY) from None
    return expiry


def validate_issued_session(result: dict[str, str]) -> None:
    """Reject a Keeper reply that the session store or expiry reader cannot use."""
    try:
        expiry_time(storage.check_session_fields(result))
    except SessionError:
        raise keeper.KeeperError("Keeper returned an invalid session expiry") from None
    except ValueError:
        raise keeper.KeeperError("Keeper returned invalid session data") from None


def renewal_command(result: dict[str, str]) -> str:
    """Return recovery instructions scoped to the session's Keeper."""
    endpoint = result.get("endpoint")
    origin = _display_origin(result, endpoint) if endpoint else None

    def command(*args: str) -> str:
        if endpoint:
            url = log.REDACTED if origin == log.REDACTED else endpoint
            return storage.keeper_command_hint(url, "auth", *args)
        return shlex.join(["sai", "auth", *args])

    suffix = ""
    if not endpoint or endpoint != origin:
        where = (
            f" at {origin}"
            if origin not in (None, log.REDACTED, log.REDACTED_URL)
            else ""
        )
        suffix = f" (reuse the Keeper endpoint{where} configured when this session was stored)"
    if result.get("keyName"):
        recovery = command("renew", result["evmAddress"])
    else:
        browser = command("import")
        recovery = (
            browser
            if result.get("source") == "imported"
            else f"{command('login', 'NAME')} or {browser}"
        )
    return _redact_field(result, recovery + suffix)


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
    return _redact_field(
        result, f"Sai session for {result['evmAddress']} expires in {when}."
    )


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
    expiry = expiry_time(result)
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
            hint=renewal_command(result)
            if result.get("keyName") or result.get("source") == "imported"
            else storage.keeper_command_hint(
                endpoint, "auth", "login", "NAME", "--addr", result["evmAddress"]
            ),
        )
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
        "source": "imported",
    }


def session_address(result: dict[str, str], account_key: str) -> str:
    addr = result["evmAddress"]
    if not keeper.is_address(addr) or addr.lower() != account_key:
        raise SessionError(
            f"Stored session has an invalid address; {storage.SIGN_IN_AGAIN}"
        )
    return addr


def summary(
    result: dict[str, str], *, account_key: str | None = None
) -> dict[str, str | None]:
    """Return the address, expiry, and Keeper origin for display."""
    endpoint = result.get("endpoint")
    if endpoint is not None and not isinstance(endpoint, str):
        raise SessionError(
            f"Stored session has an invalid endpoint; {storage.SIGN_IN_AGAIN}"
        )
    addr = (
        result["evmAddress"]
        if account_key is None
        else session_address(result, account_key)
    )
    return {
        "addr": _redact_field(result, addr),
        "expiresAt": _redact_field(result, result["expiresAt"]),
        "endpoint": _display_origin(result, endpoint) if endpoint is not None else None,
    }


def expiry_fields(
    result: dict[str, str], now: datetime | None = None
) -> dict[str, str]:
    """Report a near-expiry warning without exposing the stored signer."""
    warning = expiry_warning(result, now or datetime.now(UTC))
    if warning is None:
        return {}
    return {"expiryWarning": warning, "renewalCommand": renewal_command(result)}
