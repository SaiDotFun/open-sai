"""Compare CLI observations without emitting their potentially sensitive values."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

import tomlkit
from cryptography.exceptions import InvalidTag
from sai_cli.core import storage
from tomlkit.items import Item

VERSION = re.compile(r"v?\d+\.\d+\.\d+(?:[-+][\w.+-]+)?")
LOG_FILE = re.compile(r"logs/(?P<surface>[a-z]+)-(?P<day>\d{4}-\d{2}-\d{2})\.jsonl")
LOG_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z"
)
LEDGER = "trader/intents.sqlite3"
MIN_SQLITE_SCHEMA = (3, 26, 0)
SQLITE_SCHEMA_REQUIREMENT = (
    "Ledger parity requires SQLite 3.26.0 or newer for table_xinfo"
)
SIDECARS = ("-wal", "-shm", "-journal")
DURATION = re.compile(r"(?<= in )\d+(?= ms\b)")
INTENT_ID = re.compile(r"\b\d{8}T\d{9}Z_[0-9A-HJKMNP-TV-Z]{16}\b")
TOML_TABLE = re.compile(r"\s*\[([^\[\]]+)\]\s*(?:#.*)?")
SQLITE_TRIVIA = r"[ \t\n\f\r]+|--[^\n]*|/\*[\s\S]*?(?:\*/|$)"
SQLITE_SYNTAX = (
    r"""|(?:[xX])?'(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]"""
    r"|(?P<name>[a-zA-Z_\x80-\U0010ffff][a-zA-Z_0-9$\x80-\U0010ffff]*)"
    r"|0[xX][0-9a-fA-F_]+|(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9][0-9_]*)?"
    r"|->>|->|<=|>=|<>|!=|==|\|\||<<|>>|."
)
ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


class InvalidReservationTime(ValueError):
    """The ledger clock cannot be normalized."""


class InvalidLogTime(ValueError):
    """An event timestamp has a noncanonical format or is outside the run."""


def parse_log_timestamp(value: object) -> datetime:
    message = "Log timestamp must use UTC millisecond format YYYY-MM-DDTHH:MM:SS.sssZ"
    if not isinstance(value, str) or LOG_TIMESTAMP.fullmatch(value) is None:
        raise InvalidLogTime(message)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise InvalidLogTime(message) from None


def config_text(text: str, edited_key: str) -> str:
    """Keep the original text except for the edited scalar assignment."""
    section, key = edited_key.split(".", 1)
    assignment = re.compile(rf"\s*(?:#\s*)?({re.escape(key)}\s*=.+)")
    table = ""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if header := TOML_TABLE.fullmatch(line.rstrip("\r\n")):
            table = header[1].strip()
        if table == section and (match := assignment.fullmatch(line.rstrip("\r\n"))):
            item = tomlkit.parse(match[1]).item(key)
            if not isinstance(item, Item):
                raise ValueError("Expected a scalar key in config.toml")
            lines[i] = f"<edited key>{item.trivia.comment}\n"
            break
    return "".join(lines)


# The log schema lists volatile fields in docs/log-events.json. Intent keys
# are also generated in parity cases and must join to the ledger and stdout.
LOG_VOLATILE_FIELDS: Mapping[str, str] = {
    "ts": "timestamp",
    "durationMs": "duration",
    "version": "version",
    "pid": "pid",
    "runId": "runId",
    "callId": "callId",
    "commandId": "commandId",
    "endpointId": "endpointId",
    "intentKey": "intentKey",
}


def timestamp(value: object) -> bool:
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        return datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def replace_if(
    value: dict[str, Any], key: str, predicate: Callable[[object], bool]
) -> None:
    if key in value and predicate(value[key]):
        value[key] = f"<{key}>"


def is_count(value: object) -> bool:
    return type(value) is int and value >= 0


def is_version(value: object) -> bool:
    return isinstance(value, str) and VERSION.fullmatch(value) is not None


class Normalize:
    def __init__(
        self,
        home: Path,
        log_fields: Mapping[str, str] = LOG_VOLATILE_FIELDS,
        *,
        concurrent: bool = False,
        started_at: float | None = None,
        ended_at: float | None = None,
    ):
        self.home = str(home)
        self.log_fields = log_fields
        self.concurrent = concurrent
        self.ids: dict[str, dict[int | str, str]] = {}
        self.started_at = started_at
        self.ended_at = ended_at

    def identity(self, kind: str, value: int | str) -> str:
        existing = self.ids.setdefault(kind, {})
        return existing.setdefault(value, f"<{kind} {len(existing) + 1}>")

    def text(self, value: str) -> str:
        return value.replace(self.home, "<HOME>")

    def intent_refs(self, value: str) -> str:
        return INTENT_ID.sub(lambda match: self.identity("intentKey", match[0]), value)

    def paths(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {
                key: self.intent_refs(item)
                if key == "intentKey"
                and isinstance(item, str)
                and INTENT_ID.fullmatch(item)
                else self.paths(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.paths(item) for item in value]
        return value

    def log_event(self, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TypeError("Expected a JSON log event object")
        stamp = parse_log_timestamp(value.get("ts"))
        if self.started_at is not None and self.ended_at is not None:
            # Sai truncates its event clock to milliseconds.
            first_ms = int(self.started_at * 1000) / 1000
            if not first_ms <= stamp.timestamp() <= self.ended_at:
                raise InvalidLogTime("Log timestamp is outside the invocation window")
        result = self.paths(value)
        predicates = {
            "duration": is_count,
            "version": is_version,
        }
        id_shapes = {
            "runId": r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
            "callId": r"[0-9a-fA-F]{12}",
            "commandId": r"[0-9a-f]{32}",
            "endpointId": r"h1_[0-9a-f]{64}",
            "intentKey": INTENT_ID,
        }
        for field, rule in self.log_fields.items():
            item = value.get(field)
            if rule == "timestamp" and field in value:
                if field != "ts":
                    parse_log_timestamp(item)
                result[field] = f"<{field}>"
            elif rule in predicates:
                replace_if(result, field, predicates[rule])
            elif (
                rule == "pid"
                and isinstance(item, int)
                and is_count(item)
                or (
                    rule in id_shapes
                    and isinstance(item, str)
                    and re.fullmatch(id_shapes[rule], item)
                )
            ):
                result[field] = self.identity(rule, item)
        if result.get("event") == "update.check":
            replace_if(result, "installed", is_version)
        if result.get("event") == "doctor.result":
            doctor_checks(result.get("checks"))
        if result.get("event") == "command.end" and isinstance(
            result.get("error"), str
        ):
            error = result["error"]
            # A command hint can be clipped by logging midway through a
            # fixture path, before the full home can be normalized.
            message, separator, omitted = error.rpartition("... [")
            if separator and re.fullmatch(r"[0-9]+ more characters\]", omitted):
                parent = f"{Path(self.home).parent}/"
                index = message.rfind(parent)
                if index >= 0 and self.home.startswith(message[index:]):
                    error = f"{message[:index]}<HOME>{separator}{omitted}"
            result["error"] = self.intent_refs(error)
        return result

    def log_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.concurrent:
            # The CLI processes can flush in either order. The successful
            # submit and the blocked contender each have one command.end.
            groups: dict[str, list[dict[str, Any]]] = {}
            for record in records:
                groups.setdefault(record.get("runId", ""), []).append(record)
            records = [
                record
                for group in sorted(
                    groups.values(),
                    key=lambda events: next(
                        (
                            event["exitCode"]
                            for event in events
                            if event.get("event") == "command.end"
                        ),
                        -1,
                    ),
                )
                for record in group
            ]
        return [self.log_event(record) for record in records]


def doctor_checks(checks: Any) -> None:
    if not isinstance(checks, list):
        return
    for check in checks:
        if not isinstance(check, dict):
            continue
        replace_if(check, "durationMs", is_count)
        name = check.get("id")
        detail = check.get("details")
        if name in {"install", "version"} and isinstance(detail, dict):
            for field in ("version", "installed", "running"):
                replace_if(detail, field, is_version)
        message = check.get("summary")
        if isinstance(message, str):
            if name in {"keeper", "websocket", "lcd"}:
                check["summary"] = DURATION.sub("<durationMs>", message)
            if name in {"install", "version"}:
                check["summary"] = re.sub(
                    rf"(?<=sai ){VERSION.pattern}(?=[ ;]|$)", "<version>", message
                )
            if name == "install":
                checkout = "sai <version> runs from a source checkout at "
                if check["summary"].startswith(checkout):
                    root = check["summary"].removeprefix(checkout)
                    if Path(root).is_absolute():
                        check["summary"] = checkout + "<checkout>"
                if isinstance(detail, dict) and isinstance(
                    provenance := detail.get("provenance"), dict
                ):
                    doctor_provenance(provenance)


def doctor_provenance(install: dict[str, Any]) -> None:
    if install.get("kind") != "source-checkout":
        return
    replace_if(
        install, "path", lambda path: isinstance(path, str) and Path(path).is_absolute()
    )
    if isinstance(git := install.get("git"), dict):
        replace_if(
            git,
            "commit",
            lambda sha: (
                isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) is not None
            ),
        )
        replace_if(git, "dirty", lambda dirty: type(dirty) is bool)


def doctor_report(value: dict[str, Any]) -> None:
    replace_if(value, "generatedAt", timestamp)
    doctor_checks(value.get("checks"))
    if isinstance(sai := value.get("sai"), dict):
        replace_if(sai, "version", is_version)
        if isinstance(install := sai.get("install"), dict):
            doctor_provenance(install)


def stdout(raw: str, mode: str, command: str, normalize: Normalize) -> Any:
    if raw == "":
        return ""
    if mode == "text":
        value = normalize.text(raw)
        if command == "--version":
            return re.sub(
                rf"(?<=version ){VERSION.pattern}(?=\n?$)", "<version>", value
            )
        return value
    if mode in {"jsonl", "mcp"}:
        records = [json.loads(line) for line in raw.splitlines()]
        if command == "logs tail":
            return [normalize.log_event(record) for record in records]
        if mode == "mcp":
            for record in records:
                result = record.get("result", {})
                info = result.get("serverInfo")
                if isinstance(info, dict):
                    replace_if(info, "version", is_version)
                if isinstance(result.get("tools"), list):
                    result["tools"].sort(key=lambda tool: tool["name"])
                for block in result.get("content", []):
                    if block.get("type") == "text":
                        try:
                            block["text"] = json.loads(block["text"])
                        except (json.JSONDecodeError, TypeError):
                            pass
            records.sort(key=lambda record: str(record.get("id", "")))
        return normalize.paths(records)
    value = normalize.paths(json.loads(raw))
    if (
        isinstance(value, dict)
        and isinstance(error := value.get("error"), dict)
        and "intentKey" in error
    ):
        for field in ("message", "hint"):
            if isinstance(error.get(field), str):
                error[field] = normalize.intent_refs(error[field])
    if command == "doctor" and isinstance(value, dict):
        if isinstance(error := value.get("error"), dict):
            if isinstance(report := error.get("details"), dict):
                doctor_report(report)
            if isinstance(error.get("message"), str):
                error["message"] = DURATION.sub("<durationMs>", error["message"])
        else:
            doctor_report(value)
    if (
        command.startswith("trade ")
        and isinstance(value, dict)
        and isinstance(estimates := value.get("estimates"), dict)
    ):
        for field, price in (
            ("fees", "collateralPriceUsd"),
            ("liquidationPrice", "entry"),
            ("worstCaseFill", "reference"),
        ):
            estimate = estimates.get(field)
            if isinstance(estimate, dict) and isinstance(
                source := estimate.get(price), dict
            ):
                replace_if(source, "observedAt", timestamp)
    if command == "skill source" and isinstance(value, dict):
        replace_if(value, "cliVersion", is_version)
    if (
        command == "logs path"
        and isinstance(value, dict)
        and isinstance(value.get("files"), list)
    ):
        # A UTC midnight split may add a second dated file for one surface.
        value["files"] = list(
            dict.fromkeys(
                f"{match['surface']}.jsonl"
                if isinstance(name, str)
                and (match := LOG_FILE.fullmatch(f"logs/{name}"))
                else name
                for name in value["files"]
            )
        )
    if command == "update" and isinstance(value, dict):
        replace_if(value, "installed", is_version)
    return value


def stderr_category(raw: str) -> tuple[str, ...]:
    categories: dict[str, None] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("Usage:"):
            category = "usage"
        elif line.startswith("Error:"):
            category = "error"
        elif line.startswith("Warning:"):
            category = "warning"
        elif line.startswith(("Traceback (", "panic:", "thread '")):
            category = "exception"
        elif line == "Connected; waiting for current snapshot":
            category = "connected"
        elif line.startswith("Disconnected ("):
            category = "disconnected"
        elif line.startswith("Try ") and "--help" in line:
            continue
        else:
            category = "diagnostic"
        categories[category] = None
    return tuple(categories)


@dataclass(frozen=True)
class Entry:
    kind: str
    mode: int
    data: Any


def paths(root: Path) -> Iterator[Path]:
    yield root
    if stat.S_ISDIR(root.lstat().st_mode):
        for child in sorted(root.iterdir()):
            yield from paths(child)


@cache
def sqlite_tokenizer() -> re.Pattern[str]:
    trivia = SQLITE_TRIVIA
    with closing(sqlite3.connect(":memory:")) as conn:
        try:
            conn.execute("SELECT \ufeff 1")
        except sqlite3.OperationalError:
            pass  # Older SQLite versions tokenize this BOM as an identifier.
        else:
            trivia += r"|\ufeff"
    return re.compile(rf"(?P<trivia>{trivia}){SQLITE_SYNTAX}", re.DOTALL)


def sqlite_identifier(token: str) -> str:
    quote = token[0]
    if quote == "[":
        token = token[1:-1]
    elif quote in {'"', "'", "`"}:
        token = token[1:-1].replace(quote * 2, quote)
    return token.translate(ASCII_UPPER)


def table_ddl(sql: str) -> tuple[list[list[str]], list[str], dict[str, str]]:
    """Read CHECK expressions, table options, and column collations from stored DDL."""
    # SQLite treats high-byte characters as identifier characters, except
    # when its tokenizer skips a BOM. Quoted tokens can contain parentheses.
    tokens = [
        token[0].translate(ASCII_UPPER) if token.lastgroup == "name" else token[0]
        for token in sqlite_tokenizer().finditer(sql)
        if token.lastgroup != "trivia"
    ]
    if tokens[:2] != ["CREATE", "TABLE"]:
        return [], [], {}
    opening = tokens.index("(")
    checks: list[list[str]] = []
    collations: dict[str, str] = {}
    column = None
    depth = 0
    check_start = None
    for i in range(opening, len(tokens)):
        token = tokens[i]
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
            if depth == 1 and check_start is not None:
                checks.append(tokens[check_start:i])
                check_start = None
            if depth == 0:
                return checks, tokens[i + 1 :], collations
        elif depth == 1:
            if tokens[i - 1] in {"(", ","}:
                column = (
                    None
                    if token in {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"}
                    else sqlite_identifier(token)
                )
            if tokens[i : i + 2] == ["CHECK", "("]:
                check_start = i + 2
            elif token == "COLLATE" and column is not None:
                collations[column] = sqlite_identifier(tokens[i + 1])
    raise ValueError("Unbalanced CREATE TABLE definition")


def sqlite_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    if sqlite3.sqlite_version_info < MIN_SQLITE_SCHEMA:
        raise ValueError(SQLITE_SCHEMA_REQUIREMENT)
    catalog = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    tables = {}
    for kind, name, _, sql in catalog:
        if kind not in {"table", "view"}:
            continue
        checks, options, collations = table_ddl(sql)
        # The PRAGMA reports parsed defaults, nullability, PK order, and
        # hidden or generated columns, including when the table is empty.
        columns = [
            {
                "cid": cid,
                "name": column,
                "type": datatype.upper() if datatype.isascii() else datatype,
                "notnull": notnull,
                "default": default,
                "pk": primary_key,
                "hidden": hidden,
                "collation": collations.get(column.translate(ASCII_UPPER), "BINARY"),
            }
            for cid, column, datatype, notnull, default, primary_key, hidden in conn.execute(
                "SELECT * FROM pragma_table_xinfo(?) ORDER BY cid", (name,)
            )
        ]
        indexes = {}
        for index, unique, origin, partial in conn.execute(
            'SELECT name, "unique", origin, partial FROM pragma_index_list(?) ORDER BY name',
            (name,),
        ):
            indexes[index] = {
                "unique": unique,
                "origin": origin,
                "partial": partial,
                "columns": [
                    (
                        seqno,
                        cid,
                        index_column,
                        descending,
                        "BINARY"
                        if collation is None
                        else collation.translate(ASCII_UPPER),
                        is_key,
                    )
                    for seqno, cid, index_column, descending, collation, is_key in conn.execute(
                        'SELECT seqno, cid, name, "desc", coll, "key" FROM pragma_index_xinfo(?) ORDER BY seqno',
                        (index,),
                    )
                ],
            }
        tables[name] = {
            "type": kind,
            "without_rowid": "ROWID" in options,
            "strict": "STRICT" in options,
            "columns": columns,
            "checks": checks,
            "indexes": indexes,
            "foreign_keys": conn.execute(
                "SELECT * FROM pragma_foreign_key_list(?) ORDER BY id, seq", (name,)
            ).fetchall(),
        }
    return {
        "tables": tables,
        "objects": [(kind, name, table) for kind, name, table, _ in catalog],
    }


def ledger(path: Path, normalize: Normalize) -> dict[str, Any]:
    # Read a copy so SQLite can recover or checkpoint a journal without
    # touching either CLI's captured tree.
    with tempfile.TemporaryDirectory(prefix="sai-parity-ledger-") as temp:
        copy = Path(temp) / path.name
        shutil.copyfile(path, copy)
        for suffix in SIDECARS:
            sidecar = path.with_name(path.name + suffix)
            if sidecar.is_file() and not sidecar.is_symlink():
                shutil.copyfile(sidecar, copy.with_name(copy.name + suffix))
        conn = sqlite3.connect(copy)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            schema = sqlite_schema(conn)
            result: dict[str, Any] = {
                "user_version": version,
                "schema": schema,
                "tables": {},
            }
            for kind, name, _ in schema["objects"]:
                if kind != "table":
                    continue
                quoted = name.replace('"', '""')
                snapshot = conn.execute(f'SELECT * FROM "{quoted}"')
                columns = [item[0] for item in snapshot.description]
                rows: list[tuple[dict[str, Any], dict[str, str]]] = []
                for values in snapshot:
                    row = normalize.paths(dict(zip(columns, values, strict=True)))
                    types = dict(
                        zip(
                            columns,
                            (type(value).__name__ for value in values),
                            strict=True,
                        )
                    )
                    if (
                        name == "intents"
                        and isinstance(row.get("id"), str)
                        and INTENT_ID.fullmatch(row["id"])
                    ):
                        row["id"] = normalize.intent_refs(row["id"])
                        created_at = row.get("created_at")
                        if (
                            type(created_at) is not float
                            or normalize.started_at is None
                            or not normalize.started_at <= created_at <= time.time()
                        ):
                            raise InvalidReservationTime(
                                "Ledger created_at must be a REAL Unix timestamp in seconds during the invocation"
                            )
                        row["created_at"] = "<created_at>"
                    rows.append((row, types))
                rows.sort(key=repr)
                result["tables"][name] = {
                    "columns": sorted(columns),
                    "rows": [row for row, _ in rows],
                    "row_types": [types for _, types in rows],
                }
            return result
        finally:
            conn.close()


def content(path: Path, name: str, normalize: Normalize) -> Any:
    if name == LEDGER:
        return ledger(path, normalize)
    if (
        name in {LEDGER + suffix for suffix in SIDECARS}
        and path.with_name("intents.sqlite3").is_file()
    ):
        return "<SQLite journal checked with its ledger>"
    if name == "config.toml":
        return normalize.paths(tomllib.loads(path.read_text(encoding="utf-8")))
    if name in {"trader/sessions.json", "trader/session.json"}:
        stored = json.loads(path.read_text(encoding="utf-8"))
        aad = storage._AAD if name.endswith("/sessions.json") else storage._V2_AAD
        payload = storage._decrypt(stored, aad)
        envelope = {
            **stored,
            "nonce": "<authenticated nonce>",
            "ciphertext": "<authenticated data>",
        }
        return normalize.paths({"envelope": envelope, "payload": payload})
    if name == "update.json":
        value = normalize.paths(json.loads(path.read_text(encoding="utf-8")))
        replace_if(value, "checkedAt", timestamp)
        return value
    if name == "logs/.endpoint-key":
        if path.stat().st_size != 32:
            raise ValueError("Expected a 32-byte log endpoint key")
        return "<generated endpoint key>"
    return path.read_bytes()


def read_log(path: Path, day: str) -> list[dict[str, Any]]:
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    for event in records:
        stamp = event.get("ts") if isinstance(event, dict) else None
        if parse_log_timestamp(stamp).date().isoformat() != day:
            raise ValueError("Log timestamp does not match its filename")
    return records


def snapshot(
    root: Path, normalize: Normalize, *, raw_config: bool = False
) -> dict[str, Entry]:
    if not root.exists() and not root.is_symlink():
        return {}
    entries: dict[str, Entry] = {}
    logs: dict[str, list[dict[str, Any]]] = {}
    log_modes: dict[str, int] = {}
    for path in paths(root):
        name = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            entry = Entry(
                "symlink", stat.S_IMODE(mode), normalize.text(str(path.readlink()))
            )
        elif stat.S_ISDIR(mode):
            entry = Entry("directory", stat.S_IMODE(mode), None)
        elif stat.S_ISREG(mode):
            try:
                if match := LOG_FILE.fullmatch(name):
                    # paths() visits each surface's dated files in date order.
                    stream = f"logs/{match['surface']}.jsonl"
                    records = logs.setdefault(stream, [])
                    if log_modes.setdefault(stream, stat.S_IMODE(mode)) != stat.S_IMODE(
                        mode
                    ):
                        raise ValueError("Log file modes differ within a surface")
                    records.extend(read_log(path, match["day"]))
                    continue
                data = (
                    path.read_bytes()
                    if raw_config and name == "config.toml"
                    else content(path, name, normalize)
                )
            except (InvalidReservationTime, InvalidLogTime):
                raise
            except (ValueError, KeyError, TypeError, sqlite3.Error, InvalidTag) as exc:
                # A decode failure must fail even when both sides produce it.
                raise ValueError(
                    f"Cannot decode {name} ({type(exc).__name__})"
                ) from None
            entry = Entry("file", stat.S_IMODE(mode), data)
        else:
            raise ValueError(f"Unsupported file type at {name}")
        entries[name] = entry
    for name, records in logs.items():
        if name in entries:
            raise ValueError(f"File collides with merged log stream at {name}")
        entries[name] = Entry("file", log_modes[name], normalize.log_records(records))
    return entries


def first_difference(left: Any, right: Any, path: str) -> str | None:
    if isinstance(left, bool) != isinstance(right, bool):
        return f"{path} type"
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            # Keys like connected account addresses can be sensitive too.
            return f"{path} keys"
        for key in sorted(left):
            if found := first_difference(left[key], right[key], f"{path}.{key}"):
                return found
        return None
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return f"{path} length"
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            if found := first_difference(a, b, f"{path}[{index}]"):
                return found
        return None
    return path if left != right else None
