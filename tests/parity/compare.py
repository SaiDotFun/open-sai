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
# Native MCP changes from #267. Map the native market_id schema and docs to
# the Python spelling so the comparison still checks everything else.
NATIVE_MCP_SPELLINGS = (
    (
        (
            '"market_id":{"anyOf":[{"type":"integer"},{"type":"string"}],'
            '"title":"Market Id"}'
        ),
        '"market_id":{"title":"Market Id","type":"integer"}',
    ),
    (
        (
            r"market_id: Market ID from market_list, or a market symbol such as\n"
            r"            \"ETH\". A symbol that matches more than one market is refused."
        ),
        "market_id: Market id from market_list.",
    ),
)
LOG_SCHEMA = Path(__file__).resolve().parents[2] / "docs" / "log-events.json"
LEDGER = "trader/intents.sqlite3"
MIN_SQLITE_SCHEMA = (3, 26, 0)
SQLITE_SCHEMA_REQUIREMENT = (
    "Ledger parity requires SQLite 3.26.0 or newer for table_xinfo"
)
SIDECARS = ("-wal", "-shm", "-journal")
DURATION = re.compile(r"(?<= in )\d+(?= ms\b)")
DOCTOR_GENERATED_AT = re.compile(r'("generatedAt":)"([^"\\]+)"')
DOCTOR_DURATION_MS = re.compile(r'(?<="durationMs":)(?:0|[1-9]\d*)(?=[,}])')
DOCTOR_BUILD_VERSION = re.compile(rf'("(?:version|installed)":"){VERSION.pattern}(?=")')
DOCTOR_DISPLAY_VERSION = re.compile(rf"(?<=sai ){VERSION.pattern}(?=[ ;]|$)")
DOCTOR_CHECKOUT_GIT = re.compile(
    r'("git":\{"commit":")[0-9a-f]{40}(","dirty":)(?:true|false)(?=\})'
)
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
SQLITE_OPERAND_PREFIXES = {
    "(",
    ",",
    "+",
    "-",
    "*",
    "/",
    "%",
    "~",
    "&",
    "|",
    "<<",
    ">>",
    "||",
    "->",
    "->>",
    "=",
    "==",
    "!=",
    "<>",
    "<",
    "<=",
    ">",
    ">=",
    "ALL",
    "AND",
    "BETWEEN",
    "BY",
    "DISTINCT",
    "ELSE",
    "ESCAPE",
    "GLOB",
    "HAVING",
    "IS",
    "LIKE",
    "LIMIT",
    "MATCH",
    "NOT",
    "OFFSET",
    "ON",
    "OR",
    "REGEXP",
    "SELECT",
    "THEN",
    "WHEN",
    "WHERE",
}
SQLITE_NUMERIC_START = re.compile(r"[0-9]|\.[0-9]")


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


# Each rule validates the field before masking its value or mapping its ID.
LOG_FIELD_RULES = {
    "ts": "timestamp",
    "durationMs": "duration",
    "version": "version",
    "pid": "pid",
    "runId": "runId",
    "callId": "callId",
    "commandId": "commandId",
    "endpointId": "endpointId",
}


@cache
def load_log_field_rules(schema: Path) -> Mapping[str, str]:
    fields = json.loads(schema.read_text(encoding="utf-8"))["volatileFields"]
    if not isinstance(fields, list) or any(not isinstance(f, str) for f in fields):
        raise ValueError(f"Expected a list of volatile field names in {schema}")
    try:
        return {field: LOG_FIELD_RULES[field] for field in fields}
    except KeyError as exc:
        raise ValueError(f"Missing log normalization rule for {exc.args[0]}") from exc


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
        *,
        log_schema: Path = LOG_SCHEMA,
        concurrent: bool = False,
        started_at: float | None = None,
        ended_at: float | None = None,
        fixture_values: Mapping[str, str] | None = None,
    ):
        self.home = str(home)
        self.log_fields = load_log_field_rules(log_schema)
        self.concurrent = concurrent
        self.ids: dict[str, dict[int | str, str]] = {}
        self.started_at = started_at
        self.ended_at = ended_at
        self.fixture_values = fixture_values or {}

    def identity(self, kind: str, value: int | str) -> str:
        existing = self.ids.setdefault(kind, {})
        return existing.setdefault(value, f"<{kind} {len(existing) + 1}>")

    def text(self, value: str) -> str:
        value = value.replace(self.home, "<HOME>")
        for fixture, label in self.fixture_values.items():
            value = re.sub(re.escape(fixture) + r"(?!\d)", label, value)
        return value

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
        }
        for field, rule in self.log_fields.items():
            item = value.get(field)
            if rule == "timestamp":
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
            replace_if(result, "installKind", development_install)
        if result.get("event") == "doctor.result":
            doctor_checks(result.get("checks"))
        if (
            result.get("event") == "command.start"
            and isinstance(argv := result.get("argv"), list)
            and all(isinstance(arg, str) for arg in argv)
        ):
            # Python records names only. Pinned releases and Rust retain values.
            result["argv"] = "<argv>"
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


def mask_doctor_stream(raw: str, *, checkout: bool = False) -> str:
    """Mask clock and build values without changing serialization or layout."""
    raw = DOCTOR_GENERATED_AT.sub(
        lambda match: f'{match[1]}"<generatedAt>"' if timestamp(match[2]) else match[0],
        raw,
    )
    raw = DOCTOR_DURATION_MS.sub('"<durationMs>"', raw)
    raw = DOCTOR_BUILD_VERSION.sub(r"\g<1><version>", raw)
    raw = DOCTOR_DISPLAY_VERSION.sub("<version>", raw)
    if checkout:
        raw = DOCTOR_CHECKOUT_GIT.sub(r'\g<1><commit>\g<2>"<dirty>"', raw)
    return DURATION.sub("<durationMs>", raw)


def rewrite_mcp_fields(text: str, updates: dict[str, Any]) -> str:
    """Replace selected top-level JSON values while retaining all other bytes."""
    if not updates:
        return text
    decoder = json.JSONDecoder()
    whitespace = re.compile(r"\s*")
    pos = whitespace.match(text).end() + 1
    previous = 0
    chunks = []
    while (pos := whitespace.match(text, pos).end()) < len(text) and text[pos] != "}":
        name, end = decoder.raw_decode(text, pos)
        colon = whitespace.match(text, end).end()
        start = whitespace.match(text, colon + 1).end()
        _, pos = decoder.raw_decode(text, start)
        if name in updates:
            chunks.extend((text[previous:start], json.dumps(updates[name])))
            previous = pos
        pos = whitespace.match(text, pos).end()
        if text[pos] == "}":
            break
        pos += 1
    chunks.append(text[previous:])
    return "".join(chunks)


def normalize_mcp_result(result: dict[str, Any]) -> None:
    info = result.get("serverInfo")
    if isinstance(info, dict):
        replace_if(info, "version", is_version)
    for block in result.get("content", []):
        if block.get("type") != "text":
            continue
        text = block["text"]
        if not isinstance(text, str):
            raise TypeError("MCP text content must be a string")
        if result.get("isError"):
            continue
        if result.get("structuredContent") != {"result": text}:
            raise ValueError("MCP structuredContent must carry the same JSON string")
        value = json.loads(text)
        updates = {}
        if isinstance(value, dict) and value.get("name") == "sai":
            for version in ("version", "installedVersion", "mcpVersion"):
                if is_version(value.get(version)):
                    updates[version] = f"<{version}>"
            # Each implementation reports its own installation or checkout.
            # Validate the shape before substituting a common path.
            provenance = value["provenance"]
            kind = provenance.get("kind")
            path = provenance.get("path")
            if (
                kind not in {"installed", "source-checkout"}
                or not isinstance(path, str)
                or not Path(path).is_absolute()
            ):
                raise ValueError("Invalid MCP server provenance")
            keys = {"kind", "path"}
            if kind == "source-checkout":
                keys.add("git")
                git = provenance.get("git")
                if git is not None and (
                    set(git) != {"commit", "dirty"}
                    or not re.fullmatch(r"[0-9a-f]{40,64}", git["commit"])
                    or type(git["dirty"]) is not bool
                ):
                    raise ValueError("Invalid MCP server Git provenance")
            if set(provenance) != keys:
                raise ValueError("Unexpected MCP server provenance fields")
            updates["provenance"] = "<provenance>"
            value.update(updates)
        result["_toolText"] = rewrite_mcp_fields(text, updates)
        block["text"] = value
        result["structuredContent"]["result"] = value


OVERVIEW_AGE = re.compile(r"(?<=\()-?[\d.,]+(?:e[+-]\d+)?(?= s\))")
OVERVIEW_TABLE = re.compile(
    r"(?m)^  [^\n]* │ [^\n]*\n  ─+\n(?:  [^\n]* │ [^\n]*\n(?!  ─+\n))+"
)


def normalize_overview_table(match: re.Match[str]) -> str:
    """Reflow only columns that contain a rendered source age."""
    header, divider, *body = match[0].splitlines()
    rows = [line[2:].split(" │ ") for line in (header, *body)]
    widths = [len(cell) for cell in rows[0]]
    # Keep malformed layout visible to the byte comparison.
    if len(divider) != 2 + sum(widths) + 3 * (len(widths) - 1) or any(
        [len(cell) for cell in row] != widths for row in rows
    ):
        return match[0]
    for column, cells in enumerate(zip(*rows, strict=True)):
        if any(OVERVIEW_AGE.search(cell) for cell in cells):
            masked = [
                OVERVIEW_AGE.sub("<sourceAgeSeconds>", cell.rstrip()) for cell in cells
            ]
            widths[column] = max(map(len, masked))
            for row, cell in zip(rows, masked, strict=True):
                row[column] = cell.ljust(widths[column])
    lines = [" │ ".join(row) for row in rows]
    lines.insert(1, "───".join("─" * width for width in widths))
    return "".join(f"  {line}\n" for line in lines)


def project_historical_overview_balances(raw: str, mode: str) -> str:
    """Render the native balance fields using the pinned Python overview format."""
    if mode == "json":
        value = json.loads(raw)
        rows = value.get("balances", {}).get("data")
        if rows is None:
            return raw
        for row in rows.values():
            if row.pop("sourceAtBasis", None) == "fetchedAt":
                row["sourceAt"] = None
            row.pop("stale")
        return json.dumps(value) + "\n"

    def project_table(match: re.Match[str]) -> str:
        header, _, *body = match[0].splitlines()
        if not header.startswith("  Funds "):
            return match[0]
        rows = [[cell.strip() for cell in line[2:].split(" │ ")] for line in body]
        if any(len(row) != 3 or not row[2].endswith(" (age unknown)") for row in rows):
            return match[0]
        for row in rows:
            source = row[2].removesuffix(" (age unknown)")
            row[2] = (
                "unknown" if source == "block unknown" else f"{source} (time unknown)"
            )
        headings = [cell.strip() for cell in header[2:].split(" │ ")]
        widths = [max(map(len, column)) for column in zip(headings, *rows, strict=True)]

        def render(row: list[str]) -> str:
            return " │ ".join(
                cell.ljust(width) for cell, width in zip(row, widths, strict=True)
            )

        divider = "───".join("─" * width for width in widths)
        return "".join(
            f"  {line}\n" for line in (render(headings), divider, *map(render, rows))
        )

    return OVERVIEW_TABLE.sub(project_table, raw)


def mask_overview_stdout(raw: str, mode: str) -> str:
    """Mask clock values in place so serialization and table layout stay comparable."""
    if mode == "json":
        fetch_pattern = r'(?<!\\)("fetchedAt"\s*:\s*)"([^"]+)"'
        fetched = {stamp for _, stamp in re.findall(fetch_pattern, raw)}
        raw = re.sub(fetch_pattern, r'\1"<fetchedAt>"', raw)
        raw = re.sub(
            r'(?<!\\)("sourceAt"\s*:\s*)"([^"]+)"',
            lambda match: (
                f'{match[1]}"<fetchedAt>"' if match[2] in fetched else match[0]
            ),
            raw,
        )
        return re.sub(
            r'(?<!\\)("sourceAgeSeconds"\s*:\s*)-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?',
            r'\1"<sourceAgeSeconds>"',
            raw,
        )
    raw = re.sub(
        r"(?i)(fetched at )\d{4}-\d\d-\d\dT[\d:.]+\+00:00",
        r"\1<fetchedAt>",
        raw,
    )
    raw = OVERVIEW_TABLE.sub(normalize_overview_table, raw)
    return OVERVIEW_AGE.sub("<sourceAgeSeconds>", raw)


def overview_clocks(
    value: Any, normalize: Normalize, fetched: datetime | None = None
) -> None:
    """Check age against the matching fetch before normalizing either value."""
    if isinstance(value, list):
        for item in value:
            overview_clocks(item, normalize, fetched)
        return
    if not isinstance(value, dict):
        return
    if "fetchedAt" in value:
        stamp = value["fetchedAt"]
        if stamp is None:
            fetched = None
        else:
            if not timestamp(stamp):
                raise ValueError("Invalid overview fetch timestamp")
            fetched = datetime.fromisoformat(stamp)
            if (
                normalize.started_at is None
                or normalize.ended_at is None
                or not normalize.started_at <= fetched.timestamp() <= normalize.ended_at
            ):
                raise ValueError("Overview fetch timestamp is outside the invocation")
            value["fetchedAt"] = "<fetchedAt>"
            if "sourceAt" in value and "sourceAgeSeconds" not in value:
                if value["sourceAt"] != stamp:
                    raise ValueError(
                        "Overview section source time differs from its fetch"
                    )
                value["sourceAt"] = "<fetchedAt>"
    if "sourceAgeSeconds" in value:
        source, age = value["sourceAt"], value["sourceAgeSeconds"]
        if value.get("sourceAtBasis") == "fetchedAt":
            if (
                age is not None
                or fetched is None
                or not timestamp(source)
                or datetime.fromisoformat(source) != fetched
            ):
                raise ValueError("Invalid overview fetch-time source")
            value["sourceAt"] = "<fetchedAt>"
        elif source is None:
            if age is not None:
                raise ValueError("Overview reported an age without a source timestamp")
        else:
            if not timestamp(source) or type(age) is not float or fetched is None:
                raise ValueError("Invalid overview source timestamp or age")
            elapsed = (fetched - datetime.fromisoformat(source)).total_seconds()
            if not abs(age - elapsed) <= 0.00051:
                raise ValueError(
                    "Overview age disagrees with its fetch and source times"
                )
            value["sourceAgeSeconds"] = "<sourceAgeSeconds>"
    for item in value.values():
        overview_clocks(item, normalize, fetched)


def trade_estimate_clocks(value: Any) -> None:
    if not isinstance(value, dict) or not isinstance(
        estimates := value.get("estimates"), dict
    ):
        return
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
            for native, python in NATIVE_MCP_SPELLINGS:
                raw = raw.replace(native, python)
            records = [json.loads(line) for line in raw.splitlines()]
            for frame, record in zip(raw.splitlines(), records, strict=True):
                result = record.get("result", {})
                if "tools" in result:
                    result["_schemaWire"] = frame
                normalize_mcp_result(result)
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
    if command == "account overview":
        overview_clocks(value, normalize)
    if command == "doctor" and isinstance(value, dict):
        if isinstance(error := value.get("error"), dict):
            if isinstance(report := error.get("details"), dict):
                doctor_report(report)
            if isinstance(error.get("message"), str):
                error["message"] = DURATION.sub("<durationMs>", error["message"])
        else:
            doctor_report(value)
    if command.startswith("trade "):
        trade_estimate_clocks(value)
    if command == "skill source" and isinstance(value, dict):
        replace_if(value, "cliVersion", is_version)
        if isinstance(path := value.get("path"), str) and (
            Path(path).is_absolute() or path.startswith("<HOME>/")
        ):
            value["path"] = "<package-location>"
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
        install = value.get("install")
        if (
            isinstance(install, dict)
            and development_install(install.get("kind"))
            and isinstance(install.get("prefix"), str)
            and Path(install["prefix"]).is_absolute()
        ):
            kind, url = install["kind"], install.get("url")
            install.update({"kind": "<development install>", "prefix": "<prefix>"})
            if (
                kind == "checkout"
                and isinstance(url, str)
                and url.startswith("file:///")
                or kind == "other"
                and "url" in install
                and url is None
            ):
                install["url"] = "<development origin>"
            update = value.get("update")
            if isinstance(update, dict) and update.get("note") in {
                "This is a source checkout; pull, then run `uv sync`.",
                "This launcher is managed outside sai. Use its installer to update.",
            }:
                update["note"] = "<development install guidance>"
    return value


def development_install(kind: object) -> bool:
    # The Python checkout and the unregistered Rust build have different owners.
    return kind in ("checkout", "other")


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


def sqlite_tokens(sql: str) -> list[str]:
    # SQLite treats high-byte characters as identifier characters, except
    # when its tokenizer skips a BOM. Quoted tokens can contain parentheses.
    return [
        token[0].translate(ASCII_UPPER) if token.lastgroup == "name" else token[0]
        for token in sqlite_tokenizer().finditer(sql)
        if token.lastgroup != "trivia"
    ]


def normalize_collations(tokens: list[str]) -> list[str]:
    normalized = tokens.copy()
    for i, token in enumerate(tokens[:-1]):
        if token == "COLLATE":
            normalized[i + 1] = sqlite_identifier(tokens[i + 1])
    return normalized


def parenthesis_pairs(tokens: list[str]) -> Iterator[tuple[int, int]]:
    openings = []
    for i, token in enumerate(tokens):
        if token == "(":
            openings.append(i)
        elif token == ")":
            yield openings.pop(), i


def strip_outer_parentheses(tokens: list[str]) -> list[str]:
    while (
        tokens[:1] == ["("]
        and (0, len(tokens) - 1) in parenthesis_pairs(tokens)
        and tokens[1] not in {"SELECT", "VALUES", "WITH"}
    ):
        tokens = tokens[1:-1]
    return tokens


def view_expressions(tokens: list[str]) -> list[str]:
    """Strip outer groups at view expression boundaries, including in subqueries."""
    starts = {
        ",",
        "ALL",
        "BY",
        "DISTINCT",
        "HAVING",
        "LIMIT",
        "OFFSET",
        "ON",
        "SELECT",
        "WHERE",
    }
    ends = {
        ")",
        ",",
        "AS",
        "ASC",
        "DESC",
        "EXCEPT",
        "FROM",
        "GROUP",
        "HAVING",
        "INTERSECT",
        "JOIN",
        "LIMIT",
        "NULLS",
        "OFFSET",
        "ORDER",
        "UNION",
        "WHERE",
        "WINDOW",
    }
    removed = set()
    for start, end in parenthesis_pairs(tokens):
        if start == 0 or tokens[start - 1] not in starts:
            continue
        following = tokens[end + 1 : end + 3]
        if following and following[0] not in ends:
            alias = following[0]
            if not (
                sqlite_name_token(alias)
                and (len(following) == 1 or following[1] in ends)
            ):
                continue
        expression = tokens[start : end + 1]
        pairs = (len(expression) - len(strip_outer_parentheses(expression))) // 2
        removed.update(range(start, start + pairs))
        removed.update(range(end - pairs + 1, end + 1))
    return [token for i, token in enumerate(tokens) if i not in removed]


def unquoted_sqlite_name(token: str) -> bool:
    match = sqlite_tokenizer().fullmatch(token)
    return match is not None and match.lastgroup == "name"


def sqlite_name_token(token: str) -> bool:
    return token[0] in {'"', "`", "["} or unquoted_sqlite_name(token)


def strip_operand_parentheses(tokens: list[str]) -> list[str]:
    """Try ungrouping individual operands within larger SQL expressions."""
    pairs = dict(parenthesis_pairs(tokens))
    removed = set()
    for opening, close_index in pairs.items():
        # Function argument lists and IN lists have structural parentheses.
        if opening and tokens[opening - 1] not in SQLITE_OPERAND_PREFIXES:
            continue
        start, end = opening + 1, close_index - 1
        while start in removed and end in removed:
            start += 1
            end -= 1
        if start > end:
            continue
        first = tokens[start]
        if start == end:
            operand = (
                sqlite_name_token(first)
                or first.startswith(("'", "x'", "X'"))
                or SQLITE_NUMERIC_START.match(first) is not None
            )
        elif start + 1 == end and first in {"+", "-"}:
            operand = SQLITE_NUMERIC_START.match(tokens[end]) is not None
        elif pairs.get(start + 1) == end:
            operand = sqlite_name_token(first)
        else:
            parts = tokens[start : end + 1]
            operand = (
                len(parts) in {3, 5}
                and all(token == "." for token in parts[1::2])
                and all(sqlite_name_token(token) for token in parts[::2])
            )
        if operand:
            removed.update((opening, close_index))
    return [token for i, token in enumerate(tokens) if i not in removed]


def normalize_expression_sql(
    conn: sqlite3.Connection,
    original: list[str],
    ungrouped: list[str],
    as_select: Callable[[str], str],
) -> list[str]:
    """Use SQLite's name resolution to check edits to expression syntax."""

    def program(tokens: list[str]) -> list[tuple] | None:
        try:
            return conn.execute("EXPLAIN " + as_select(" ".join(tokens))).fetchall()
        except sqlite3.DatabaseError:
            return None

    # SQLite can treat an unresolved double-quoted token as a string, and an
    # unquoted name can be a keyword. Only accept rewrites with the same program.
    # Collation names are identifiers by grammar and are normalized separately.
    def quoted_names(tokens: list[str]) -> list[int]:
        return [
            i
            for i, token in enumerate(tokens)
            if token[0] in {'"', "`", "["} and (i == 0 or tokens[i - 1] != "COLLATE")
        ]

    operands = strip_operand_parentheses(ungrouped)
    if operands == original and not quoted_names(original):
        return normalize_collations(original)
    reference = program(original)
    if reference is None:
        return normalize_collations(original)
    normalized = next(
        candidate.copy()
        for candidate in (operands, ungrouped, original)
        if candidate == original or program(candidate) == reference
    )
    for i in quoted_names(normalized):
        token = normalized[i]
        name = sqlite_identifier(token)
        # Keep a canonical quoted spelling for names that require quoting.
        quoted_name = "`" + name.replace("`", "``") + "`"
        candidates = (
            (name, quoted_name) if unquoted_sqlite_name(name) else (quoted_name,)
        )
        for candidate in candidates:
            normalized[i] = candidate
            if candidate == token or program(normalized) == reference:
                break
        else:
            normalized[i] = token
    return normalize_collations(normalized)


def table_ddl(sql: str) -> tuple[list[list[str]], list[str], dict[str, str]]:
    """Read CHECK expressions, table options, and column collations from stored DDL."""
    tokens = sqlite_tokens(sql)
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


def index_ddl(sql: str) -> tuple[list[list[str]], list[str]]:
    """Read index keys and the partial-index predicate from stored DDL."""
    tokens = sqlite_tokens(sql)
    start = tokens.index("(") + 1
    keys = []
    depth = 0
    for i in range(start, len(tokens)):
        token = tokens[i]
        if depth == 0 and token in {",", ")"}:
            keys.append(tokens[start:i])
            start = i + 1
            if token == ")":
                where = tokens[i + 2 :] if tokens[i + 1 : i + 2] == ["WHERE"] else []
                return keys, where
        elif token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
    raise ValueError("Unbalanced CREATE INDEX definition")


def index_expressions(
    conn: sqlite3.Connection,
    table: str,
    keys: list[list[str]],
    where: list[str],
    index_columns: list[tuple],
) -> tuple[dict[int, list[str]], list[str]]:
    source = '"' + table.replace('"', '""') + '" NOT INDEXED'
    expressions = {}
    for seqno, key in enumerate(keys):
        # PRAGMA reports the effective collation and uses cid -2 for expressions.
        if index_columns[seqno][1] != -2 and key.count("COLLATE") <= 1:
            continue
        end = -1 if key[-1] in {"ASC", "DESC"} else len(key)
        ungrouped = strip_outer_parentheses(key[:end]) + key[end:]
        # In a SELECT projection, SQLite reads a trailing ASC or DESC as the
        # result alias. An integer key remains a literal in this context.
        expressions[seqno] = normalize_expression_sql(
            conn, key, ungrouped, lambda expr: f"SELECT {expr} FROM {source}"
        )
    predicate = normalize_expression_sql(
        conn,
        where,
        strip_outer_parentheses(where),
        lambda expr: f"SELECT {expr} FROM {source}",
    )
    return expressions, predicate


def sqlite_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    if sqlite3.sqlite_version_info < MIN_SQLITE_SCHEMA:
        raise ValueError(SQLITE_SCHEMA_REQUIREMENT)
    catalog = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    index_sql = {name: sql for kind, name, _, sql in catalog if kind == "index" and sql}
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
            keys, where = (
                index_ddl(index_sql[index]) if index in index_sql else ([], [])
            )
            index_columns = conn.execute(
                'SELECT seqno, cid, name, "desc", coll, "key" FROM pragma_index_xinfo(?) ORDER BY seqno',
                (index,),
            ).fetchall()
            expressions, where = index_expressions(
                conn, name, keys, where, index_columns
            )
            indexes[index] = {
                "unique": unique,
                "origin": origin,
                "partial": partial,
                "where": where,
                "expressions": expressions,
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
                    for seqno, cid, index_column, descending, collation, is_key in index_columns
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
        if kind == "view":
            tokens = sqlite_tokens(sql)
            query_start = tokens.index("AS") + 1
            body = tokens[query_start:]
            tables[name]["definition"] = normalize_expression_sql(
                conn, body, view_expressions(body), str
            )
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
        validate_log_endpoint_key(path)
        return "<generated endpoint key>"
    return path.read_bytes()


def validate_log_endpoint_key(path: Path) -> None:
    if path.stat().st_size != 32:
        raise ValueError("Expected a 32-byte log endpoint key")


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
