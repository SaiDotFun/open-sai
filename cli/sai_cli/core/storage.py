"""Encrypted sessions for connected accounts and durable, non-secret pending
intent identities."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import shlex
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import log, paths
from .errors import Code, SaiError, SessionError, describe


class IntentReservationError(SaiError):
    """A local ledger failure prevented submission to Keeper."""

    def __init__(
        self,
        cause: Exception | KeyboardInterrupt,
        *,
        intent_key: str | None = None,
        hint: str | None = None,
        account: str | None = None,
    ) -> None:
        if isinstance(cause, KeyboardInterrupt):
            message, code = "Trade interrupted.", Code.INVALID_REQUEST
        else:
            failure = describe(cause)
            detail = (
                str(cause)
                if isinstance(cause, (sqlite3.Error, OSError))
                else failure.message
            )
            message = f"Could not reserve the trade intent. {detail}."
            code = failure.code
        message += " Nothing was sent."
        if intent_key is not None:
            message += " Sai could not verify or release the local reservation."
        super().__init__(message, code, intent_key=intent_key, hint=hint)
        self.account = account


class UnresolvedIntent(SaiError, ValueError):
    """The intent ledger already blocks a submission for this account."""

    code = Code.INVALID_REQUEST


class RecentOpenPending(UnresolvedIntent):
    """An earlier open on the same account may still execute."""

    code = Code.RECENT_OPEN_PENDING

    def __init__(self, keys: list[str], endpoint: str, account: str) -> None:
        key = keys[0]
        hint = intent_status_hint(endpoint, account, key)
        super().__init__(
            f"Earlier open intent {key} may still execute."
            f" Check its status with `{hint}` before retrying.",
            intent_key=key,
            hint=hint,
        )
        self.keys = keys


def home() -> Path:
    return paths.sai_home() / "trader"


def directory() -> Path:
    path = home()
    paths.private_directory(path.parent, profile_root=True)
    return paths.private_directory(path)


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
INVALID_EXPIRY = f"Stored session has an invalid expiry; {SIGN_IN_AGAIN}"
NO_SESSION = "No Sai session; run `sai auth import` or `sai auth login NAME`"
SESSION_FIELDS = ("token", "evmAddress")
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
    if not isinstance(session.get("expiresAt"), str):
        raise SessionError(INVALID_EXPIRY)
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
    except SessionError:
        raise
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
    except SessionError:
        raise
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
    try:
        _v2_path().unlink(missing_ok=True)
    except OSError:
        logging.getLogger(__name__).warning(
            "Could not remove legacy session file trader/session.json after saving accounts."
        )


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


def _find(accounts: dict[str, Any], addr: str, *, include_addr: bool = True) -> str:
    key = addr.lower()
    if key not in accounts["sessions"]:
        raise ValueError(
            f"{addr if include_addr else 'Requested account'} is not connected;"
            " run `sai auth list` to see connected"
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
    _log_saved(session, len(accounts["sessions"]))


def replace_session(previous: dict[str, str], session: dict[str, str]) -> None:
    """Renew the same stored session without switching the active account."""
    with _locked():
        accounts = load_accounts()
        addr = previous["evmAddress"].lower()
        if accounts["sessions"].get(addr) != previous:
            raise ValueError(
                "Stored session changed during renewal. Run sai auth status."
            )
        accounts["sessions"][addr] = session
        _save_accounts(accounts)
    _log_saved(session, len(accounts["sessions"]))


def _log_saved(session: dict[str, str], count: int) -> None:
    log.event(
        "session.saved",
        addr=session["evmAddress"],
        expiresAt=session["expiresAt"],
        endpoint=log.endpoint(session.get("endpoint")),
        connectedAccounts=count,
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


def use_session(
    addr: str, prepare: Callable[[dict[str, Any]], dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Select ADDR, preparing its response from the candidate state before saving."""
    from .session import expiry_time, session_address

    with _locked():
        accounts = load_accounts()
        key = _find(accounts, addr, include_addr=False)
        session = accounts["sessions"][key]
        address = session_address(session, key)
        expiry_time(session)
        accounts["active"] = key
        response = session if prepare is None else prepare(accounts)
        _save_accounts(accounts)
        log.event("session.active", addr=address)
        return response


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


def _intent_columns(connection: sqlite3.Connection) -> set[str]:
    return {row[1] for row in connection.execute("PRAGMA table_info(intents)")}


def _migrate(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError("Intent database was written by a newer sai version")
    if version == SCHEMA_VERSION and "keeper_status" in _intent_columns(connection):
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, entity TEXT NOT NULL DEFAULT '', created_at REAL)"
            )
            if "created_at" not in _intent_columns(connection):
                connection.execute("ALTER TABLE intents ADD COLUMN created_at REAL")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        # Keep version 1 readable by v0.1.0. Its trade guard still sees
        # status='unknown' for pending intents and uses explicit column lists.
        if "keeper_status" not in _intent_columns(connection):
            connection.execute("ALTER TABLE intents ADD COLUMN keeper_status TEXT")
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
    connection = sqlite3.connect(path, timeout=_BUSY_TIMEOUT.get())
    try:
        # Closing a separate file descriptor would release any POSIX locks
        # that another connection in this process holds on the ledger.
        path.chmod(0o600)
        _migrate(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def keeper_command_hint(endpoint: str, *args: str) -> str:
    """Pin the selected Keeper in a recovery command.

    Ask the caller to supply the original URL when the origin is insufficient.
    """
    origin = log.origin(endpoint)
    if origin != log.REDACTED_URL and origin == endpoint == log.redact(endpoint):
        flag = shlex.quote(endpoint)
    else:
        flag = (
            '"${SAI_RECONCILE_ENDPOINT:?'
            'Set SAI_RECONCILE_ENDPOINT to the original Keeper URL}"'
        )
    return f"sai --endpoint {flag} {shlex.join(args)}"


def intent_status_hint(endpoint: str, account: str, key: str) -> str:
    return keeper_command_hint(endpoint, "intent", "status", account, "--key", key)


def _check_open_pending(
    connection: sqlite3.Connection,
    endpoint: str,
    account: str,
    allow_concurrent_open: bool,
) -> None:
    # Locally pending opens store 'unknown' in status for older readers.
    opens = connection.execute(
        "SELECT id FROM intents WHERE endpoint=? AND account=?"
        " AND kind='OPEN_TRADE' AND (keeper_status='pending'"
        " OR (NOT ? AND status IN ('unknown', 'accepted')))"
        " ORDER BY rowid DESC",
        (endpoint, account.lower(), allow_concurrent_open),
    ).fetchall()
    if opens:
        raise RecentOpenPending([row[0] for row in opens], endpoint, account)


@contextmanager
def _reservation_errors() -> Generator[None]:
    try:
        yield
    except UnresolvedIntent:
        raise
    except (sqlite3.Error, OSError, ValueError, KeyboardInterrupt) as exc:
        raise IntentReservationError(exc) from exc


def check_open_pending(
    endpoint: str, account: str, *, allow_concurrent_open: bool
) -> None:
    """Refuse an already blocked open early. Reservation checks again."""
    with _reservation_errors(), contextlib.closing(db()) as connection:
        _check_open_pending(connection, endpoint, account, allow_concurrent_open)


def pending(
    key: str,
    endpoint: str,
    account: str,
    kind: str,
    entity: str,
    *,
    allow_concurrent_open: bool = False,
) -> None:
    opening = kind == "OPEN_TRADE"
    concurrent_open = opening and allow_concurrent_open
    with _reservation_errors():
        connection = db()
    # Leave the error wrapper before committing. A commit error may have
    # written the row, so the submitter must check the generated key.
    with contextlib.closing(connection), connection, _reservation_errors():
        connection.execute("BEGIN IMMEDIATE")
        if opening:
            _check_open_pending(connection, endpoint, account, concurrent_open)
        unresolved = connection.execute(
            "SELECT id FROM intents WHERE endpoint=? AND account=?"
            " AND ((status='unknown' AND (NOT ? OR kind<>'OPEN_TRADE'))"
            " OR keeper_status='pending'"
            " OR (status='accepted' AND entity <> '' AND entity=?)) LIMIT 1",
            (endpoint, account.lower(), concurrent_open, entity),
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
        endpointId=log.endpoint_identity(endpoint),
    )


_STATUS_TRANSITIONS = {
    "unknown": {"accepted", "pending", "terminal", "rejected", "not_sent"},
    # A positive Keeper result can supersede an earlier empty read.
    "absent": {"pending", "terminal"},
    "accepted": {"pending", "terminal"},
    "pending": {"terminal"},
}


def set_status(
    key: str,
    endpoint: str,
    account: str,
    status: str,
    *,
    repair_legacy_terminal: bool = False,
    only_if_absent: bool = False,
) -> str | None:
    """Advance a recorded intent and return its current status.

    Check the transition under the same write lock as reservation and expiry.
    A late acknowledgement or status response cannot downgrade the ledger.
    Explicit evidence of a pending market order can repair a legacy terminal
    row whose outcome was never tracked by this version.
    With ONLY_IF_ABSENT, leave every other status alone under the write lock.
    """
    if not (home() / "intents.sqlite3").exists():
        return None
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT kind, status, keeper_status FROM intents"
            " WHERE id=? AND endpoint=? AND account=?",
            (key, endpoint, account.lower()),
        ).fetchone()
        if row is None:
            return None
        kind, stored, keeper_status = row
        previous = keeper_status or stored
        if only_if_absent and previous != "absent":
            return previous
        if previous == "absent" and status == "accepted":
            # A late admission can still execute, even after the expiry check.
            status = "pending"
        legacy_terminal = stored == "terminal" and keeper_status is None
        repair = legacy_terminal and (
            status == "terminal" or (status == "pending" and repair_legacy_terminal)
        )
        if status not in _STATUS_TRANSITIONS.get(previous, ()) and not repair:
            return previous
        connection.execute(
            "UPDATE intents SET status=?, keeper_status=?"
            " WHERE id=? AND endpoint=? AND account=?",
            (
                "unknown" if status == "pending" else status,
                status if status in ("pending", "terminal") else None,
                key,
                endpoint,
                account.lower(),
            ),
        )
    if previous != status:
        _log_status(key, endpoint, account, kind, previous, status)
    return status


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
        endpointId=log.endpoint_identity(endpoint),
        code=code,
    )


def list_pending(endpoint: str, account: str) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute(
            "SELECT id,kind,COALESCE(keeper_status,status) FROM intents"
            " WHERE endpoint=? AND account=? ORDER BY rowid DESC",
            (endpoint, account.lower()),
        ).fetchall()
    return [{"intentKey": row[0], "kind": row[1], "status": row[2]} for row in rows]


def intent_record(endpoint: str, key: str) -> tuple[str, str] | None:
    """Return the recorded account and status for KEY on this endpoint."""
    if not (home() / "intents.sqlite3").exists():
        return None
    with db() as connection:
        row = connection.execute(
            "SELECT account, COALESCE(keeper_status,status) FROM intents"
            " WHERE id=? AND endpoint=?",
            (key, endpoint),
        ).fetchone()
    return (row[0], row[1]) if row else None


def _key_time(key: str) -> float | None:
    """Read the reservation time embedded in an intent key, if it has one."""
    try:
        stamp = datetime.strptime(key[:18], "%Y%m%dT%H%M%S%f")
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC).timestamp()


def unresolved_intents(endpoint: str, account: str) -> list[dict[str, Any]]:
    """Read unknown, accepted, and pending intents without migrating a ledger."""
    path = home() / "intents.sqlite3"
    try:
        path.stat()
    except FileNotFoundError:
        return []
    with contextlib.closing(
        sqlite3.connect(
            f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT.get()
        )
    ) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError("Intent database was written by a newer sai version")
        created_column = "created_at" if version >= 1 else "NULL"
        status = (
            "COALESCE(keeper_status,status)"
            if "keeper_status" in _intent_columns(connection)
            else "status"
        )
        rows = connection.execute(
            f"SELECT id, kind, {status}, entity, {created_column} FROM intents"
            f" WHERE endpoint=? AND account=? AND {status} IN ('unknown', 'accepted', 'pending')"
            " ORDER BY rowid DESC",
            (endpoint, account.lower()),
        ).fetchall()
    return [
        {
            "intentKey": key,
            "kind": kind,
            "status": status,
            "entity": entity,
            "createdAt": created,
        }
        for key, kind, status, entity, created in rows
    ]


def mark_absent(
    key: str, endpoint: str, account: str, older_than: float, now: float | None = None
) -> str | None:
    """Mark an unknown intent `absent` after the admission grace period.

    Call this only after Keeper returned no record for the key. Returns the
    local status after the check, or None when the key is not recorded here.
    """
    current = time.time() if now is None else now
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT COALESCE(keeper_status,status), created_at, kind FROM intents"
            " WHERE id=? AND endpoint=? AND account=?",
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
