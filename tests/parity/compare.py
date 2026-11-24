"""Compare CLI observations without emitting their potentially sensitive values."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import stat
import tempfile
import tomllib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from sai_cli.core import storage

VERSION = re.compile(r"v?\d+\.\d+\.\d+(?:[-+][\w.+-]+)?")
LOG_FILE = re.compile(r"logs/(?P<surface>[a-z]+)-(?P<day>\d{4}-\d{2}-\d{2})\.jsonl")
LEDGER = "trader/intents.sqlite3"
SIDECARS = ("-wal", "-shm", "-journal")
DURATION = re.compile(r"(?<= in )\d+(?= ms\b)")
INTENT_ID = re.compile(r"\b\d{8}T\d{9}Z_[0-9A-HJKMNP-TV-Z]{16}\b")

# Can be supplied to Normalize from docs/log-events.json once the registry
# defines its format. Values select a normalization rule, never a deletion.
LOG_VOLATILE_FIELDS: Mapping[str, str] = {
    "ts": "timestamp",
    "durationMs": "duration",
    "version": "version",
    "pid": "pid",
    "runId": "runId",
    "callId": "callId",
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
    ):
        self.home = str(home)
        self.log_fields = log_fields
        self.concurrent = concurrent
        self.ids: dict[str, dict[int | str, str]] = {}
        self.first_log_day: date | None = None

    def log_name(self, name: str) -> str:
        match = LOG_FILE.fullmatch(name)
        if match is None:
            return name
        day = date.fromisoformat(match["day"])
        if self.first_log_day is None:
            self.first_log_day = day
        offset = (day - self.first_log_day).days
        return f"logs/{match['surface']}-<day {offset}>.jsonl"

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
        result = self.paths(value)
        predicates = {
            "timestamp": timestamp,
            "duration": is_count,
            "version": is_version,
        }
        id_shapes = {
            "runId": r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
            "callId": r"[0-9a-fA-F]{12}",
            "intentKey": INTENT_ID,
        }
        for field, rule in self.log_fields.items():
            item = value.get(field)
            if rule in predicates:
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
            result["error"] = self.intent_refs(result["error"])
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
    if command == "doctor" and isinstance(value, dict):
        replace_if(value, "generatedAt", timestamp)
        doctor_checks(value.get("checks"))
    if command == "skill source" and isinstance(value, dict):
        replace_if(value, "cliVersion", is_version)
    if (
        command == "logs path"
        and isinstance(value, dict)
        and isinstance(value.get("files"), list)
    ):
        value["files"] = [
            normalize.log_name(f"logs/{name}").removeprefix("logs/")
            if isinstance(name, str)
            else name
            for name in value["files"]
        ]
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
            tables = conn.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
            )
            result: dict[str, Any] = {"user_version": version, "tables": {}}
            for (name,) in tables:
                quoted = name.replace('"', '""')
                snapshot = conn.execute(f'SELECT * FROM "{quoted}"')
                columns = [item[0] for item in snapshot.description]
                rows = []
                for values in snapshot:
                    row = normalize.paths(dict(zip(columns, values, strict=True)))
                    if (
                        name == "intents"
                        and isinstance(row.get("id"), str)
                        and INTENT_ID.fullmatch(row["id"])
                    ):
                        row["id"] = normalize.intent_refs(row["id"])
                        if type(row.get("created_at")) in (float, int):
                            row["created_at"] = "<created_at>"
                    rows.append(row)
                result["tables"][name] = {
                    "columns": sorted(columns),
                    "rows": sorted(rows, key=repr),
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
    if match := LOG_FILE.fullmatch(name):
        records = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]
        for event in records:
            stamp = event.get("ts") if isinstance(event, dict) else None
            if (
                not isinstance(stamp, str)
                or not timestamp(stamp)
                or not stamp.startswith(match["day"])
            ):
                raise ValueError("Log timestamp does not match its filename")
        return normalize.log_records(records)
    return path.read_bytes()


def snapshot(root: Path, normalize: Normalize) -> dict[str, Entry]:
    if not root.exists() and not root.is_symlink():
        return {}
    entries = {}
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
                data = content(path, name, normalize)
            except (ValueError, KeyError, TypeError, sqlite3.Error, InvalidTag) as exc:
                # A decode failure must fail even when both sides produce it.
                raise ValueError(
                    f"Cannot decode {name} ({type(exc).__name__})"
                ) from None
            entry = Entry("file", stat.S_IMODE(mode), data)
        else:
            raise ValueError(f"Unsupported file type at {name}")
        entries[normalize.log_name(name)] = entry
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
