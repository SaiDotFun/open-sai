"""Deliberately wrong candidate CLI selected by the executable's filename."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx
from sai_cli.core import log, storage, stream
from sai_cli.sai_cli import main

MUTATION = Path(sys.argv[0]).name
SAI_HOME = Path(os.environ["SAI_HOME"])
LEDGER = SAI_HOME / "trader/intents.sqlite3"


def change_http() -> None:
    send = httpx.post

    def post(*args: Any, **kwargs: Any) -> httpx.Response:
        if body := kwargs.get("json"):
            query = body["query"]
            if "tradeHistory" in query:
                if MUTATION == "history-order":
                    body["query"] = query.replace("order_desc:true", "order_desc:false")
                elif MUTATION == "history-projection":
                    body["query"] = query.replace(" evmTxHash", "")
                elif MUTATION == "history-operation":
                    body["query"] = query.replace("query(", "query Renamed(")
                    body["operationName"] = "Renamed"
                else:
                    body["query"] = query.replace(
                        "txHash evmTxHash", "evmTxHash  txHash"
                    )
        return send(*args, **kwargs)

    httpx.post = post  # ty: ignore[invalid-assignment]


def change_reservation_clock() -> None:
    reserve = storage.pending

    def pending(key: str, endpoint: str, account: str, kind: str, entity: str) -> None:
        reserve(key, endpoint, account, kind, entity)
        with storage.db() as conn:
            conn.execute(
                "UPDATE intents SET created_at = created_at * 1000 WHERE id = ?", (key,)
            )

    storage.pending = pending  # ty: ignore[invalid-assignment]


def change_schema() -> None:
    with sqlite3.connect(LEDGER) as conn:
        if MUTATION == "ledger-index":
            conn.execute("CREATE INDEX unexpected_status_idx ON intents(status)")
        elif MUTATION == "ledger-version":
            conn.execute("PRAGMA user_version = 2")
        else:
            (ddl,) = conn.execute(
                "SELECT sql FROM sqlite_schema WHERE name = 'intents'"
            ).fetchone()
            conn.execute("ALTER TABLE intents RENAME TO original_intents")
            conn.execute(ddl + " WITHOUT ROWID")
            conn.execute("INSERT INTO intents SELECT * FROM original_intents")
            conn.execute("DROP TABLE original_intents")


def change_config() -> None:
    path = SAI_HOME / "config.toml"
    text = path.read_text(encoding="utf-8")
    if MUTATION == "config-comment":
        text = text.removeprefix("# Keep this comment when editing\n")
    elif MUTATION == "config-inline":
        text = text.replace(" # Keep this inline comment", "")
    elif MUTATION == "config-order":
        original, logs = text.split("[logs]", 1)
        comment, update = original.split("[update]", 1)
        text = comment + "[logs]" + logs + "\n[update]" + update
    else:
        text = text.replace("keep_days  =  3", "keep_days=3")
    path.write_text(text, encoding="utf-8")


def rollover() -> None:
    assert log.flush()
    logs = SAI_HOME / "logs"
    events = []
    for source in sorted(logs.glob("cli-*.jsonl")):
        events.extend(
            json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()
        )
        source.unlink()
    assert len(events) >= 2
    if MUTATION == "midnight-order":
        events[0], events[1] = events[1], events[0]
    for index, event in enumerate(events):
        early = index == 0
        day = "2026-11-24" if early else "2026-11-25"
        event["ts"] = f"{day}T23:59:59Z" if early else f"{day}T00:00:{index:02d}Z"
        if MUTATION == "midnight-wrong-file":
            day = "2026-11-25"
        target = logs / f"cli-{day}.jsonl"
        with target.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event) + "\n")
        target.chmod(0o600)


if MUTATION.startswith("history-"):
    change_http()
elif MUTATION == "ws-projection":
    stream.PRICE = stream.PRICE.replace(" block_ts", "")  # ty: ignore[invalid-assignment]
elif MUTATION == "ledger-milliseconds":
    change_reservation_clock()
elif MUTATION.startswith("network-"):
    # The namespace prevents these documentation addresses from being routed.
    # Swallow the expected failure to ensure parity notices the attempt.
    assert "HTTPS_PROXY" not in os.environ
    family, address = (
        (socket.AF_INET, "192.0.2.1")
        if MUTATION == "network-ipv4"
        else (socket.AF_INET6, "2001:db8::1")
    )
    try:
        with socket.socket(family) as connection:
            connection.settimeout(1)
            connection.connect((address, 443))
    except OSError:
        pass

try:
    main()
finally:
    if MUTATION in {"ledger-rowid", "ledger-index", "ledger-version"}:
        change_schema()
    elif MUTATION.startswith("config-"):
        change_config()
    elif MUTATION.startswith("midnight-"):
        rollover()
