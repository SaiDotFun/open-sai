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
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from case_inventory import cli_commands, require_manifest_case_ids
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
    mask_doctor_stream,
    mask_overview_stdout,
    rewrite_mcp_fields,
    snapshot,
    stderr_category,
    stdout,
    trade_estimate_clocks,
)
from manifest_gaps import ManifestGaps
from mcp_cases import CALLS, RELEASE_MCP_TOOL_NAMES, handshake
from mcp_clients import SECRET, client_state, seed_clients
from network import enter_loopback, restricted, unroutable_packets
from reference_uv import resolve_uv
from release_mismatch import differing_paths, value_fingerprint
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

C2_CONFIG_CASE_IDS = {
    "config-get-default",
    "config-get-env",
    "config-get-file",
    "config-get-text",
    "config-invalid-value",
    "config-set",
    "config-set-existing",
    "config-unknown-key",
    "config-unset",
}


@dataclass(frozen=True)
class Case:
    name: str
    args: list[str]
    expect: int = 0
    mode: str = "json"
    mcp_profile: str = ""
    mcp_protocol: str = "2025-11-25"
    mcp_logs: bool = False
    mcp_trade: bool = False
    checkout_reference: bool = False
    seed: str = "empty"
    server: str = "normal"
    setup: list[str] | None = None
    unset: list[str] | None = None
    wire: list[str] | None = None
    state: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    retry_expect: int | None = None
    mcp_calls: list[dict[str, Any]] | None = None
    rust: dict[str, Any] | None = None
    rust_early_position_reads: int = 0
    accepted_difference: str | None = None
    client_seed: str | None = None
    accepted_divergence: str | None = None
    absent_log_text: list[str] | None = None
    absent_output_text: list[str] | None = None
    interrupt_after: int | None = None
    interrupt_on_request: bool = False
    overview_expected: dict[str, Any] | None = None
    exact_output: bool = False
    current_only: bool = False
    reference_stderr_contains: list[str] | None = None
    collateral_price_after: str | None = None

    @property
    def command(self) -> str:
        return " ".join(command_path(python_cli, self.args)) or self.args[0]

    def rust_case(self) -> Case:
        wire = list(self.wire or [])
        if self.position_case:
            # Rust checks the position owner before its first LCD request.
            for index in range(len(wire) - 2):
                if wire[index : index + 3] == ["oneClickTrading", "node_info", "trade"]:
                    wire[index + 1 : index + 3] = ["trade", "node_info"]
        return replace(
            self,
            **{"wire": wire if self.wire is not None else None, **(self.rust or {})},
        )

    @property
    def position_case(self) -> bool:
        return self.name.startswith(("trade-margin", "trade-partial-close"))


@dataclass(frozen=True)
class Observation:
    exit_code: int
    output: Any
    stderr: tuple[str, ...]
    files: dict[str, Entry]
    raw: tuple[bytes, bytes] | None = None
    checks: tuple[str, ...] = ()


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
                    (request, None, None) for request in handshake(case.mcp_protocol)
                ]
                if case.mcp_calls is None:
                    calls = CALLS[case.mcp_profile]
                    requests.extend(
                        (
                            call.request(id_, endpoints),
                            call.session,
                            call.switch_active,
                        )
                        for id_, call in enumerate(calls, 3)
                    )
                else:
                    # Position cases assert their tool results directly.
                    requests.pop()  # tools/list
                    requests.extend(
                        (
                            {"id": id_, "method": "tools/call", "params": call},
                            None,
                            None,
                        )
                        for id_, call in enumerate(case.mcp_calls, 2)
                    )
                for request, session, switch_active in requests:
                    if session is not None:
                        write_seed(
                            home, endpoints, [session_fixture(endpoints, session)]
                        )
                    if switch_active is not None:
                        result = invoke(
                            binary,
                            ["auth", "use", endpoints.expand(switch_active)],
                            home,
                            env,
                        )
                        if result.returncode != 0:
                            raise ValueError("External fixture account switch failed")
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


ABSENT = object()


def field_matches(actual: Any, keys: str, expected: Any) -> bool:
    for key in keys.split(".") if keys else ():
        if isinstance(actual, list) and key.isdecimal() and int(key) < len(actual):
            actual = actual[int(key)]
        else:
            actual = actual.get(key, ABSENT) if isinstance(actual, dict) else ABSENT
    if expected == "<absent>":
        return actual is ABSENT
    return first_difference(actual, expected, keys) is None


def state_differences(state: dict[str, Any], files: dict[str, Entry]) -> list[str]:
    # A filename and optional dotted field path are separated with '#'.
    differences = []
    for selector, expected in state.items():
        name, _, keys = selector.partition("#")
        actual: Any = ABSENT
        if entry := files.get(name):
            if keys == "@mode":
                if entry.mode == expected:
                    continue
                differences.append(f"Persisted mode differs from expectation at {name}")
                continue
            actual = entry.data if keys else entry.kind
        if not field_matches(actual, keys, expected):
            differences.append(
                f"Persisted state differs from expectation at {selector}"
            )
    return differences


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
    cutover: bool = False,
) -> Observation:
    checks: list[str] = []

    def check_failed(message: str) -> None:
        if not cutover:
            raise ValueError(message)
        checks.append(message)

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
        )
    else:
        result = invoke(binary, args, home, env)
        if case.retry_expect is not None:
            contender = invoke(binary, args, home, env)
            if contender.returncode != case.retry_expect:
                raise ValueError(
                    f"Expected retry exit {case.retry_expect}, got {contender.returncode}"
                )
    ended_at = time.time()
    if candidate and any(
        marker in result.stdout or marker in result.stderr
        for marker in case.absent_output_text or ()
    ):
        raise ValueError("Fixture credential appeared in CLI output")
    if not candidate and any(
        marker not in result.stderr for marker in case.reference_stderr_contains or ()
    ):
        check_failed("Reference stderr is missing required help text")
    for error in endpoints.errors:
        check_failed(error)
    actual, expected = [request.label for request in endpoints.wire], case.wire or []
    if case.mcp_trade and case.wire is None:
        actual = [label for label in actual if label == "recordUserIntent"]
    if case.command == "doctor":
        actual, expected = sorted(actual), sorted(expected)
    if actual != expected:
        check_failed(f"Expected wire {expected}, got {actual}")
    if before_config is not None:
        if config.is_symlink() or not config.is_file():
            raise ValueError("The existing config.toml was replaced or removed")
        after_config = config.read_bytes().decode("utf-8")
        if case.command in {"config set", "config unset"}:
            key = case.args[-2] if case.command == "config set" else case.args[-1]
            before_config = config_text(before_config, key)
            after_config = config_text(after_config, key)
        if before_config != after_config:
            check_failed("config.toml changed text outside the edited value")
    fixture_values = None
    if cutover:
        fixture_values = {
            urlsplit(endpoints.http).netloc: "<fixture-http>",
            urlsplit(endpoints.ws).netloc: "<fixture-ws>",
            urlsplit(endpoints.http).path: "<fixture-route>",
            endpoints.near_expiry: "<fixture-near-expiry>",
            endpoints.overview_expiry: "<fixture-overview-expiry>",
        }
    if fixture_values is not None and case.command == "doctor":
        # Doctor reports both its own checkout and the launcher found on PATH.
        # Match the known fixture paths before the enclosing checkout path.
        fixture_values[str(Path(sys.executable).with_name("sai"))] = "<fixture-sai>"
        checkout = Path(binary[0]).parent.parent.parent
        if (checkout / ".git").exists():
            fixture_values[str(checkout)] = "<checkout>"
        fixture_values[str(ROOT)] = "<checkout>"
    normalize = Normalize(
        home,
        concurrent=concurrent,
        started_at=started_at,
        ended_at=ended_at,
        fixture_values=fixture_values,
    )
    admission_key = endpoints.admission.key
    expected_id = (
        normalize.identity("intentKey", admission_key) if admission_key else None
    )
    if case.absent_log_text:
        for path in (home / "sai" / "logs").glob("*.jsonl"):
            text = path.read_text(encoding="utf-8")
            if any(value in text for value in case.absent_log_text):
                if candidate or not cutover:
                    raise ValueError("Fixture credential appeared in a log")
                # The immutable release may contain a marker from this
                # synthetic fixture. Compare its fingerprint in the snapshot.
                check_failed("Published reference logs a synthetic fixture marker")
    files = snapshot(
        home / "sai", normalize, raw_config=case.seed == "doctor-malformed-toml"
    )
    if case.client_seed:
        if SECRET in result.stdout or SECRET in result.stderr:
            raise ValueError("Client config value escaped into CLI output")
        files.update(client_state(home, client_before, normalize))
    state = dict(case.state or {})
    if candidate and case.accepted_divergence == "codex-uninstall-rollback":
        state["clients/.codex/config.toml#changed"] = False
        state["clients/.codex/config.toml#data.settings.mcp_servers.sai.args"] = [
            "mcp",
            "serve",
        ]
        del state["clients/.codex/config.toml#data.settings.mcp_servers.sai"]
    if case.accepted_divergence == "uninstall-read-only":
        filename, entry = read_only_uninstall_path(case)
        state[f"{filename}#changed"] = not candidate
        if candidate:
            state[f"{filename}#{entry}.args"] = ["mcp", "serve"]
        else:
            state[f"{filename}#{entry}"] = "<absent>"
    if expected_id is not None:
        state[f"{LEDGER}#tables.intents.rows.0.id"] = expected_id
    for mismatch in state_differences(state, files):
        check_failed(mismatch)
    display = result.stdout
    if case.accepted_divergence == "overview-lcd-risk-error" and not candidate:
        # The pinned Python CLI records the failed LCD reads on each row.
        # Rust also reports the failure on the positions section.
        reference_overview = json.loads(display)
        reference_overview["positions"]["error"] = {
            "code": "INCOMPLETE_DATA",
            "message": "LCD position read failed. Position risk is partial.",
            "retryable": False,
        }
        display = json.dumps(reference_overview) + "\n"
    if case.command == "account overview" and case.mode == "text":
        display = mask_overview_stdout(display, case.mode)
    if case.command == "doctor" and case.exact_output and case.mode == "text":
        display = mask_doctor_stream(display)
    try:
        output = stdout(display, case.mode, case.command, normalize)
        if contender:
            output = {
                "first": output,
                "contender" if concurrent else "retry": stdout(
                    contender.stdout, case.mode, case.command, normalize
                ),
            }
    except InvalidLogTime:
        raise
    except (ValueError, AttributeError, KeyError, TypeError):
        raise ValueError(
            f"Invalid {case.mode} stdout (exit {result.returncode})"
        ) from None
    for keys, expected in (case.output or {}).items():
        if not field_matches(output, keys, expected):
            check_failed(f"Stdout differs from expectation at {keys}")
    if case.overview_expected:
        try:
            check_overview_fields(case.overview_expected, output)
        except ValueError as exc:
            check_failed(str(exc))
    if case.mode == "mcp" and case.mcp_calls is None:
        mcp_tools(output)
        try:
            check_mcp_calls(output, case.mcp_profile)
        except ValueError as exc:
            check_failed(str(exc))
    if case.mcp_trade:
        for frame in output[1:]:
            result_fields = frame["result"]
            # stdout() already checked that both MCP representations carry
            # the same text. Compare the JSON members independent of order.
            result_fields.pop("_toolText", None)
            for payload in (
                result_fields["content"][0]["text"],
                result_fields.get("structuredContent", {}).get("result"),
            ):
                trade_estimate_clocks(payload)
                if isinstance(payload, dict):
                    trade_estimate_clocks(payload.get("preview"))
    stderr = stderr_category(result.stderr)
    if case.accepted_divergence == "uninstall-read-only":
        check_read_only_uninstall(case, result.returncode, output, stderr, candidate)
    if contender:
        followup = "contender" if concurrent else "retry"
        stderr += tuple(
            f"{followup} {category}" for category in stderr_category(contender.stderr)
        )
    exact = case.exact_output or case.command.split()[0] in {
        "market",
        "account",
        "watch",
    }
    if case.command == "account overview" and case.mode == "json":
        display = mask_overview_stdout(display, case.mode)
    if cutover and endpoints.overview_times is not None:
        risk_time, mark_time = endpoints.overview_times
        clocks = Normalize(
            home,
            fixture_values={
                risk_time: "<fixture-risk-time>",
                mark_time: "<fixture-mark-time>",
            },
        )
        # Validate the timestamps and their ages in stdout before replacing
        # the two source times allocated by the overview fixture.
        output = clocks.paths(output)
        display = clocks.text(display)
    diagnostic = result.stderr
    if case.command == "doctor" and case.exact_output:
        if case.mode == "json":
            display = mask_doctor_stream(display, checkout=cutover)
        display = normalize.text(display)
        diagnostic = normalize.text(mask_doctor_stream(diagnostic))
    raw = None
    if exact:
        raw = (
            (normalize.text(display).encode(), normalize.text(diagnostic).encode())
            if cutover
            else (display.encode(), diagnostic.encode())
        )
    return Observation(result.returncode, output, stderr, files, raw, tuple(checks))


def read_only_uninstall_path(case: Case) -> tuple[str, str]:
    if case.args[3] == "codex":
        return "clients/.codex/config.toml", "data.settings.mcp_servers.sai"
    return "clients/.cursor/mcp.json", "data.mcpServers.sai"


def check_read_only_uninstall(
    case: Case, code: int, output: Any, stderr: tuple[str, ...], candidate: bool
) -> None:
    if candidate:
        error = output["error"]
        if code != 1 or error["code"] != "INTERNAL_ERROR" or stderr:
            raise ValueError("Expected the native read-only uninstall error")
        report = error["details"]
        (row,) = report["results"]
        if row["error"] != "The client config is read-only" or row["changed"]:
            raise ValueError("The read-only error reported a change")
    else:
        report = output
        (row,) = report["results"]
        if code != 0 or stderr != ("diagnostic",):
            raise ValueError("Expected Python to remove the read-only entry")
    if (
        row["client"] != case.args[3]
        or row["scope"] != "user"
        or row["action"] != ("failed" if candidate else "removed")
        or report["reconnectNeeded"] != (not candidate)
    ):
        raise ValueError("Unexpected read-only uninstall result")


def mcp_tools(output: Any) -> dict[str, dict[str, Any]]:
    try:
        replies = {reply["id"]: reply["result"] for reply in output if "id" in reply}
        if not replies[1]["protocolVersion"]:
            raise ValueError("MCP initialize lacked a protocol version")
        tools = replies[2]["tools"]
        inventory = {tool["name"]: tool for tool in tools}
        if not inventory or len(inventory) != len(tools):
            raise ValueError("MCP tools/list gave an empty or duplicate inventory")
        if any(not isinstance(name, str) for name in inventory):
            raise ValueError("MCP tools/list gave a non-string tool name")
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
        if call.outcome is not None and text.get("outcome") != call.outcome:
            raise ValueError(f"{call.tool} expected intent outcome {call.outcome}")


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


def cutover_differences(reference: Observation, candidate: Observation) -> list[str]:
    left, right = asdict(reference), asdict(candidate)
    checks = [
        f"{side}: {mismatch}"
        for side, observed in (("reference", left), ("candidate", right))
        for mismatch in observed.pop("checks")
    ]
    # Preserve the exit codes in the signature. All other observations
    # report paths and shape without emitting potentially private values.
    for observed in (left, right):
        observed["exit"] = observed.pop("exit_code")
    return checks + differing_paths(left, right, "parity")


def mcp_evidence(
    case: Case,
    reference: Observation | None,
    candidate: Observation | None,
    evidence: dict[str, list[str]],
) -> None:
    def record(tool: str, fields: list[str]) -> None:
        if fields:
            evidence.setdefault(tool, []).extend(fields)

    prefix = case.name
    left_tools = (
        mcp_tools(reference.output) if reference and case.mcp_calls is None else {}
    )
    right_tools = (
        mcp_tools(candidate.output) if candidate and case.mcp_calls is None else {}
    )
    for tool in left_tools.keys() | right_tools.keys():
        record(
            tool,
            differing_paths(
                left_tools.get(tool), right_tools.get(tool), f"{prefix}.tool"
            ),
        )
    left_calls = (
        {reply["id"]: reply["result"] for reply in reference.output}
        if reference
        else {}
    )
    right_calls = (
        {reply["id"]: reply["result"] for reply in candidate.output}
        if candidate
        else {}
    )
    if case.mcp_calls is None:
        names = enumerate((call.tool for call in CALLS[case.mcp_profile]), 3)
    else:
        names = enumerate((call["name"] for call in case.mcp_calls), 2)
    for id_, name in names:
        record(
            name,
            differing_paths(
                left_calls.get(id_), right_calls.get(id_), f"{prefix}.call[{id_}]"
            ),
        )


def matches_early_position_reads(
    reference: Observation, candidate: Observation, expected_reads: int
) -> bool:
    name = "logs/cli.jsonl"
    entry = candidate.files.get(name)
    if entry is None:
        return False
    remaining = []
    reads = 0
    for event in entry.data:
        sequence = event.get("seq")
        if type(sequence) is not int:
            return False
        if (event.get("event"), event.get("operation"), event.get("status")) == (
            "keeper.request",
            "position",
            "ok",
        ):
            if not remaining or sequence != remaining[-1]["seq"] + reads + 1:
                return False
            reads += 1
        else:
            remaining.append({**event, "seq": sequence - reads})
    if reads != expected_reads:
        return False
    files = {**candidate.files, name: replace(entry, data=remaining)}
    return not differences(reference, replace(candidate, files=files))


def safe_observe(
    case: Case,
    binary: list[str],
    home: Path,
    endpoints: Endpoints,
    *,
    candidate: bool = False,
    cutover: bool = False,
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
            observe(
                case, binary, home, endpoints, candidate=candidate, cutover=cutover
            ),
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


def project_reference_auth_status_errors(
    output: list[dict[str, Any]], endpoints: Endpoints
) -> str | None:
    """Replace the rejected address that the pinned Python reply quotes."""
    calls = CALLS["auth-status"]
    replies = {frame["id"]: frame["result"] for frame in output}
    changed = False
    for id_, call in enumerate(calls, 3):
        addr = call.args.get("addr")
        if isinstance(addr, str) and call.expected == "SESSION_UNAVAILABLE":
            result = replies[id_]
            body = result["content"][0]["text"]
            prefix = endpoints.expand(addr) + " is not connected;"
            if body["error"].startswith(prefix):
                error = (
                    "Requested account is not connected;" + body["error"][len(prefix) :]
                )
                body["error"] = error
                result["structuredContent"]["result"]["error"] = error
                result["_toolText"] = rewrite_mcp_fields(
                    result["_toolText"], {"error": error}
                )
                changed = True
    return "Native MCP auth status omits disconnected addresses" if changed else None


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


def project_reference_log_privacy(
    reference: Observation, candidate: Observation
) -> None:
    """Accept placeholder values while comparing log shape and public fields."""
    for name, reference_file in reference.files.items():
        if not name.startswith("logs/") or not name.endswith(".jsonl"):
            continue
        candidate_file = candidate.files.get(name)
        if candidate_file is None or len(reference_file.data) != len(
            candidate_file.data
        ):
            continue
        for old, new in zip(reference_file.data, candidate_file.data, strict=True):
            if old.get("event") != new.get("event"):
                continue
            old_argv, new_argv = old.get("argv"), new.get("argv")
            if (
                isinstance(old_argv, list)
                and isinstance(new_argv, list)
                and len(old_argv) == len(new_argv)
            ):
                for index, (before, after) in enumerate(
                    zip(old_argv, new_argv, strict=True)
                ):
                    if after == "[redacted]" or (
                        isinstance(after, str)
                        and isinstance(before, str)
                        and after.endswith("=[redacted]")
                        and before.startswith(after.removesuffix("[redacted]"))
                    ):
                        old_argv[index] = after
            for field in ("error", "errorMsg", "errorMessage"):
                if field in old and new.get(field) == "[redacted]":
                    old[field] = "[redacted]"
            if old.get("event") == "mcp.tool_call":
                old_args, new_args = old.get("arguments"), new.get("arguments")
                if isinstance(old_args, dict) and isinstance(new_args, dict):
                    for key in old_args.keys() & new_args.keys():
                        if new_args[key] == "[redacted]":
                            old_args[key] = "[redacted]"


def project_reference_rejected_scope(reference: Observation) -> Observation:
    # The usage envelope remains comparable. Only the fixed error line differs.
    if reference.raw is None:
        return reference
    stdout_bytes, stderr = reference.raw
    before, separator, message = stderr.rpartition(b"\nError: ")
    if not separator or not message.startswith(b"Invalid value for '--scope': "):
        return reference
    stderr = (
        before
        + separator
        + b"Invalid value for '--scope': expected one of 'user', 'project', 'local'.\n"
    )
    return replace(reference, raw=(stdout_bytes, stderr))


def project_reference_doctor_keeper_log(left: Observation, right: Observation) -> None:
    """Allow the existing omission of command context on Rust's Keeper probe."""
    name = "logs/cli.jsonl"
    if (reference := left.files.get(name)) is None or (
        native := right.files.get(name)
    ) is None:
        return
    for expected, actual in zip(reference.data, native.data, strict=False):
        if (
            expected.get("event") == actual.get("event") == "keeper.request"
            and expected.get("command") == "doctor"
            and "command" not in actual
            and "commandId" not in actual
        ):
            del expected["command"]
            expected.pop("commandId", None)


def hidden_arguments(count: int) -> list[str]:
    return [f"<argument hidden {i} of {count}>" for i in range(1, count + 1)]


def project_reference_saved_mcp_commands(case: Case, output: dict[str, Any]) -> None:
    def project(row: dict[str, Any], key: str) -> None:
        if argv := row.get(key):
            if row.get("args") == "<hidden>":
                # The shadowed Codex entries use a missing launcher and two operands.
                if case.client_seed == "shadowed":
                    row[key] = hidden_arguments(3)
            else:
                row[key] = [argv[0], *hidden_arguments(len(argv) - 1)]

    if "error" in output:
        output = output["error"].get("details", {})
    for row in output.get("clients", []):
        project(row, "registeredCommand")
        for registration in row["registrations"]:
            project(registration, "command")
    for row in output.get("results", []):
        project(row, "command")


def run_case(
    case: Case,
    reference: list[str],
    candidate: list[str],
    *,
    cutover: bool = False,
    python_main: bool = False,
    tool_evidence: dict[str, list[str]] | None = None,
) -> tuple[list[str], dict[str, str], str | None]:
    rust = candidate is not reference
    candidate_case = case.rust_case() if rust else case
    account_balances = case.command == "account balances"
    if python_main and case.collateral_price_after is not None:
        wire = list(case.wire or [])
        index = wire.index(case.collateral_price_after)
        wire.insert(index + 1, "get_collateral_price")
        case = replace(case, wire=wire)
        if not rust:
            candidate_case = case
    with (
        tempfile.TemporaryDirectory(prefix="sai-parity-") as temp,
        loopback(case.server, balance_block_reads=account_balances) as endpoints,
    ):
        left, left_error = safe_observe(
            case, reference, Path(temp) / "reference", endpoints, cutover=cutover
        )
        right, right_error = safe_observe(
            candidate_case,
            candidate,
            Path(temp) / "candidate",
            endpoints,
            candidate=rust,
            cutover=cutover,
        )
    if case.mode == "mcp" and tool_evidence is not None:
        mcp_evidence(case, left, right, tool_evidence)
    tools = {}
    inventory_case = case.mode == "mcp" and case.mcp_calls is None
    if inventory_case and left is not None:
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
    reference_exit = left.exit_code
    if candidate != reference and case.accepted_divergence == "lcd-origin-only":
        project_reference_lcd_origin(left.output)
    if rust and not cutover:
        project_reference_log_privacy(left, right)
        if case.name in {"mcp-install-invalid-scope", "mcp-install-empty-scope"}:
            left = project_reference_rejected_scope(left)
        if case.accepted_divergence == "claude-env-report":
            for result in left.output["results"]:
                if result["client"] == "claude":
                    result["argv"] = [
                        [
                            "--env=SAI_HOME=[redacted]"
                            if arg.startswith("--env=SAI_HOME=")
                            else arg
                            for arg in argv
                        ]
                        for argv in result["argv"]
                    ]
        if case.accepted_divergence == "launcher-argument-report":
            for result in left.output["results"]:
                launcher, *args = result["command"]
                result["command"] = [launcher, *hidden_arguments(len(args))]
        if (
            case.command in {"mcp list", "mcp uninstall"}
            or case.name == "mcp-install-legacy"
        ):
            project_reference_saved_mcp_commands(case, left.output)
        if case.name == "mcp-list-redacted" and isinstance(right.output, dict):
            for client in right.output.get("clients", []):
                for row in (client, *client.get("registrations", [])):
                    if row.get("staleEnv") == ["FIXTURE_CREDENTIAL"]:
                        row["staleEnv"] = ["1 other entry"]
    if (
        candidate != reference
        and case.accepted_divergence == "doctor-keeper-log-context"
    ):
        project_reference_doctor_keeper_log(left, right)
    if (
        candidate != reference
        and case.accepted_divergence == "codex-uninstall-rollback"
    ):
        # The fixture asserts deletion on Python and byte-for-byte restoration
        # on Rust. Compare the error and every other file and log record.
        error = left.output["error"]
        report = error["details"]
        report["results"][0]["changed"] = False
        report["reconnectNeeded"] = False
        reconnect = "\nClient registrations changed."
        error["message"] = error["message"].removesuffix(reconnect)
        if log := left.files.get("logs/cli.jsonl"):
            for record in log.data:
                if record["event"] == "command.end" and isinstance(
                    record.get("error"), str
                ):
                    record["error"] = record["error"].removesuffix(reconnect)
        filename = "clients/.codex/config.toml"
        left.files[filename] = right.files[filename]
        left = replace(
            left, stderr=tuple(item for item in left.stderr if item != "diagnostic")
        )
    if candidate != reference and case.accepted_divergence == "uninstall-read-only":
        # Both sides already checked exit, report, mode and changed bytes.
        # A successful removal also changes the command end record and trace.
        filename, _ = read_only_uninstall_path(case)
        left.files[filename] = right.files[filename]
        log = "logs/cli.jsonl"
        if log in left.files and log in right.files:
            left.files[log] = replace(
                left.files[log],
                data=left.files[log].data[:-1] + right.files[log].data[-1:],
            )
        trace = "clients/argv.jsonl"
        if trace in left.files and trace in right.files:
            python_trace = left.files[trace].data
            rust_trace = right.files[trace].data
            left.files[trace] = replace(
                left.files[trace],
                data={
                    **python_trace,
                    "data": python_trace["data"][: len(rust_trace["data"])],
                },
            )
        left = replace(
            left, exit_code=right.exit_code, output=right.output, stderr=right.stderr
        )
    expected = None
    if cutover:
        diffs = cutover_differences(left, right)
    else:
        if rust and case.position_case and not case.mcp_trade:
            project_position_estimates(left.output)
        if rust and case.accepted_difference and case.mode != "mcp":
            diffs = []
            if right.exit_code != candidate_case.expect:
                diffs.append(
                    f"expected candidate exit {candidate_case.expect}, got {right.exit_code}"
                )
            # Clap prints the cancel description before the usage line.
            if case.name == "bare-help-trade-cancel":
                if set(left.stderr) != set(right.stderr):
                    diffs.append("stderr categories differ")
            elif left.stderr != right.stderr:
                diffs.append("stderr differs")
        else:
            diffs = differences(left, right)
        expected = case.accepted_difference if rust else None
        if (
            rust
            and diffs
            and case.rust_early_position_reads
            and matches_early_position_reads(
                left, right, case.rust_early_position_reads
            )
        ):
            diffs = []
            expected = "Rust checks Keeper ownership before reading the LCD network."

        if inventory_case and rust:
            validation = expect_native_mcp_validation(left.output, case.mcp_profile)
            auth_status = None
            if case.mcp_profile == "auth-status":
                auth_status = project_reference_auth_status_errors(
                    left.output, endpoints
                )
            expected = (
                ", ".join(reason for reason in (validation, auth_status) if reason)
                or None
            )
            diffs = differences(left, right)
    if reference_exit != case.expect:
        diffs.insert(0, f"expected reference exit {case.expect}, got {reference_exit}")
    if (case.mode in {"mcp", "jsonl"} or case.expect == 0) and left.output == "":
        diffs.insert(0, "expected reference JSON output")
    if inventory_case:
        for call in CALLS[case.mcp_profile]:
            if call.expected == "ok" and not diffs:
                tools[call.tool] = "PASS"
    elif case.mcp_trade and not diffs:
        tools.update({call["name"]: "PASS" for call in case.mcp_calls or ()})
    return diffs, tools, expected


def release_commands(reference: list[str]) -> set[str]:
    interpreter = Path(reference[0]).with_name("python")
    if not interpreter.is_file():
        raise ValueError("The release launcher needs its installed Python interpreter")
    with (
        tempfile.TemporaryDirectory(prefix="sai-release-inventory-") as temp,
        loopback("normal") as endpoints,
    ):
        home = Path(temp)
        env = child_env(home, endpoints)
        result = invoke(
            [str(interpreter)], [str(HERE / "case_inventory.py")], home, env
        )
    if result.returncode:
        raise ValueError(f"Release command inventory exited {result.returncode}")
    try:
        names = json.loads(result.stdout)
    except ValueError:
        raise ValueError("Invalid release command inventory") from None
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) for name in names)
    ):
        raise ValueError("Invalid release command inventory")
    return set(names)


def supports_current_trade_ux(reference: list[str]) -> bool:
    """Probe the executable so installed launchers and wrappers both work."""
    with (
        tempfile.TemporaryDirectory(prefix="sai-reference-help-") as temp,
        loopback("normal") as endpoints,
    ):
        home = Path(temp)
        result = invoke(
            reference, ["trade", "cancel", "--help"], home, child_env(home, endpoints)
        )
    return (
        result.returncode == 0
        and result.stdout.startswith("Usage: sai trade cancel")
        and "--preview-digest" in result.stdout
    )


def project_position_estimates(value: Any) -> None:
    """Compare position quotes independently of Python's newer read-only estimates."""
    if isinstance(value, list):
        for item in value:
            project_position_estimates(item)
    elif isinstance(value, dict):
        if value.get("dryRun") and "positionRoute" in value:
            value.pop("estimates", None)
        for item in value.values():
            project_position_estimates(item)


def row(command: str, status: str, detail: str = "") -> None:
    escaped = [
        item.replace("|", "\\|").replace("\n", " ")
        for item in (command, status, detail)
    ]
    print("| " + " | ".join(escaped) + " |", flush=True)


def report_mcp_tools(
    tools: dict[str, str], evidence: dict[str, list[str]], gaps: ManifestGaps | None
) -> None:
    print("\n| MCP tool | Status | Diff summary |\n| --- | --- | --- |")
    for name in sorted(tools.keys() | evidence.keys()):
        status = tools.get(name, "MISSING")
        label = (
            name
            if name in RELEASE_MCP_TOOL_NAMES
            else f"unexpected-sha256:{value_fingerprint(name)}"
        )
        if gaps:
            diffs = sorted(evidence.get(name, []))
            if status != "PASS":
                diffs.append(
                    f"tools/list or tools/call: {status}; no passing transcript"
                )
            row(f"`{label}`", *gaps.result(f"mcp:{label}", diffs))
        else:
            row(
                f"`{label}`", status, "tools/call" if status == "PASS" else "tools/list"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", action="append", help="Run matching case IDs (glob, repeatable)"
    )
    parser.add_argument(
        "--reference",
        "--python-reference",
        help="Use this Python sai executable for every selected case",
    )
    parser.add_argument(
        "--cutover",
        action="store_true",
        help="Require each release row to pass or match its reviewed signature",
    )
    parser.add_argument(
        "--c2-v011",
        action="store_true",
        help="Run the C2 config comparisons with their exact logging signatures",
    )
    args = parser.parse_args()
    comparing_release = args.cutover or args.c2_v011
    if comparing_release and (
        args.case
        or not args.reference
        or not os.getenv("SAI_PARITY_BIN")
        or (args.cutover and args.c2_v011)
    ):
        parser.error(
            "Select one release suite with --reference and SAI_PARITY_BIN, without --case"
        )
    if sqlite3.sqlite_version_info < MIN_SQLITE_SCHEMA:
        parser.error(SQLITE_SCHEMA_REQUIREMENT)
    try:
        enter_loopback()
        # Use the CLI subprocess umask when seeding fixtures.
        os.umask(0o022)
        configured_reference = args.reference or os.environ.get("SAI_PARITY_PYTHON")
        if configured_reference is None:
            reference = [
                resolve_uv(),
                "run",
                "--project",
                str(ROOT),
                "--no-sync",
                "--offline",
                "sai",
            ]
        elif executable := shutil.which(configured_reference):
            reference = [str(Path(executable).absolute())]
        else:
            parser.error("--reference must name an executable Python sai")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    with (HERE / "cases.toml").open("rb") as file:
        configured = tomllib.load(file)
    cases = [Case(**item) for item in configured["case"]]
    deferred: dict[str, str] = configured.get("deferred", {})
    commands = cli_commands(python_cli)
    try:
        if args.cutover:
            require_manifest_case_ids(
                (case.name for case in cases), HERE / "release-case-ids.txt"
            )
            commands |= release_commands(reference)
        # The cancel command and the current margin preview shipped together.
        python_main = not comparing_release and (
            configured_reference is None or supports_current_trade_ux(reference)
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    missing = commands - ({case.command for case in cases} | deferred.keys())
    if missing:
        parser.error(
            f"Add cases or mark these commands deferred: {', '.join(sorted(missing))}"
        )
    selected = [
        case
        for case in cases
        if (python_main or not case.current_only)
        and (
            case.name in C2_CONFIG_CASE_IDS
            if args.c2_v011
            else not args.case
            or any(fnmatch.fnmatchcase(case.name, pattern) for pattern in args.case)
        )
    ]
    if args.c2_v011 and (
        missing := C2_CONFIG_CASE_IDS - {case.name for case in selected}
    ):
        parser.error(f"Missing C2 config case IDs: {', '.join(sorted(missing))}")
    if not selected:
        parser.error("No parity cases matched")
    candidate = reference
    checkout_python = str(Path(sys.executable).with_name("sai"))
    c9a_python = os.environ.get("SAI_C9A_PYTHON") or checkout_python
    uninstall_python = os.environ.get("SAI_MCP_UNINSTALL_PYTHON") or c9a_python
    if "SAI_PARITY_BIN" in os.environ:
        executable = shutil.which(os.environ["SAI_PARITY_BIN"])
        if not executable:
            parser.error("SAI_PARITY_BIN must name an executable file or command")
        candidate = [str(Path(executable).absolute())]
    mcp_reference = reference
    mcp_trade_reference = None
    if override := os.getenv("SAI_PARITY_MCP_TRADE_PYTHON"):
        executable = shutil.which(override)
        if not executable:
            parser.error(
                "SAI_PARITY_MCP_TRADE_PYTHON must name an executable Python sai"
            )
        mcp_trade_reference = [str(Path(executable).absolute())]
    if args.reference is None and any(case.mode == "mcp" for case in selected):
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
    evidence: dict[str, list[str]] = {}
    gaps = None
    if comparing_release:
        gaps = ManifestGaps(
            HERE / "manifest-xfails.toml",
            case_ids={f"case:{name}" for name in C2_CONFIG_CASE_IDS}
            if args.c2_v011
            else None,
            row_label="C2_V011_ROW" if args.c2_v011 else "CUTOVER_ROW",
        )
    current_gaps = None
    if python_main and candidate is not reference:
        current_gaps = ManifestGaps(
            HERE / "python-main-gaps.toml",
            case_ids={f"case:{case.name}" for case in selected} if args.case else None,
            row_label="CURRENT_PYTHON_ROW",
        )
    overview_reference = os.environ.get("SAI_PARITY_OVERVIEW_PYTHON")
    for case in selected:
        if case.checkout_reference:
            oracle = [checkout_python]
        elif case.mcp_trade and mcp_trade_reference:
            oracle = mcp_trade_reference
        elif args.reference is not None:
            oracle = reference
        elif overview_reference and (
            case.name == "account-bare" or case.command == "account overview"
        ):
            oracle = [str(Path(overview_reference).absolute())]
        elif case.mode == "mcp":
            oracle = mcp_reference
        elif case.command == "mcp uninstall":
            oracle = [uninstall_python]
        elif case.command == "doctor" and "node_info" in (case.wire or []):
            oracle = [checkout_python]
        elif case.client_seed or case.command in {"doctor", "mcp install", "mcp list"}:
            # Launch directly so uv cannot shadow the fixture launcher on PATH.
            oracle = [c9a_python]
        else:
            oracle = reference
        executable = oracle if candidate is reference else candidate
        if comparing_release:
            case = replace(
                case,
                accepted_divergence=None,
            )
        case_id = f"case:{case.name}"
        current_gap = (
            current_gaps is not None
            and oracle is reference
            and case_id in current_gaps.gaps
        )
        diffs, listed, expected = run_case(
            case,
            oracle,
            executable,
            cutover=comparing_release or current_gap,
            python_main=python_main and oracle is reference,
            tool_evidence=evidence if gaps else None,
        )
        for tool, status in listed.items():
            if status != "LISTED" or tool not in tools:
                tools[tool] = status
        shown_args = [
            "<redacted>"
            if any(marker in arg for marker in case.absent_output_text or ())
            else arg
            for arg in case.args
        ]
        command = f"`sai {shlex.join(shown_args)}` ({case.name})"
        if current_gap and current_gaps is not None:
            status, detail = current_gaps.result(case_id, diffs)
            mismatches += status in {"DIFF", "XPASS"}
            expected_differences += status == "XFAIL"
            row(command, status, detail)
            continue
        mismatches += bool(diffs)
        expected_differences += bool(expected and not diffs)
        if gaps:
            row(
                command,
                *gaps.result(f"case:{case.name}", diffs),
            )
            continue
        row(
            command,
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
    if not args.case and not args.c2_v011:
        for name, reason in sorted(deferred.items()):
            if gaps:
                row(
                    f"`sai {name}`",
                    *gaps.result(f"cli:{name}", [f"deferred: {reason}"]),
                )
            else:
                row(f"`sai {name}`", "DEFERRED", reason)
    if tools:
        report_mcp_tools(tools, evidence, gaps)
    passed = len(selected) - mismatches - expected_differences
    print(f"\n{passed}/{len(selected)} cases passed.")
    if expected_differences:
        print(f"Expected differences: {expected_differences}.")
    if gaps:
        if args.cutover and not tools:
            print("Cutover could not verify an MCP inventory.")
            return 1
        return gaps.finish()
    current_status = current_gaps.finish() if current_gaps is not None else 0
    return int(bool(mismatches or current_status))


if __name__ == "__main__":
    sys.exit(main())
