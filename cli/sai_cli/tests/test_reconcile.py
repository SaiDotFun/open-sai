"""Resolve intents that Keeper never recorded."""

from __future__ import annotations

import socket
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest
from sai_cli.core import intents, keeper, storage

ADDR = "0x" + "a" * 40


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))


def closed_endpoint() -> str:
    """Return a loopback URL whose port refuses connections."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}/query"


def session_for(endpoint: str) -> dict[str, str]:
    return {
        "token": "fake-token",
        "evmAddress": ADDR,
        "endpoint": endpoint,
        "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }


def test_connect_error_is_not_sent_and_does_not_block():
    endpoint = closed_endpoint()
    active = session_for(endpoint)
    with pytest.raises(keeper.NotSent):
        intents.submit(endpoint, active, intents.close_intent(7))
    assert storage.list_pending(endpoint, ADDR)[0]["status"] == "not_sent"
    # A second attempt reaches the transport instead of the reconcile guard.
    with pytest.raises(keeper.NotSent):
        intents.submit(endpoint, active, intents.close_intent(7))


def test_mark_absent_waits_for_admission_expiry():
    endpoint = keeper.MAINNET_HTTP
    storage.pending("K1", endpoint, ADDR, "CLOSE_POSITION", "7")
    after = keeper.ABSENT_AFTER_SECONDS
    now = time.time()
    assert storage.mark_absent("K1", endpoint, ADDR, after, now) == "unknown"
    assert storage.mark_absent("K1", endpoint, ADDR, after, now + after + 1) == "absent"
    assert storage.mark_absent("missing", endpoint, ADDR, after) is None
    # An absent key no longer blocks the same position.
    storage.pending("K2", endpoint, ADDR, "CLOSE_POSITION", "7")


def test_v010_trade_guard_sees_pending_intent_after_downgrade():
    endpoint = closed_endpoint()
    storage.pending("K1", endpoint, ADDR, "OPEN_TRADE", "")
    assert storage.set_status("K1", endpoint, ADDR, "pending") == "pending"
    assert storage.list_pending(endpoint, ADDR)[0]["status"] == "pending"

    with sqlite3.connect(storage.home() / "intents.sqlite3") as old_db:
        assert old_db.execute("PRAGMA user_version").fetchone() == (1,)
        # sai-cli/v0.1.0's reservation query for a different trade entity.
        blocker = old_db.execute(
            "SELECT id FROM intents WHERE endpoint=? AND account=?"
            " AND (status='unknown' OR (status='accepted' AND entity <> '' AND entity=?)) LIMIT 1",
            (endpoint, ADDR.lower(), "99"),
        ).fetchone()
    assert blocker == ("K1",)


def test_unversioned_database_migrates_and_uses_key_time():
    path = storage.directory() / "intents.sqlite3"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, entity TEXT NOT NULL DEFAULT '')"
    )
    stamp = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y%m%dT%H%M%S")
    key = f"{stamp}000Z_ABCDEFGHJKMNPQRS"
    old.execute(
        "INSERT INTO intents VALUES (?, ?, ?, 'CLOSE_POSITION', 'unknown', '7')",
        (key, keeper.MAINNET_HTTP, ADDR),
    )
    old.commit()
    old.close()

    status = storage.mark_absent(
        key, keeper.MAINNET_HTTP, ADDR, keeper.ABSENT_AFTER_SECONDS
    )
    assert status == "absent"
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    columns = {row[1] for row in connection.execute("PRAGMA table_info(intents)")}
    assert "created_at" in columns
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="newer sai version"):
        storage.list_pending(keeper.MAINNET_HTTP, ADDR)
