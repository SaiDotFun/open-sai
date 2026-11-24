"""Encrypted sessions for connected accounts and durable, non-secret pending
intent identities."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shlex
import sqlite3
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import log
from .errors import Code, SaiError, SessionError

PLAIN_KEEPER_URL = re.compile(
    r"https?://(?:[a-zA-Z0-9.-]+|\[[0-9a-fA-F:.]+\])(?::[0-9]+)?/query"
)


class UnresolvedIntent(SaiError, ValueError):
    """The intent ledger already blocks a submission for this account."""

    code = Code.INVALID_REQUEST


def home() -> Path:
    return Path(os.environ.get("SAI_HOME", Path.home() / ".sai")) / "trader"


def directory() -> Path:
    path = home()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


# Sessions are encrypted with this fixed key, which ships in the source, like
# the Cosmos SDK `test` keyring. It keeps tokens out of plaintext files, logs,
# and backups that grep for JWTs. It does not protect against anyone who can
# read the file and this code; the file mode (0600) does that part.
_SESSION_KEY = hashlib.sha256(b"sai-cli keyring-backend test; not a secret").digest()
# Version 3 holds every connected account and which one is active.
_VERSION = 3
_AAD = b"sai-sessions-v3"
# Version 2 held one session; it is read once and rewritten as version 3.
_V2_AAD = b"sai-session-v2-test"

SIGN_IN_AGAIN = "run `sai auth import` or `sai auth login NAME` again"
NO_SESSION = "No Sai session; run `sai auth import` or `sai auth login NAME`"
SESSION_FIELDS = ("token", "evmAddress", "expiresAt")
# What delete_session reports after deleting a store it could not read.
UNREADABLE_REMOVED = "unreadable session file"


def _sessions_path() -> Path:
    # home(), not directory(): reading must not create the directory.
    return home() / "sessions.json"


def _v2_path() -> Path:
    return home() / "session.json"


def _decrypt(fields: dict[str, Any], aad: bytes) -> Any:
    nonce, ciphertext = (
        base64.b64decode(fields[k], validate=True) for k in ("nonce", "ciphertext")
    )
    return json.loads(AESGCM(_SESSION_KEY).decrypt(nonce, ciphertext, aad))


def _check_session(session: Any) -> dict[str, str]:
    if not isinstance(session, dict) or not all(
        isinstance(session.get(k), str) for k in SESSION_FIELDS
    ):
        raise ValueError("Invalid session")
    return session


def _read_envelope(path: Path) -> dict[str, Any] | None:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"Stored session is unreadable; {SIGN_IN_AGAIN}") from exc
    return envelope if isinstance(envelope, dict) else {}


def _load_v2(fields: dict[str, Any]) -> dict[str, Any]:
    """Read a version 2 single-session file as a version 3 account set.

    Sessions from versions that used a passphrase (envelope version 1, or
    version 2 with backend `file`) are refused, never prompted for.
    """
    if fields.get("version") == 1 or (
        fields.get("version") == 2 and fields.get("backend") == "file"
    ):
        raise ValueError(
            "Stored session is passphrase-encrypted, which sai no longer"
            f" supports; {SIGN_IN_AGAIN}"
        )
    if fields.get("version") != 2 or fields.get("backend") != "test":
        raise ValueError(f"Unsupported session version; {SIGN_IN_AGAIN}")
    try:
        session = _check_session(_decrypt(fields, _V2_AAD))
        log.remember([session["token"]])
    except (ValueError, KeyError, TypeError, InvalidTag) as exc:
        raise ValueError(f"Cannot unlock session; {SIGN_IN_AGAIN}") from exc
    addr = session["evmAddress"].lower()
    return {"active": addr, "sessions": {addr: session}}


def load_accounts() -> dict[str, Any]:
    """Decrypt every connected account.

    Returns {"active": addr or None, "sessions": {addr: session}}, with
    addrs in lowercase. An empty set means nobody is signed in. A version
    2 `session.json` is read in place; the next save rewrites it.
    """
    fields = _read_envelope(_sessions_path())
    if fields is None:
        old = _read_envelope(_v2_path())
        if old is not None:
            return _load_v2(old)
        # A sign-in may have migrated the old file between the two reads.
        fields = _read_envelope(_sessions_path())
        if fields is None:
            return {"active": None, "sessions": {}}
    if fields.get("version") != _VERSION:
        raise ValueError(f"Unsupported session version; {SIGN_IN_AGAIN}")
    try:
        accounts = _decrypt(fields, _AAD)
        stored = accounts["sessions"]
        if not isinstance(stored, dict):
            raise TypeError("Invalid session set")
        sessions = {k: _check_session(v) for k, v in stored.items()}
        log.remember(session["token"] for session in sessions.values())
        active = accounts.get("active")
        if active is not None and active not in sessions:
            active = None
    except (ValueError, KeyError, TypeError, InvalidTag) as exc:
        raise ValueError(f"Cannot unlock session; {SIGN_IN_AGAIN}") from exc
    return {"active": active, "sessions": sessions}


def _save_accounts(accounts: dict[str, Any]) -> None:
    """Encrypt ACCOUNTS and store them at mode 0600; drop the version 2 file."""
    log.remember(s.get("token") for s in accounts["sessions"].values())
    nonce = os.urandom(12)
    plaintext = json.dumps(accounts).encode()
    ciphertext = AESGCM(_SESSION_KEY).encrypt(nonce, plaintext, _AAD)
    envelope = {
        "version": _VERSION,
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }
    root = directory()
    # mkstemp creates the file with mode 0600.
    fd, temp = tempfile.mkstemp(dir=root, prefix=".sessions-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(json.dumps(envelope))
        os.replace(temp, root / "sessions.json")
    finally:
        Path(temp).unlink(missing_ok=True)
    _v2_path().unlink(missing_ok=True)


@contextlib.contextmanager
def _locked() -> Generator[None]:
    """Hold an exclusive lock across one read-modify-write of the accounts,
    so concurrent sign-ins, switches, and logouts do not lose each other."""
    with open(directory() / "sessions.lock", "a+b") as handle:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _remove_files() -> None:
    _sessions_path().unlink(missing_ok=True)
    _v2_path().unlink(missing_ok=True)


def _find(accounts: dict[str, Any], addr: str) -> str:
    key = addr.lower()
    if key not in accounts["sessions"]:
        raise ValueError(
            f"{addr} is not connected; run `sai auth list` to see connected"
            " accounts, or sign in with `sai auth login NAME` or `sai auth import`"
        )
    return key


def save_session(session: dict[str, str]) -> None:
    """Add or replace the account for SESSION's addr and make it active.

    A store that cannot be read (damaged, or from a passphrase version) is
    replaced, since signing in is how the person recovers from it.
    """
    with _locked():
        try:
            accounts = load_accounts()
        except ValueError:
            accounts: dict[str, Any] = {"active": None, "sessions": {}}
        addr = session["evmAddress"].lower()
        accounts["sessions"][addr] = session
        accounts["active"] = addr
        _save_accounts(accounts)
    log.event(
        "session.saved",
        addr=session["evmAddress"],
        expiresAt=session["expiresAt"],
        endpoint=log.endpoint(session.get("endpoint")),
        connectedAccounts=len(accounts["sessions"]),
    )


def load_session(addr: str | None = None) -> dict[str, str]:
    """Return the session for ADDR, or the active account's session."""
    accounts = load_accounts()
    if addr is not None:
        try:
            key = _find(accounts, addr)
        except ValueError as exc:
            raise SessionError(str(exc), Code.SIGNED_OUT) from exc
        return accounts["sessions"][key]
    if accounts["active"] is None:
        if accounts["sessions"]:
            raise SessionError(
                "No active Sai account; run `sai auth use ADDR` with an"
                " addr from `sai auth list`",
                hint="sai auth list",
            )
        raise SessionError(NO_SESSION, Code.SIGNED_OUT)
    return accounts["sessions"][accounts["active"]]


def use_session(addr: str) -> dict[str, str]:
    """Make the connected ADDR the active account and return its session."""
    with _locked():
        accounts = load_accounts()
        key = _find(accounts, addr)
        accounts["active"] = key
        _save_accounts(accounts)
        log.event("session.active", addr=accounts["sessions"][key]["evmAddress"])
        return accounts["sessions"][key]


def session_exists() -> bool:
    """Report whether any session file is stored, without reading it."""
    return (home() / "sessions.json").is_file() or _v2_path().is_file()


def delete_session(addr: str | None = None) -> str | None:
    """Remove ADDR, or the active account, from this machine.

    Returns the removed addr, or None when there was nothing to remove.
    Removing the active account leaves no account active. Without ADDR, a
    store that cannot be read is deleted and reported as removed.
    """
    with _locked():
        # Checked under the lock: a sign-in may be migrating the old file.
        if not session_exists():
            return None
        try:
            accounts = load_accounts()
        except ValueError:
            if addr is not None:
                raise
            _remove_files()
            log.event(
                "session.removed",
                "warn",
                code=Code.SESSION_UNAVAILABLE,
                reason="unreadable session file",
            )
            return UNREADABLE_REMOVED
        if addr is None:
            key = accounts["active"]
            if key is None:
                return None
        else:
            key = addr.lower()
            if key not in accounts["sessions"]:
                return None
        removed = accounts["sessions"].pop(key)["evmAddress"]
        if accounts["active"] == key:
            accounts["active"] = None
        if accounts["sessions"]:
            _save_accounts(accounts)
        else:
            _remove_files()
        log.event(
            "session.removed", addr=removed, connectedAccounts=len(accounts["sessions"])
        )
        return removed


# Version 1 adds `created_at`, the Unix time when the CLI reserved the key.
SCHEMA_VERSION = 1


def _migrate(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError("Intent database was written by a newer sai version")
    if version == SCHEMA_VERSION:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, entity TEXT NOT NULL DEFAULT '', created_at REAL)"
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(intents)")
            }
            if "created_at" not in columns:
                connection.execute("ALTER TABLE intents ADD COLUMN created_at REAL")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


# Seconds a ledger write waits for another process's SQLite lock. A caller
# with a deadline, such as `sai doctor`, lowers it with `busy_timeout`.
_BUSY_TIMEOUT: ContextVar[float] = ContextVar("busy_timeout", default=10)


@contextmanager
def busy_timeout(seconds: float) -> Generator[None]:
    """Make ledger connections opened in this context wait at most SECONDS
    for a lock."""
    token = _BUSY_TIMEOUT.set(seconds)
    try:
        yield
    finally:
        _BUSY_TIMEOUT.reset(token)


def db() -> sqlite3.Connection:
    path = directory() / "intents.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    connection = sqlite3.connect(path, timeout=_BUSY_TIMEOUT.get())
    _migrate(connection)
    return connection


def keeper_command_hint(endpoint: str, *args: str) -> str:
    """Pin the selected Keeper in a recovery command.

    Only the standard /query path is safe to echo. Custom gateway paths
    can carry credentials.
    """
    if PLAIN_KEEPER_URL.fullmatch(endpoint) and log.redact(endpoint) == endpoint:
        flag = shlex.quote(endpoint)
    else:
        flag = (
            '"${SAI_RECONCILE_ENDPOINT:?'
            'Set SAI_RECONCILE_ENDPOINT to the original Keeper URL}"'
        )
    return f"sai --endpoint {flag} {shlex.join(args)}"


def intent_status_hint(endpoint: str, account: str, key: str) -> str:
    return keeper_command_hint(endpoint, "intent", "status", account, "--key", key)


def pending(key: str, endpoint: str, account: str, kind: str, entity: str) -> None:
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        unresolved = connection.execute(
            "SELECT id FROM intents WHERE endpoint=? AND account=? AND (status='unknown' OR (status='accepted' AND entity <> '' AND entity=?)) LIMIT 1",
            (endpoint, account.lower(), entity),
        ).fetchone()
        if unresolved:
            blocked_key = unresolved[0]
            raise UnresolvedIntent(
                f"Reconcile uncertain intent {blocked_key} before submitting another trade",
                intent_key=blocked_key,
                hint=intent_status_hint(endpoint, account, blocked_key),
            )
        connection.execute(
            "INSERT INTO intents (id, endpoint, account, kind, status, entity, created_at) VALUES (?, ?, ?, ?, 'unknown', ?, ?)",
            (key, endpoint, account.lower(), kind, entity, time.time()),
        )
    log.event(
        "intent.reserved",
        intentKey=key,
        kind=kind,
        status="unknown",
        addr=account,
        tradeId=entity or None,
        endpoint=log.endpoint(endpoint),
    )


def set_status(key: str, endpoint: str, account: str, status: str) -> None:
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT kind, status FROM intents WHERE id=? AND endpoint=? AND account=?",
            (key, endpoint, account.lower()),
        ).fetchone()
        connection.execute(
            "UPDATE intents SET status=? WHERE id=? AND endpoint=? AND account=?",
            (status, key, endpoint, account.lower()),
        )
    if row is not None and row[1] != status:
        _log_status(key, endpoint, account, row[0], row[1], status)


def _log_status(
    key: str, endpoint: str, account: str, kind: str, previous: str, status: str
) -> None:
    code = {"rejected": Code.ADMISSION_REJECTED, "not_sent": Code.NOT_SENT}.get(status)
    log.event(
        "intent.status",
        intentKey=key,
        kind=kind,
        previous=previous,
        status=status,
        addr=account,
        endpoint=log.endpoint(endpoint),
        **({"code": code} if code is not None else {}),
    )


def list_pending(endpoint: str, account: str) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute(
            "SELECT id,kind,status FROM intents WHERE endpoint=? AND account=? ORDER BY rowid DESC",
            (endpoint, account.lower()),
        ).fetchall()
    return [{"intentKey": row[0], "kind": row[1], "status": row[2]} for row in rows]


def _key_time(key: str) -> float | None:
    """Read the reservation time embedded in an intent key, if it has one."""
    try:
        stamp = datetime.strptime(key[:18], "%Y%m%dT%H%M%S%f")
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC).timestamp()


def mark_absent(
    key: str, endpoint: str, account: str, older_than: float, now: float | None = None
) -> str | None:
    """Mark an unknown intent `absent` once Keeper can no longer admit it.

    Call this only after Keeper returned no record for the key. Returns the
    local status after the check, or None when the key is not recorded here.
    """
    current = time.time() if now is None else now
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status, created_at, kind FROM intents WHERE id=? AND endpoint=? AND account=?",
            (key, endpoint, account.lower()),
        ).fetchone()
        if row is None:
            return None
        status, created, kind = row
        created = created if created is not None else _key_time(key)
        if (
            status == "unknown"
            and created is not None
            and current - created > older_than
        ):
            connection.execute(
                "UPDATE intents SET status='absent' WHERE id=? AND endpoint=? AND account=?",
                (key, endpoint, account.lower()),
            )
            absent = True
        else:
            absent = False
    if absent:
        _log_status(key, endpoint, account, kind, status, "absent")
        return "absent"
    return status
