"""Compare the Python sai CLI with SAI_PARITY_BIN, or with itself."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import queue
import re
import select
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import click
from compare import (
    LEDGER,
    MIN_SQLITE_SCHEMA,
    SQLITE_SCHEMA_REQUIREMENT,
    Entry,
    InvalidLogTime,
    Normalize,
    config_text,
    first_difference,
    ledger,
    mask_overview_stdout,
    rewrite_mcp_fields,
    snapshot,
    stderr_category,
    stdout,
)
from mcp_cases import CALLS, DEFERRED_TRADE_PROBE, handshake
from mcp_clients import SECRET, client_state, seed_clients
from network import enter_loopback, restricted, unroutable_packets
from reference_uv import resolve_uv
from sai_cli.sai_cli import command_path
from sai_cli.sai_cli import main as python_cli
from scenarios import (
    HERE,
    ROOT,
    Endpoints,
    child_env,
    loopback,
    seed_home,
    session_fixture,
    write_seed,
)


@dataclass(frozen=True)
class Case:
    name: str
    args: list[str]
    expect: int = 0
    mode: str = "json"
    mcp_profile: str = ""
    mcp_protocol: str = "2025-11-25"
    mcp_logs: bool = False
    mcp_deferred_trade_exception: bool = False
    mcp_checkout_wire: list[str] | None = None
    seed: str = "empty"
    server: str = "normal"
    setup: list[str] | None = None
    unset: list[str] | None = None
    wire: list[str] | None = None
    state: dict[str, Any] | None = None
    client_seed: str | None = None
    accepted_divergence: str | None = None
    absent_log_text: list[str] | None = None
    interrupt_after: int | None = None
    interrupt_on_request: bool = False
    overview_expected: dict[str, Any] | None = None
    exact_output: bool = False

    @property
    def command(self) -> str:
        return " ".join(command_path(python_cli, self.args)) or self.args[0]


@dataclass(frozen=True)
class Observation:
    exit_code: int
    output: Any
    stderr: tuple[str, ...]
    files: dict[str, Entry]
    raw: tuple[bytes, bytes] | None = None


def invoke(
    binary: list[str],
    args: list[str],
    home: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        restricted([*binary, *args]),
        cwd=home,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        umask=0o022,
        check=False,
    )
    check_sandbox_exit(result.returncode)
    return subprocess.CompletedProcess(
        result.args, result.returncode, result.stdout.decode(), result.stderr.decode()
    )


def check_sandbox_exit(exit_code: int) -> None:
    if exit_code in {-signal.SIGSYS, 128 + signal.SIGSYS}:
        raise ValueError("attempted a blocked host socket")


def interrupt_command(
    binary: list[str],
    args: list[str],
    home: Path,
    env: dict[str, str],
    *,
    snapshots: int = 0,
    ready: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    """Interrupt after a fixture sees the request or after the requested snapshots."""
    with subprocess.Popen(
        restricted([*binary, *args]),
        cwd=home,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        umask=0o022,
    ) as proc:
        assert proc.stdout is not None
        deadline = time.monotonic() + 12
        seen = bytearray()
        received = 0
        try:
            if ready is not None and not ready.wait(8):
                raise ValueError(
                    "Timed out waiting for the interrupted command's request"
                )
            while received < snapshots:
                remaining = max(0, deadline - time.monotonic())
                if not select.select([proc.stdout], [], [], remaining)[0]:
                    raise ValueError("Timed out waiting for a watch snapshot")
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise ValueError("Watch exited before the expected snapshots")
                seen.extend(chunk)
                received += chunk.count(b"\n")
            for line in seen.split(b"\n")[:snapshots]:
                if "data" not in json.loads(line):
                    raise ValueError("Watch exited before the expected snapshots")
            os.killpg(proc.pid, signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=4)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate(timeout=4)
        check_sandbox_exit(proc.returncode)
        return subprocess.CompletedProcess(
            args, proc.returncode, (seen + stdout).decode(), stderr.decode()
        )


def mcp_exchange(
    binary: list[str],
    args: list[str],
    home: Path,
    env: dict[str, str],
    endpoints: Endpoints,
    case: Case,
    *,
    probe_deferred_trade: bool,
) -> subprocess.CompletedProcess[str]:
    # Keep stdin open until the server replies. Closing it while a request is
    # still running can cancel the MCP server's pending work.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        with subprocess.Popen(
            restricted([*binary, *args]),
            cwd=home,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            start_new_session=True,
            umask=0o022,
        ) as proc:
            assert proc.stdin is not None and proc.stdout is not None
            lines: queue.Queue[bytes | None] = queue.Queue()
            stream = proc.stdout

            def read() -> None:
                try:
                    for line in stream:
                        lines.put(line)
                finally:
                    lines.put(None)

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            received: list[str] = []
            deadline = time.monotonic() + 20
            try:
                requests = [
                    (request, None, False) for request in handshake(case.mcp_protocol)
                ]
                calls = CALLS[case.mcp_profile]
                if probe_deferred_trade:
                    requests.append(
                        (
                            DEFERRED_TRADE_PROBE.request(3 + len(calls), endpoints),
                            None,
                            True,
                        )
                    )
                requests.extend(
                    (call.request(id_, endpoints), call.session, False)
                    for id_, call in enumerate(calls, 3)
                )
                for request, session, probe in requests:
                    if session is not None:
                        write_seed(
                            home, endpoints, [session_fixture(endpoints, session)]
                        )
                    wire = json.dumps({"jsonrpc": "2.0", **request}) + "\n"
                    proc.stdin.write(wire.encode())
                    proc.stdin.flush()
                    if "id" not in request:
                        continue
                    raw = lines.get(timeout=max(0, deadline - time.monotonic()))
                    if raw is None:
                        check_sandbox_exit(
                            proc.wait(timeout=max(0, deadline - time.monotonic()))
                        )
                        raise ValueError("MCP server closed stdout before replying")
                    if not raw.endswith(b"\n"):
                        raise ValueError("MCP stdout frame is missing its newline")
                    line = raw.decode("utf-8")
                    message = json.loads(line)
                    if (
                        not isinstance(message, dict)
                        or message.get("jsonrpc") != "2.0"
                        or message.get("id") != request["id"]
                        or not isinstance(message.get("result"), dict)
                    ):
                        label = request.get("params", {}).get("name", request["method"])
                        raise ValueError(f"Unexpected MCP frame for {label}")
                    if probe:
                        check_deferred_trade_probe(message["result"])
                    else:
                        received.append(line)
                proc.stdin.close()
                if lines.get(timeout=max(0, deadline - time.monotonic())) is not None:
                    raise ValueError("Unexpected MCP stdout after the last response")
                code = proc.wait(timeout=max(0, deadline - time.monotonic()))
                check_sandbox_exit(code)
            except queue.Empty:
                raise ValueError("MCP exchange timed out") from None
            except (UnicodeError, json.JSONDecodeError):
                raise ValueError("Invalid MCP stdout") from None
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                reader.join(timeout=2)
        stderr.seek(0)
        return subprocess.CompletedProcess(
            [*binary, *args], code, "".join(received), stderr.read()
        )


def check_deferred_trade_probe(result: dict[str, Any]) -> None:
    blocks = result.get("content")
    if (
        result.get("isError") is False
        and isinstance(blocks, list)
        and len(blocks) == 1
        and blocks[0].get("type") == "text"
        and isinstance(text := blocks[0].get("text"), str)
        and result.get("structuredContent") == {"result": text}
        and isinstance(value := json.loads(text), dict)
        and value.get("code") == DEFERRED_TRADE_PROBE.expected
    ):
        return
    raise ValueError(
        "Temporary trading availability exception expired. Remove it once trade_open is implemented"
    )


def check_state(state: dict[str, Any], files: dict[str, Entry]) -> None:
    # A filename and optional dotted field path are separated with '#'.
    for selector, expected in state.items():
        name, _, keys = selector.partition("#")
        absent = object()
        actual: Any = absent
        if entry := files.get(name):
            if keys == "@mode":
                if entry.mode == expected:
                    continue
                raise ValueError(f"Persisted mode differs from expectation at {name}")
            actual = entry.data if keys else entry.kind
        for key in keys.split(".") if keys else ():
            if isinstance(actual, list) and key.isdecimal() and int(key) < len(actual):
                actual = actual[int(key)]
            else:
                actual = actual.get(key, absent) if isinstance(actual, dict) else absent
        mismatch = (
            actual is not absent
            if expected == "<absent>"
            else first_difference(actual, expected, selector)
        )
        if mismatch:
            raise ValueError(f"Persisted state differs from expectation at {selector}")


def check_overview_fields(fields: dict[str, Any], output: Any) -> None:
    absent = object()
    for selector, expected in fields.items():
        value = output
        for key in selector.split("."):
            if isinstance(value, dict):
                value = value.get(key, absent)
            elif isinstance(value, list) and key == "@len":
                value = len(value)
            elif isinstance(value, list) and key.isdecimal() and int(key) < len(value):
                value = value[int(key)]
            else:
                value = absent
        if first_difference(
            value, None if expected == "<null>" else expected, selector
        ):
            raise ValueError(
                f"Overview field {selector} differs from the fixture expectation"
            )


def observe(
    case: Case,
    binary: list[str],
    home: Path,
    endpoints: Endpoints,
    *,
    candidate: bool = False,
) -> Observation:
    started_at = time.time()
    seed_home(home, endpoints, case.seed)
    config = home / "sai" / "config.toml"
    before_config = config.read_bytes().decode("utf-8") if config.is_file() else None
    env = child_env(home, endpoints)
    client_before = (
        seed_clients(home, env, case.client_seed) if case.client_seed else {}
    )
    for key in case.unset or ():
        env.pop(key, None)
    if case.setup:
        setup = invoke(binary, [endpoints.expand(arg) for arg in case.setup], home, env)
        if setup.returncode:
            raise ValueError(f"Setup exited {setup.returncode}")
    args = [endpoints.expand(arg) for arg in case.args]
    contender = None
    concurrent = case.server == "concurrent-close"
    if concurrent:
        admission = endpoints.admission
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(invoke, binary, args, home, env)
            try:
                if not admission.arrived.wait(10):
                    raise ValueError("First process did not reach synthetic admission")
                before = Normalize(home, started_at=started_at)
                expected_id = before.identity("intentKey", admission.key)
                before_ack = ledger(home / "sai" / LEDGER, before)
                rows = before_ack["tables"]["intents"]["rows"]
                if (
                    len(rows) != 1
                    or rows[0]["status"] != "unknown"
                    or rows[0]["id"] != expected_id
                    or first.done()
                ):
                    raise ValueError(
                        "First process did not persist one unresolved reservation"
                    )
                contender = invoke(binary, args, home, env)
                if contender.returncode != 1:
                    raise ValueError(
                        f"Expected contender exit 1, got {contender.returncode}"
                    )
            finally:
                admission.release.set()
            result = first.result()
    elif case.interrupt_on_request:
        try:
            result = interrupt_command(
                binary, args, home, env, ready=endpoints.interrupt_ready
            )
            if case.server not in {
                "interrupt-http",
                "overview-interrupt-http",
            } and not endpoints.interrupt_closed.wait(4):
                raise ValueError("WebSocket did not close after SIGINT")
        finally:
            endpoints.interrupt_closed.set()
    elif case.interrupt_after is not None:
        result = interrupt_command(
            binary, args, home, env, snapshots=case.interrupt_after
        )
    elif case.mode == "mcp":
        if not case.mcp_logs:
            env["SAI_LOG"] = "0"
        result = mcp_exchange(
            binary,
            args,
            home,
            env,
            endpoints,
            case,
            probe_deferred_trade=candidate and case.mcp_deferred_trade_exception,
        )
    else:
        result = invoke(binary, args, home, env)
    ended_at = time.time()
    if endpoints.errors:
        raise ValueError(endpoints.errors[0])
    actual, expected = [request.label for request in endpoints.wire], case.wire or []
    if case.command == "doctor":
        actual, expected = sorted(actual), sorted(expected)
    if actual != expected:
        raise ValueError(f"Expected wire {expected}, got {actual}")
    if before_config is not None:
        if config.is_symlink() or not config.is_file():
            raise ValueError("The existing config.toml was replaced or removed")
        after_config = config.read_bytes().decode("utf-8")
        if case.command in {"config set", "config unset"}:
            key = case.args[-2] if case.command == "config set" else case.args[-1]
            before_config = config_text(before_config, key)
            after_config = config_text(after_config, key)
        if before_config != after_config:
            raise ValueError("config.toml changed text outside the edited value")
    normalize = Normalize(
        home, concurrent=concurrent, started_at=started_at, ended_at=ended_at
    )
    if case.absent_log_text:
        for path in (home / "sai" / "logs").glob("*.jsonl"):
            text = path.read_text(encoding="utf-8")
            if any(value in text for value in case.absent_log_text):
                raise ValueError("Fixture credential appeared in a log")
    files = snapshot(
        home / "sai", normalize, raw_config=case.seed == "doctor-malformed-toml"
    )
    if case.client_seed:
        if SECRET in result.stdout or SECRET in result.stderr:
            raise ValueError("Client config value escaped into CLI output")
        files.update(client_state(home, client_before, normalize))
    state = dict(case.state or {})
    if candidate and case.accepted_divergence == "codex-repair-rollback":
        state["clients/.codex/config.toml#changed"] = False
    check_state(state, files)
    display = result.stdout
    if case.command == "account overview" and case.mode == "text":
        display = mask_overview_stdout(display, case.mode)
    try:
        output = stdout(display, case.mode, case.command, normalize)
        if contender:
            output = {
                "first": output,
                "contender": stdout(
                    contender.stdout, case.mode, case.command, normalize
                ),
            }
    except InvalidLogTime:
        raise
    except (ValueError, AttributeError, KeyError, TypeError):
        raise ValueError(
            f"Invalid {case.mode} stdout (exit {result.returncode})"
        ) from None
    if case.overview_expected:
        check_overview_fields(case.overview_expected, output)
    if case.mode == "mcp":
        check_mcp_calls(output, case.mcp_profile)
    stderr = stderr_category(result.stderr)
    if contender:
        stderr += tuple(
            f"contender {category}" for category in stderr_category(contender.stderr)
        )
    exact = case.exact_output or case.command.split()[0] in {
        "market",
        "account",
        "watch",
    }
    if case.command == "account overview" and case.mode == "json":
        display = mask_overview_stdout(display, case.mode)
    raw = (display.encode(), result.stderr.encode()) if exact else None
    return Observation(result.returncode, output, stderr, files, raw)


def mcp_tools(output: Any) -> dict[str, dict[str, Any]]:
    try:
        replies = {reply["id"]: reply["result"] for reply in output if "id" in reply}
        if not replies[1]["protocolVersion"]:
            raise ValueError("MCP initialize lacked a protocol version")
        tools = replies[2]["tools"]
        inventory = {tool["name"]: tool for tool in tools}
        if not inventory or len(inventory) != len(tools):
            raise ValueError("MCP tools/list gave an empty or duplicate inventory")
        return inventory
    except (KeyError, TypeError):
        raise ValueError(
            "MCP response did not include initialize and tools/list"
        ) from None


def check_mcp_calls(output: Any, profile: str) -> None:
    mcp_tools(output)
    replies = {reply["id"]: reply["result"] for reply in output}
    for id_, call in enumerate(CALLS[profile], 3):
        payload = replies[id_]
        blocks = payload.get("content")
        if (
            not isinstance(blocks, list)
            or len(blocks) != 1
            or blocks[0]["type"] != "text"
        ):
            raise ValueError(f"{call.tool} did not return one MCP text block")
        text = blocks[0]["text"]
        invalid = call.expected == "invalid"
        if payload.get("isError") is not invalid:
            raise ValueError(f"{call.tool} returned the wrong MCP isError")
        if invalid:
            if not isinstance(text, str) or "structuredContent" in payload:
                raise ValueError(f"{call.tool} did not return an MCP validation error")
        elif not isinstance(text, dict) or text.get("code") != (
            None if call.expected == "ok" else call.expected
        ):
            raise ValueError(f"{call.tool} expected MCP result {call.expected}")


def differences(reference: Observation, candidate: Observation) -> list[str]:
    diffs = []
    if reference.exit_code != candidate.exit_code:
        diffs.append(f"exit {reference.exit_code} to {candidate.exit_code}")
    if reference.stderr != candidate.stderr:
        a = ", ".join(reference.stderr) or "empty"
        b = ", ".join(candidate.stderr) or "empty"
        diffs.append(f"stderr {a} to {b}")
    if found := first_difference(reference.output, candidate.output, "stdout"):
        diffs.append(found)
    if reference.raw is not None and candidate.raw is not None:
        for stream, expected, actual in zip(
            ("stdout", "stderr"), reference.raw, candidate.raw, strict=True
        ):
            if expected != actual:
                diffs.append(f"{stream} bytes differ")
    missing = sorted(reference.files.keys() - candidate.files.keys())
    extra = sorted(candidate.files.keys() - reference.files.keys())
    diffs.extend(f"missing {name}" for name in missing)
    diffs.extend(f"extra {name}" for name in extra)
    for name in sorted(reference.files.keys() & candidate.files.keys()):
        a, b = reference.files[name], candidate.files[name]
        if a.kind != b.kind:
            diffs.append(f"{name} file type")
        if a.mode != b.mode:
            diffs.append(f"{name} mode {a.mode:04o} to {b.mode:04o}")
        if a.kind == b.kind and (found := first_difference(a.data, b.data, name)):
            diffs.append(found)
    return diffs


def safe_observe(
    case: Case,
    binary: list[str],
    home: Path,
    endpoints: Endpoints,
    *,
    candidate: bool = False,
) -> tuple[Observation | None, str]:
    endpoints.errors.clear()
    endpoints.interrupt_ready.clear()
    endpoints.interrupt_closed.clear()
    endpoints.wire.clear()
    endpoints.admission.arrived.clear()
    endpoints.admission.release.clear()
    endpoints.admission.key = ""
    outbound = unroutable_packets()
    try:
        result: tuple[Observation | None, str] = (
            observe(case, binary, home, endpoints, candidate=candidate),
            "",
        )
    except subprocess.TimeoutExpired:
        result = (None, "timed out")
    except subprocess.CalledProcessError as exc:
        result = (None, f"fixture setup exited {exc.returncode}")
    except ValueError as exc:
        result = (None, str(exc))
    except (OSError, sqlite3.Error, AttributeError, KeyError, TypeError) as exc:
        result = (None, type(exc).__name__)
    if unroutable_packets() != outbound:
        return None, "attempted a non-loopback network connection"
    return result


def expect_native_mcp_info(
    output: list[dict[str, Any]], *, allow_deferred_trade: bool
) -> str | None:
    reasons: dict[str, None] = {}
    for frame in output:
        result = frame["result"]
        value = result.get("structuredContent", {}).get("result")
        if not isinstance(value, dict) or value.get("name") != "sai":
            continue
        updates = {}
        lcd = urlsplit(value["endpoints"]["lcd"])
        # All replay URLs contain a per-run path. The explicit privacy case
        # appends a synthetic credential to that path.
        if re.fullmatch(r"/parity/[0-9a-f]{32}(?:/fixture-lcd-private)?", lcd.path):
            updates["endpoints"] = {
                **value["endpoints"],
                "lcd": f"{lcd.scheme}://{lcd.netloc}",
            }
            reasons["Native MCP reports the LCD origin without the private path"] = None
        trading = value["trading"]
        if (
            allow_deferred_trade
            and trading["available"] is True
            and trading["reason"] == "signed-in"
        ):
            updates["trading"] = {
                **trading,
                "available": False,
                "reason": "not-implemented",
            }
            reasons[
                "Temporary native trading deferral until trade_open is implemented"
            ] = None
        value.update(updates)
        result["content"][0]["text"].update(updates)
        result["_toolText"] = rewrite_mcp_fields(result["_toolText"], updates)
    return ", ".join(reasons) or None


def expect_native_mcp_validation(
    output: list[dict[str, Any]], profile: str
) -> str | None:
    """Project only the Python reference's declared validation errors."""
    validation_detail = re.compile(
        r"^([^\n]+)\n(  [^\n]+? \[type=(\w+)), input_value=[^\n]*, input_type=\w+\]$",
        re.MULTILINE,
    )

    def strip_input(match: re.Match[str]) -> str:
        field, detail, category = match.groups()
        return detail + "]" if category == "extra_forbidden" else f"{field}\n{detail}]"

    replies = {frame["id"]: frame["result"] for frame in output}
    changed = False
    for id_, call in enumerate(CALLS[profile], 3):
        if call.expected == "invalid":
            block = replies[id_]["content"][0]
            expected = validation_detail.sub(strip_input, block["text"])
            changed |= expected != block["text"]
            block["text"] = expected
    return "Native MCP validation omits rejected input" if changed else None


def project_reference_lcd_origin(output: dict[str, Any]) -> None:
    """The pinned Python reference still displays the LCD URL path."""
    error = output.get("error")
    report = error["details"] if error is not None else output
    lcd = next(check for check in report["checks"] if check["id"] == "lcd")
    endpoint = lcd["details"]["endpoint"]
    parts = urlsplit(endpoint)
    origin = f"{parts.scheme}://{parts.netloc}"
    original_summary = lcd["summary"]
    lcd["details"]["endpoint"] = origin
    lcd["summary"] = original_summary.replace(endpoint, origin, 1)
    if error is not None:
        error["message"] = error["message"].replace(
            f"lcd: {original_summary}", f"lcd: {lcd['summary']}", 1
        )


def run_case(
    case: Case, reference: list[str], candidate: list[str]
) -> tuple[list[str], dict[str, str], str | None]:
    with (
        tempfile.TemporaryDirectory(prefix="sai-parity-") as temp,
        loopback(case.server) as endpoints,
    ):
        left, left_error = safe_observe(
            case, reference, Path(temp) / "reference", endpoints
        )
        candidate_case = (
            replace(case, wire=case.mcp_checkout_wire)
            if candidate != reference and case.mcp_checkout_wire is not None
            else case
        )
        right, right_error = safe_observe(
            candidate_case,
            candidate,
            Path(temp) / "candidate",
            endpoints,
            candidate=candidate != reference,
        )
    tools = {}
    if case.mode == "mcp" and left is not None:
        expected = mcp_tools(left.output)
        actual = mcp_tools(right.output) if right is not None else {}
        for name in sorted(expected.keys() | actual.keys()):
            if name not in expected:
                tools[name] = "EXTRA"
            elif name not in actual:
                tools[name] = "MISSING"
            else:
                tools[name] = (
                    "DIFF"
                    if first_difference(expected[name], actual[name], "tool")
                    else "LISTED"
                )
    if left is None or right is None:
        return (
            [
                f"{side} {error}"
                for side, error in (
                    ("reference", left_error),
                    ("candidate", right_error),
                )
                if error
            ],
            tools,
            None,
        )
    if candidate != reference and case.accepted_divergence == "codex-repair-rollback":
        # Both observations checked their expected change flag. Compare the
        # saved file's existence and mode while allowing its contents to differ.
        filename = "clients/.codex/config.toml"
        for observation in (left, right):
            observation.files[filename] = replace(
                observation.files[filename], data=None
            )
    if candidate != reference and case.accepted_divergence == "lcd-origin-only":
        project_reference_lcd_origin(left.output)
    expected = None
    if case.mode == "mcp" and reference != candidate:
        expected = expect_native_mcp_info(
            left.output,
            allow_deferred_trade=case.mcp_deferred_trade_exception,
        )
        validation = expect_native_mcp_validation(left.output, case.mcp_profile)
        expected = (
            ", ".join(reason for reason in (expected, validation) if reason) or None
        )
    diffs = differences(left, right)
    if left.exit_code != case.expect:
        diffs.insert(0, f"expected reference exit {case.expect}, got {left.exit_code}")
    if (case.mode in {"mcp", "jsonl"} or case.expect == 0) and left.output == "":
        diffs.insert(0, "expected reference JSON output")
    if case.mode == "mcp":
        for call in CALLS[case.mcp_profile]:
            if call.expected == "ok" and not diffs:
                tools[call.tool] = "PASS"
    return diffs, tools, expected


def cli_commands(group: click.Group, prefix: str = "") -> set[str]:
    result = set()
    for name, command in group.commands.items():
        full = f"{prefix} {name}".strip()
        if isinstance(command, click.Group):
            result.update(cli_commands(command, full))
        else:
            result.add(full)
    return result


def row(command: str, status: str, detail: str = "") -> None:
    escaped = [
        item.replace("|", "\\|").replace("\n", " ")
        for item in (command, status, detail)
    ]
    print("| " + " | ".join(escaped) + " |", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", action="append", help="Run matching case IDs (glob, repeatable)"
    )
    parser.add_argument(
        "--python-reference",
        type=Path,
        metavar="SAI",
        help="Use the sai entry point of an installed Python reference",
    )
    args = parser.parse_args()
    if sqlite3.sqlite_version_info < MIN_SQLITE_SCHEMA:
        parser.error(SQLITE_SCHEMA_REQUIREMENT)
    try:
        enter_loopback()
        uv = resolve_uv()
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    with (HERE / "cases.toml").open("rb") as file:
        configured = tomllib.load(file)
    cases = [Case(**item) for item in configured["case"]]
    deferred: dict[str, str] = configured.get("deferred", {})
    missing = cli_commands(python_cli) - (
        {case.command for case in cases} | deferred.keys()
    )
    if missing:
        parser.error(
            f"Add cases or mark these commands deferred: {', '.join(sorted(missing))}"
        )
    selected = [
        case
        for case in cases
        if not args.case
        or any(fnmatch.fnmatchcase(case.name, pattern) for pattern in args.case)
    ]
    if not selected:
        parser.error("No parity cases matched")
    reference = [uv, "run", "--project", str(ROOT), "--no-sync", "--offline", "sai"]
    if args.python_reference is not None:
        executable = shutil.which(str(args.python_reference.absolute()))
        if executable is None:
            parser.error("--python-reference must name an executable file")
        reference = [executable]
    candidate = reference
    c9a_python = os.environ.get("SAI_C9A_PYTHON") or str(
        Path(sys.executable).with_name("sai")
    )
    if "SAI_PARITY_BIN" in os.environ:
        executable = shutil.which(os.environ["SAI_PARITY_BIN"])
        if not executable:
            parser.error("SAI_PARITY_BIN must name an executable file or command")
        candidate = [str(Path(executable).absolute())]
    mcp_reference = reference
    if any(case.mode == "mcp" for case in selected):
        if override := os.getenv("SAI_MCP_PARITY_PYTHON"):
            executable = shutil.which(override)
            if not executable:
                parser.error("SAI_MCP_PARITY_PYTHON must name an executable Python sai")
            mcp_reference = [str(Path(executable).absolute())]
        elif candidate is not reference:
            parser.error("Set SAI_MCP_PARITY_PYTHON to sai from the pinned C8 checkout")
    print("# Sai CLI Parity\n")
    print(
        "Python vs Python (self-check).\n"
        if candidate is reference
        else "Python vs candidate.\n"
    )
    print("| Command | Status | Diff summary |\n| --- | --- | --- |", flush=True)
    mismatches = 0
    expected_differences = 0
    tools: dict[str, str] = {}
    overview_reference = os.environ.get("SAI_PARITY_OVERVIEW_PYTHON")
    for case in selected:
        if overview_reference and (
            case.name == "account-bare" or case.command == "account overview"
        ):
            oracle = [str(Path(overview_reference).absolute())]
        elif case.mode == "mcp":
            oracle = mcp_reference
        elif case.client_seed or case.command in {"doctor", "mcp install", "mcp list"}:
            # Launch directly so uv cannot shadow the fixture launcher on PATH.
            oracle = [c9a_python]
        else:
            oracle = reference
        if (
            candidate is reference
            and oracle is reference
            and case.mcp_checkout_wire is not None
        ):
            case = replace(case, wire=case.mcp_checkout_wire)
        executable = oracle if candidate is reference else candidate
        diffs, listed, expected = run_case(case, oracle, executable)
        for tool, status in listed.items():
            if status != "LISTED" or tool not in tools:
                tools[tool] = status
        mismatches += bool(diffs)
        expected_differences += bool(expected and not diffs)
        row(
            f"`sai {shlex.join(case.args)}` ({case.name})",
            "DIFF" if diffs else "EXPECTED DIFF" if expected else "PASS",
            ", ".join(diffs)
            if diffs
            else expected
            or (
                f"Accepted divergence ({case.accepted_divergence})"
                if candidate is not reference and case.accepted_divergence
                else ""
            ),
        )
    if not args.case:
        for name, reason in sorted(deferred.items()):
            row(f"`sai {name}`", "DEFERRED", reason)
    if tools:
        print("\n| MCP tool | Status | Diff summary |\n| --- | --- | --- |")
        for name, status in tools.items():
            row(f"`{name}`", status, "tools/call" if status == "PASS" else "tools/list")
    passed = len(selected) - mismatches - expected_differences
    print(f"\n{passed}/{len(selected)} cases passed.")
    if expected_differences:
        print(f"Expected differences: {expected_differences}.")
    return int(bool(mismatches))


if __name__ == "__main__":
    sys.exit(main())
