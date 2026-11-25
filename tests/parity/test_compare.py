import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from compare import (
    LEDGER,
    MIN_SQLITE_SCHEMA,
    SQLITE_SCHEMA_REQUIREMENT,
    Entry,
    InvalidLogTime,
    Normalize,
    first_difference,
    ledger,
    mask_doctor_stream,
    mask_overview_stdout,
    snapshot,
    sqlite_schema,
    stdout,
)
from parity import Case, Observation, differences


def test_case_command_skips_global_options() -> None:
    case = Case(
        name="config-list",
        args=["--endpoint", "https://keeper.invalid", "--json", "config", "list"],
    )

    assert case.command == "config list"


def test_doctor_stream_masks_clock_values_and_keeps_other_bytes() -> None:
    text = "warn    lcd        http://127.0.0.1:9090 answered in 14 ms; another chain\n"
    assert mask_doctor_stream(text) == text.replace("14 ms", "<durationMs> ms")
    raw = (
        '{"generatedAt":"2026-11-24T01:00:00.123Z","checks":[{"id":"lcd",'
        '"durationMs":14,"details":{"matchesKeeperChain":false}}]}\n'
    )
    expected = raw.replace("2026-11-24T01:00:00.123Z", "<generatedAt>").replace(
        '"durationMs":14', '"durationMs":"<durationMs>"'
    )
    assert mask_doctor_stream(raw) == expected
    malformed = '{"generatedAt":"invalid","durationMs":14.5}\n'
    assert mask_doctor_stream(malformed) == malformed


def test_start_log_compares_command_and_argv_shape(tmp_path: Path) -> None:
    normalize = Normalize(tmp_path)
    start = {
        "ts": "2026-11-24T01:00:00.000Z",
        "event": "command.start",
        "command": "mcp install",
        "argv": ["mcp", "install", "--scope", "project"],
    }
    expected = normalize.log_event(start)
    names_only = {**start, "argv": ["mcp", "install", "--scope="]}

    assert normalize.log_event(names_only) == expected
    assert normalize.log_event({**names_only, "command": "mcp list"}) != expected
    assert normalize.log_event({**names_only, "argv": [42]}) != expected
    end = {**names_only, "event": "command.end"}
    assert normalize.log_event(end)["argv"] == names_only["argv"]


@pytest.mark.parametrize(
    ("field", "prefix", "length"),
    [("callId", "", 12), ("commandId", "", 32), ("endpointId", "h1_", 64)],
)
def test_log_ids_preserve_joins_and_sequence(
    tmp_path: Path, field: str, prefix: str, length: int
) -> None:
    def records(run: str, calls: list[str], times: list[str]) -> list[dict]:
        return [
            {"runId": run, field: call, "seq": seq, "ts": stamp, "event": event}
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

    times = [
        "2026-11-24T01:00:00.000Z",
        "2026-11-24T01:00:01.000Z",
        "2026-11-24T01:00:02.000Z",
    ]
    reference = Normalize(tmp_path).log_records(
        records("a" * 32, [prefix + "b" * length] * 3, times)
    )
    candidate_id = prefix + "d" * length
    candidate = records(
        "c" * 32,
        [candidate_id] * 3,
        [stamp.replace("01:", "02:", 1) for stamp in times],
    )
    assert (
        first_difference(reference, Normalize(tmp_path).log_records(candidate), "logs")
        is None
    )

    candidate[1][field] = prefix + "e" * length
    assert (
        first_difference(reference, Normalize(tmp_path).log_records(candidate), "logs")
        == f"logs[1].{field}"
    )
    candidate[1][field] = candidate_id
    candidate.reverse()
    assert (
        first_difference(reference, Normalize(tmp_path).log_records(candidate), "logs")
        == "logs[0].event"
    )


def test_snapshot_compares_log_endpoint_key_mode_and_length(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    key = logs / ".endpoint-key"
    key.write_bytes(b"a" * 32)
    key.chmod(0o600)
    normalize = Normalize(tmp_path)
    expected = snapshot(tmp_path, normalize)
    key.write_bytes(b"b" * 32)
    assert snapshot(tmp_path, normalize) == expected

    key.chmod(0o644)
    assert snapshot(tmp_path, normalize) != expected
    key.chmod(0o600)
    key.write_bytes(b"b" * 31)
    with pytest.raises(ValueError, match=r"Cannot decode logs/\.endpoint-key"):
        snapshot(tmp_path, normalize)


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


requires_sqlite_schema = pytest.mark.skipif(
    sqlite3.sqlite_version_info < MIN_SQLITE_SCHEMA, reason=SQLITE_SCHEMA_REQUIREMENT
)


@requires_sqlite_schema
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


@requires_sqlite_schema
def test_ledger_detects_column_collation_changes(tmp_path: Path) -> None:
    def read(name: str, collation: str) -> dict:
        home = tmp_path / name
        path = home / LEDGER
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute(f"""
                CREATE TABLE intents (
                    id TEXT PRIMARY KEY,
                    "endpoint" TEXT NOT NULL {collation} /* COLLATE RTRIM */
                        CHECK ("endpoint" COLLATE BINARY <> 'COLLATE NOCASE')
                )
            """)
            db.execute("INSERT INTO intents VALUES ('saved', '/query')")
        return ledger(path, Normalize(home))

    reference = read("reference", "")
    changed = read("changed", "COLLATE NOCASE")
    assert reference["tables"] == changed["tables"]
    assert first_difference(reference, changed, "ledger") == (
        "ledger.schema.tables.intents.columns[1].collation"
    )
    assert reference == read("explicit-default", "COLLATE BINARY")
    assert changed == read("formatted", "collate /* same name */ [nocase]")
    assert changed == read("repeated", 'COLLATE RTRIM COLLATE "nocase"')


@requires_sqlite_schema
@pytest.mark.parametrize(
    ("declared", "equivalent"),
    [("COLLATE NOCASE", "COLLATE [nocase]"), ("", "COLLATE binary")],
)
def test_ledger_ignores_equivalent_index_collations(
    tmp_path: Path, declared: str, equivalent: str
) -> None:
    def read(name: str, collation: str) -> dict:
        home = tmp_path / name
        path = home / LEDGER
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute(f"CREATE TABLE intents (id TEXT {collation} PRIMARY KEY)")
            db.execute("INSERT INTO intents VALUES ('saved')")
        return ledger(path, Normalize(home))

    reference = read("declared", declared)
    assert first_difference(reference, read("equivalent", equivalent), "ledger") is None


@requires_sqlite_schema
def test_ledger_compares_index_predicates_and_expressions(tmp_path: Path) -> None:
    ddl = """
        CREATE INDEX by_status ON intents (
            id, replace(status COLLATE RTRIM, 'where, )', 'COLLATE') COLLATE NOCASE
        ) WHERE status <> 'failed';
    """

    def read(name: str, index: str) -> dict:
        path = tmp_path / name
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE intents (id TEXT PRIMARY KEY, status TEXT)")
            db.execute(index)
            db.execute("INSERT INTO intents VALUES ('saved', 'pending')")
        return ledger(path, Normalize(tmp_path))

    reference = read("reference.sqlite3", ddl)
    formatted = """
        create index by_status on intents
            (ID, REPLACE ( STATUS collate [rtrim] , 'where, )', 'COLLATE' )
                collate /* index order */ "nocase")
        where /* partial index */ STATUS<>'failed';
    """
    assert reference == read("formatted.sqlite3", formatted)

    for filename, changed, field in (
        ("predicate", ddl.replace("'failed'", "'rejected'"), "where"),
        ("expression", ddl.replace("replace(status", "substr(status"), "expressions.1"),
        ("collation", ddl.replace("RTRIM", "BINARY"), "expressions.1"),
    ):
        actual = read(f"{filename}.sqlite3", changed)
        assert reference["tables"] == actual["tables"]
        difference = first_difference(reference, actual, "ledger")
        assert difference and difference.startswith(
            f"ledger.schema.tables.intents.indexes.by_status.{field}"
        )


@requires_sqlite_schema
def test_ledger_compares_collations_in_view_definitions(tmp_path: Path) -> None:
    def read(name: str, view: str) -> dict:
        path = tmp_path / name
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE intents (status TEXT)")
            db.execute(view)
        return ledger(path, Normalize(tmp_path))

    reference = read(
        "reference.sqlite3",
        "CREATE VIEW statuses AS SELECT status COLLATE NOCASE AS label FROM intents",
    )
    assert reference == read(
        "formatted.sqlite3",
        "create view statuses as select STATUS collate /* label */ [nocase]\n"
        " as label from intents",
    )
    changed = read(
        "changed.sqlite3",
        "CREATE VIEW statuses AS SELECT status COLLATE BINARY AS label FROM intents",
    )
    difference = first_difference(reference, changed, "ledger")
    assert difference and difference.startswith(
        "ledger.schema.tables.statuses.definition"
    )


@requires_sqlite_schema
@pytest.mark.parametrize(
    ("reference_sql", "equivalent", "changed"),
    (
        (
            "CREATE INDEX indexed ON intents(status) WHERE status <> 'failed'",
            (
                "CREATE INDEX indexed ON intents(status) WHERE ((status <> 'failed'))",
                "CREATE INDEX indexed ON intents(status) WHERE \"status\" <> 'failed'",
                "CREATE INDEX indexed ON intents(status) WHERE (status) <> ('failed')",
            ),
            "CREATE INDEX indexed ON intents(status) WHERE status <> 'status'",
        ),
        (
            "CREATE INDEX indexed ON intents(lower(status) DESC)",
            (
                "CREATE INDEX indexed ON intents((lower(status)) DESC)",
                'CREATE INDEX indexed ON intents(lower("STATUS") DESC)',
            ),
            "CREATE INDEX indexed ON intents(lower('status') DESC)",
        ),
        (
            "CREATE INDEX indexed ON intents(lower(status) COLLATE NOCASE)",
            ("CREATE INDEX indexed ON intents(((lower((status)))) COLLATE NOCASE)",),
            "CREATE INDEX indexed ON intents(lower(status) COLLATE BINARY)",
        ),
        (
            "CREATE INDEX indexed ON intents(17 DESC, lower(état))",
            ('CREATE INDEX indexed ON intents(((17)) DESC, lower("état"))',),
            "CREATE INDEX indexed ON intents(18 DESC, lower(état))",
        ),
        (
            "CREATE VIEW projected AS SELECT lower(status) AS label FROM intents WHERE priority + 1 * 2 > 3",
            (
                "CREATE VIEW projected AS SELECT (lower(status)) AS label FROM intents WHERE (priority + 1 * 2 > 3)",
                'CREATE VIEW projected AS SELECT lower("status") AS "label" FROM [intents] WHERE `priority` + 1 * 2 > 3',
            ),
            "CREATE VIEW projected AS SELECT lower(status) AS label FROM intents WHERE (priority + 1) * 2 > 3",
        ),
        (
            "CREATE VIEW projected AS SELECT lower(intents.status) COLLATE NOCASE AS label FROM intents WHERE priority IN (-2)",
            (
                "CREATE VIEW projected AS SELECT (lower((intents.status))) COLLATE NOCASE AS label FROM intents WHERE (priority) IN ((-2))",
            ),
            "CREATE VIEW projected AS SELECT lower(intents.status) COLLATE NOCASE AS label FROM intents WHERE priority IN (3)",
        ),
        (
            "CREATE VIEW projected AS SELECT label FROM (SELECT lower(status) label FROM intents)",
            (
                'CREATE VIEW projected AS SELECT label FROM (SELECT (lower("status")) label FROM intents)',
            ),
            "CREATE VIEW projected AS SELECT label FROM (SELECT upper(status) label FROM intents)",
        ),
        (
            'CREATE VIEW projected AS SELECT "true" AS label FROM intents',
            ('CREATE VIEW projected AS SELECT ("true") AS label FROM intents',),
            "CREATE VIEW projected AS SELECT true AS label FROM intents",
        ),
    ),
)
def test_schema_normalizes_only_safe_expression_formatting(
    tmp_path: Path, reference_sql: str, equivalent: tuple[str, ...], changed: str
) -> None:
    def read(name: str, sql: str) -> dict:
        path = tmp_path / name
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE intents (status TEXT, priority INTEGER, état TEXT)"
            )
            db.execute(sql)
        return ledger(path, Normalize(tmp_path))

    reference = read("reference.sqlite3", reference_sql)
    for i, sql in enumerate(equivalent):
        assert reference == read(f"equivalent-{i}.sqlite3", sql)
    difference = first_difference(reference, read("changed.sqlite3", changed), "ledger")
    assert difference and difference.startswith("ledger.schema.tables.")


@requires_sqlite_schema
def test_ledger_detects_added_or_changed_check_constraints(tmp_path: Path) -> None:
    ddl = """
        CREATE TABLE intents (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            [CHECK] TEXT DEFAULT 'CHECK(''not a constraint'')' /* CHECK(fake) */
        )
    """

    def read(name: str, schema: str) -> dict:
        path = tmp_path / name
        with sqlite3.connect(path) as db:
            db.execute(schema)
            db.execute("INSERT INTO intents (id, status) VALUES ('saved', 'unknown')")
        return ledger(path, Normalize(tmp_path))

    reference = read("reference.sqlite3", ddl)
    constraint = ", CHECK ((status <> 'rejected') AND [CHECK] <> 'CHECK()')"
    checked = read(
        "checked.sqlite3", ddl.replace("\n        )", constraint + "\n        )")
    )
    assert reference["tables"] == checked["tables"]
    assert reference["schema"]["tables"]["intents"]["checks"] == []
    assert first_difference(reference, checked, "ledger") == (
        "ledger.schema.tables.intents.checks length"
    )

    formatted = (
        ", check /* retain comments */ ( (STATUS<>'rejected') and [CHECK]<>'CHECK()' )"
    )
    equivalent = read(
        "formatted.sqlite3", ddl.replace("\n        )", formatted + "\n        )")
    )
    assert checked == equivalent
    try:
        bom = read(
            "bom.sqlite3",
            ddl.replace(
                "\n        )",
                constraint.replace("CHECK ", "CHECK \ufeff ") + "\n        )",
            ),
        )
    except sqlite3.OperationalError:
        legacy_bom = read(
            "legacy-bom.sqlite3",
            ddl.replace(
                "\n        )", ", \ufeff INTEGER, CHECK (\ufeff > 0)\n        )"
            ),
        )
        assert legacy_bom["schema"]["tables"]["intents"]["checks"] == [
            ["\ufeff", ">", "0"]
        ]
    else:
        assert checked == bom

    changed = read(
        "changed.sqlite3",
        ddl.replace(
            "\n        )", constraint.replace("'rejected'", "'failed'") + "\n        )"
        ),
    )
    difference = first_difference(checked, changed, "ledger")
    assert difference is not None and difference.startswith(
        "ledger.schema.tables.intents.checks"
    )


@requires_sqlite_schema
def test_ledger_schema_enumerates_without_table_list() -> None:
    def authorize(action: int, table: str | None, *_: object) -> int:
        if action == sqlite3.SQLITE_READ and table == "pragma_table_list":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE intents (id TEXT PRIMARY KEY) WITHOUT /* note */ ROWID"
        )
        conn.set_authorizer(authorize)
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT * FROM pragma_table_list").fetchall()
        schema = sqlite_schema(conn)
    assert schema["tables"]["intents"]["without_rowid"] is True


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


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-01-01T19:00:00.001-05:00",
        "2026-01-02T00:00:00.001+00:00",
        "2026-01-02T00:00:00.001000Z",
        "2026-01-02T00:00:00Z",
    ],
)
def test_noncanonical_log_time_fails_in_files_and_tail(
    tmp_path: Path, stamp: str
) -> None:
    with pytest.raises(InvalidLogTime, match="UTC millisecond format"):
        log_snapshot(
            tmp_path,
            {"2026-01-02": [(1, "2026-01-02T00:00:00.000Z"), (2, stamp)]},
            "2026-01-02T00:00:00Z",
            "2026-01-02T00:00:00.002Z",
        )
    with pytest.raises(InvalidLogTime, match="UTC millisecond format"):
        stdout(json.dumps({"ts": stamp}), "jsonl", "logs tail", Normalize(tmp_path))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('{"price": 1.0, "size": 2}', '{"price": 1, "size": 2}'),
        ('{"price": 1e+02}', '{"price": 100.0}'),
        ('{"price": 1, "size": 2}', '{"size": 2, "price": 1}'),
    ],
)
def test_mcp_compares_actual_json_text(tmp_path: Path, before: str, after: str) -> None:
    def decode_tool(text: str) -> Any:
        frame = {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {"result": text},
                "isError": False,
            },
        }
        return stdout(json.dumps(frame), "mcp", "mcp serve", Normalize(tmp_path))

    assert first_difference(decode_tool(before), decode_tool(after), "MCP")


def test_mcp_compares_schema_property_order(tmp_path: Path) -> None:
    def decode_inventory(properties: dict[str, Any]) -> Any:
        frame = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {"name": "fixture", "inputSchema": {"properties": properties}}
                ]
            },
        }
        return stdout(json.dumps(frame), "mcp", "mcp serve", Normalize(tmp_path))

    one = {"type": "integer"}
    two = {"type": "string"}
    assert first_difference(
        decode_inventory({"first": one, "second": two}),
        decode_inventory({"second": two, "first": one}),
        "MCP",
    )


def test_overview_text_ignores_clock_driven_table_widths():
    def table(age: str, status: str = "accepted") -> str:
        source = f"2000-01-01T00:00:00+00:00 ({age} s)"
        return (
            f"  {'Status':<8} │ {'Reserved (Age)':<{len(source)}}\n"
            f"  {'─' * (8 + 3 + len(source))}\n"
            f"  {status:<8} │ {source}\n"
            f"  {'unknown':<8} │ {'unknown':<{len(source)}}\n"
        )

    short = mask_overview_stdout(table("8.49e+08"), "text")
    long = mask_overview_stdout(table("8.4900001e+08"), "text")
    assert short == long
    leading_table = "  ID │ Price\n  ──────────\n  7  │ 100  \n"
    assert mask_overview_stdout(leading_table + table("8.49e+08"), "text") == (
        leading_table + long
    )
    assert short != mask_overview_stdout(table("8.49e+08", "rejected"), "text")
    assert short != mask_overview_stdout(
        table("8.49e+08").replace("──", "─", 1), "text"
    )
