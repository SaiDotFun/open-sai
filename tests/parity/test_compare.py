import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from compare import Entry, InvalidLogTime, Normalize, first_difference, ledger, snapshot
from parity import Observation, differences


def test_log_ids_preserve_joins_and_sequence(tmp_path: Path) -> None:
    def records(run: str, calls: list[str], times: list[str]) -> list[dict]:
        return [
            {"runId": run, "callId": call, "seq": seq, "ts": stamp, "event": event}
            for seq, (call, stamp, event) in enumerate(
                zip(
                    calls,
                    times,
                    ("call.start", "keeper.request", "call.end"),
                    strict=True,
                ),
                start=1,
            )
        ]

    times = ["2026-11-24T01:00:00Z", "2026-11-24T01:00:01Z", "2026-11-24T01:00:02Z"]
    reference = Normalize(tmp_path).log_records(
        records("a" * 32, ["b" * 12] * 3, times)
    )
    candidate = records(
        "c" * 32, ["d" * 12] * 3, [stamp.replace("01:", "02:", 1) for stamp in times]
    )
    assert (
        first_difference(reference, Normalize(tmp_path).log_records(candidate), "logs")
        is None
    )

    candidate[1]["callId"] = "e" * 12
    assert (
        first_difference(reference, Normalize(tmp_path).log_records(candidate), "logs")
        == "logs[1].callId"
    )
    candidate[1]["callId"] = "d" * 12
    candidate.reverse()
    assert (
        first_difference(reference, Normalize(tmp_path).log_records(candidate), "logs")
        == "logs[0].event"
    )


def test_json_compares_keys_and_types() -> None:
    assert first_difference({"x": 1, "y": [2]}, {"y": [2.0], "x": 1}, "stdout") is None
    assert first_difference({"x": 1}, {"x": True}, "stdout") == "stdout.x type"
    assert first_difference({"x": 1}, {}, "stdout") == "stdout keys"


def test_snapshot_reports_mode_and_semantic_config_changes(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("enabled = true\n", encoding="utf-8")
    config.chmod(0o600)
    normalize = Normalize(tmp_path.parent)
    reference = Observation(0, {}, (), snapshot(tmp_path, normalize))

    config.write_text("# fixture\nenabled=true\n", encoding="utf-8")
    assert (
        differences(reference, Observation(0, {}, (), snapshot(tmp_path, normalize)))
        == []
    )

    config.chmod(0o644)
    config.write_text("enabled = false\n", encoding="utf-8")
    changed = Observation(0, {}, (), snapshot(tmp_path, normalize))
    assert differences(reference, changed) == [
        "config.toml mode 0600 to 0644",
        "config.toml.enabled",
    ]
    assert changed.files["config.toml"] == Entry("file", 0o644, {"enabled": False})


def test_ledger_schema_ignores_only_insignificant_formatting(tmp_path: Path) -> None:
    compact = (
        "CREATE TABLE account (id INTEGER PRIMARY KEY);"
        "CREATE TABLE intents (id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'waiting now',"
        " owner INTEGER REFERENCES account(id) ON DELETE CASCADE);"
        "CREATE INDEX by_status ON intents(status);"
    )
    formatted = """
        CREATE TABLE account (id INTEGER PRIMARY KEY);
        CREATE TABLE intents (
            id text PRIMARY KEY,
            status text NOT NULL DEFAULT 'waiting now' /* keep the default */,
            owner INTEGER REFERENCES account (id) ON DELETE CASCADE
        );
        CREATE INDEX by_status
            ON intents (status);
    """

    def read(name: str, schema: str) -> dict:
        path = tmp_path / name
        with sqlite3.connect(path) as db:
            db.executescript(schema)
            db.execute("PRAGMA user_version = 1")
            db.execute("INSERT INTO intents (id, status) VALUES ('saved', 'pending')")
        return ledger(path, Normalize(tmp_path))

    reference = read("reference.sqlite3", compact)
    assert (
        first_difference(reference, read("format.sqlite3", formatted), "ledger") is None
    )
    mutations = {
        "literal": compact.replace("waiting now", "waiting  now"),
        "type": compact.replace("status TEXT", "status NUMERIC"),
        "constraint": compact.replace("NOT NULL ", ""),
        "rowid": compact.replace(");CREATE INDEX", ") WITHOUT ROWID;CREATE INDEX"),
        "index": compact.replace("CREATE INDEX", "CREATE UNIQUE INDEX"),
        "index-order": compact.replace("intents(status)", "intents(status DESC)"),
        "foreign-key": compact.replace("ON DELETE CASCADE", "ON DELETE RESTRICT"),
    }
    for name, schema in mutations.items():
        difference = first_difference(
            reference, read(f"{name}.sqlite3", schema), "ledger"
        )
        assert difference is not None and difference.startswith("ledger.schema"), name

    nullable = read(
        "nonbreaking-space.sqlite3", compact.replace("NOT NULL", "NOT\u00a0NULL")
    )
    assert reference["tables"] == nullable["tables"]
    assert first_difference(reference, nullable, "ledger") == (
        "ledger.schema.tables.intents.columns[1].notnull"
    )


def log_snapshot(
    home: Path,
    files: dict[str, list[tuple[int, str]]],
    start: str,
    end: str,
) -> dict[str, Entry]:
    root = home / "logs"
    root.mkdir(parents=True)
    for day, events in files.items():
        path = root / f"cli-{day}.jsonl"
        path.write_text(
            "".join(
                json.dumps({"ts": time, "seq": seq, "event": "fixture.event"}) + "\n"
                for seq, time in events
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
    return snapshot(
        home,
        Normalize(
            home,
            started_at=datetime.fromisoformat(start).timestamp(),
            ended_at=datetime.fromisoformat(end).timestamp(),
        ),
    )


def test_adjacent_utc_rollover_keeps_the_same_sequence(tmp_path: Path) -> None:
    split = log_snapshot(
        tmp_path / "split",
        {
            "2026-01-01": [(1, "2026-01-01T23:59:59.999Z")],
            "2026-01-02": [(2, "2026-01-02T00:00:00.001Z")],
        },
        "2026-01-01T23:59:59.999500Z",
        "2026-01-02T00:00:00.002Z",
    )
    compact = log_snapshot(
        tmp_path / "compact",
        {
            "2026-01-02": [
                (1, "2026-01-02T00:00:00.001Z"),
                (2, "2026-01-02T00:00:00.002Z"),
            ]
        },
        "2026-01-02T00:00:00Z",
        "2026-01-02T00:00:00.003Z",
    )
    assert split == compact
    reordered = log_snapshot(
        tmp_path / "reordered",
        {
            "2026-01-02": [
                (2, "2026-01-02T00:00:00.001Z"),
                (1, "2026-01-02T00:00:00.002Z"),
            ]
        },
        "2026-01-02T00:00:00Z",
        "2026-01-02T00:00:00.003Z",
    )
    assert (
        first_difference(
            split["logs/cli.jsonl"].data, reordered["logs/cli.jsonl"].data, "logs"
        )
        == "logs[0].seq"
    )


def test_logs_reject_a_correctly_named_stale_shard(tmp_path: Path) -> None:
    with pytest.raises(InvalidLogTime, match="outside the invocation window"):
        log_snapshot(
            tmp_path,
            {
                "2025-02-01": [(1, "2025-02-01T23:59:59.999Z")],
                "2026-01-02": [(2, "2026-01-02T00:00:00.001Z")],
            },
            "2026-01-01T23:59:59.999500Z",
            "2026-01-02T00:00:00.002Z",
        )


def test_logs_reject_a_valid_time_in_the_wrong_shard(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"Cannot decode logs/cli-2026-01-01"):
        log_snapshot(
            tmp_path,
            {"2026-01-01": [(1, "2026-01-02T00:00:00.001Z")]},
            "2026-01-01T23:59:59.999500Z",
            "2026-01-02T00:00:00.002Z",
        )
