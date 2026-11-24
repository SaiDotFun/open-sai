"""Compare the Python sai CLI with SAI_PARITY_BIN, or with itself."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from compare import (
    LEDGER,
    Entry,
    Normalize,
    first_difference,
    ledger,
    snapshot,
    stderr_category,
    stdout,
)
from sai_cli.sai_cli import main as python_cli
from scenarios import HERE, ROOT, Endpoints, child_env, loopback, seed_home

MCP_REQUESTS = (
    {
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "sai-parity", "version": "1.0.0"},
        },
    },
    {"method": "notifications/initialized"},
    {"id": 2, "method": "tools/list"},
    {
        "id": 3,
        "method": "tools/call",
        "params": {"name": "market_list", "arguments": {}},
    },
    {
        "id": 4,
        "method": "tools/call",
        "params": {"name": "market_prices", "arguments": {}},
    },
)


@dataclass(frozen=True)
class Case:
    name: str
    args: list[str]
    expect: int = 0
    mode: str = "json"
    seed: str = "empty"
    server: str = "normal"
    setup: list[str] | None = None
    unset: list[str] | None = None
    wire: list[str] | None = None
    state: dict[str, Any] | None = None

    @property
    def command(self) -> str:
        args = [arg for arg in self.args if arg != "--json"]
        group = python_cli.commands.get(args[0])
        if (
            isinstance(group, click.Group)
            and len(args) > 1
            and args[1] in group.commands
        ):
            return " ".join(args[:2])
        return args[0]


@dataclass(frozen=True)
class Observation:
    exit_code: int
    output: Any
    stderr: tuple[str, ...]
    files: dict[str, Entry]


def invoke(
    binary: list[str],
    args: list[str],
    home: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*binary, *args],
        cwd=home,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
        umask=0o022,
        check=False,
    )


def mcp_exchange(
    binary: list[str], args: list[str], home: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    # Keep stdin open until the server replies. Closing it while a request is
    # still running can cancel the MCP server's pending work.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        with subprocess.Popen(
            [*binary, *args],
            cwd=home,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            encoding="utf-8",
            start_new_session=True,
            umask=0o022,
        ) as proc:
            assert proc.stdin is not None and proc.stdout is not None
            lines: queue.Queue[str | None] = queue.Queue()
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
                for request in MCP_REQUESTS:
                    proc.stdin.write(json.dumps({"jsonrpc": "2.0", **request}) + "\n")
                    proc.stdin.flush()
                    if "id" not in request:
                        continue
                    while True:
                        line = lines.get(timeout=max(0, deadline - time.monotonic()))
                        if line is None:
                            raise ValueError("MCP server closed stdout before replying")
                        received.append(line)
                        message = json.loads(line)
                        if (
                            isinstance(message, dict)
                            and message.get("id") == request["id"]
                        ):
                            break
                proc.stdin.close()
                while (
                    line := lines.get(timeout=max(0, deadline - time.monotonic()))
                ) is not None:
                    received.append(line)
                code = proc.wait(timeout=max(0, deadline - time.monotonic()))
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
        if expected == "<absent>" and actual is absent:
            continue
        if first_difference(actual, expected, selector):
            raise ValueError(f"Persisted state differs from expectation at {selector}")


def observe(
    case: Case, binary: list[str], home: Path, endpoints: Endpoints
) -> Observation:
    seed_home(home, endpoints, case.seed)
    env = child_env(home, endpoints)
    for key in case.unset or ():
        env.pop(key, None)
    if case.setup:
        setup = invoke(binary, [endpoints.expand(arg) for arg in case.setup], home, env)
        if setup.returncode:
            raise ValueError(f"Setup exited {setup.returncode}")
    args = [endpoints.expand(arg) for arg in case.args]
    run = mcp_exchange if case.mode == "mcp" else invoke
    contender = None
    concurrent = case.server == "concurrent-close"
    if concurrent:
        admission = endpoints.admission
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(invoke, binary, args, home, env)
            try:
                if not admission.arrived.wait(10):
                    raise ValueError("First process did not reach synthetic admission")
                before = Normalize(home)
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
    else:
        result = run(binary, args, home, env)
    if endpoints.errors:
        raise ValueError(endpoints.errors[0])
    actual, expected = endpoints.wire, case.wire or []
    if case.command == "doctor":
        actual, expected = sorted(actual), sorted(expected)
    if actual != expected:
        raise ValueError(f"Expected wire {expected}, got {actual}")
    normalize = Normalize(home, concurrent=concurrent)
    files = snapshot(home / "sai", normalize)
    check_state(case.state or {}, files)
    try:
        output = stdout(result.stdout, case.mode, case.command, normalize)
        if contender:
            output = {
                "first": output,
                "contender": stdout(
                    contender.stdout, case.mode, case.command, normalize
                ),
            }
    except (ValueError, AttributeError, KeyError, TypeError):
        raise ValueError(
            f"Invalid {case.mode} stdout (exit {result.returncode})"
        ) from None
    if case.mode == "mcp":
        mcp_tools(output)
    stderr = stderr_category(result.stderr)
    if contender:
        stderr += tuple(
            f"contender {category}" for category in stderr_category(contender.stderr)
        )
    return Observation(result.returncode, output, stderr, files)


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
) -> tuple[Observation | None, str]:
    endpoints.errors.clear()
    endpoints.wire.clear()
    endpoints.admission.arrived.clear()
    endpoints.admission.release.clear()
    endpoints.admission.key = ""
    try:
        return observe(case, binary, home, endpoints), ""
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except subprocess.CalledProcessError as exc:
        return None, f"fixture setup exited {exc.returncode}"
    except ValueError as exc:
        return None, str(exc)
    except OSError as exc:
        return None, type(exc).__name__


def run_case(
    case: Case, reference: list[str], candidate: list[str]
) -> tuple[list[str], dict[str, str]]:
    with (
        tempfile.TemporaryDirectory(prefix="sai-parity-") as temp,
        loopback(case.server) as endpoints,
    ):
        left, left_error = safe_observe(
            case, reference, Path(temp) / "reference", endpoints
        )
        right, right_error = safe_observe(
            case, candidate, Path(temp) / "candidate", endpoints
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
        return [
            f"{side} {error}"
            for side, error in (("reference", left_error), ("candidate", right_error))
            if error
        ], tools
    diffs = differences(left, right)
    if left.exit_code != case.expect:
        diffs.insert(0, f"expected reference exit {case.expect}, got {left.exit_code}")
    if (case.mode in {"mcp", "jsonl"} or case.expect == 0) and left.output == "":
        diffs.insert(0, "expected reference JSON output")
    if case.mode == "mcp":
        calls = {reply.get("id"): reply.get("result", {}) for reply in left.output}
        for request_id, tool in [(3, "market_list"), (4, "market_prices")]:
            call = calls.get(request_id, {})
            if not call.get("content") or call.get("isError"):
                diffs.append(f"reference MCP call failed for {tool}")
            elif not diffs:
                tools[tool] = "PASS"
    return diffs, tools


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
    args = parser.parse_args()
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
    uv = shutil.which("uv")
    if not uv:
        parser.error("uv is required to run the Python reference")
    reference = [uv, "run", "--project", str(ROOT), "--no-sync", "--offline", "sai"]
    candidate = reference
    if "SAI_PARITY_BIN" in os.environ:
        executable = shutil.which(os.environ["SAI_PARITY_BIN"])
        if not executable:
            parser.error("SAI_PARITY_BIN must name an executable file or command")
        candidate = [str(Path(executable).absolute())]
    print("# Sai CLI Parity\n")
    print(
        "Python vs Python (self-check).\n"
        if candidate is reference
        else "Python vs candidate.\n"
    )
    print("| Command | Status | Diff summary |\n| --- | --- | --- |", flush=True)
    mismatches = 0
    tools: dict[str, str] = {}
    for case in selected:
        diffs, listed = run_case(case, reference, candidate)
        tools.update(listed)
        mismatches += bool(diffs)
        row(
            f"`sai {shlex.join(case.args)}` ({case.name})",
            "DIFF" if diffs else "PASS",
            ", ".join(diffs),
        )
    if not args.case:
        for name, reason in sorted(deferred.items()):
            row(f"`sai {name}`", "DEFERRED", reason)
    if tools:
        print("\n| MCP tool | Status | Diff summary |\n| --- | --- | --- |")
        for name, status in tools.items():
            row(f"`{name}`", status, "tools/call" if status == "PASS" else "tools/list")
    print(f"\n{len(selected) - mismatches}/{len(selected)} cases passed.")
    return int(bool(mismatches))


if __name__ == "__main__":
    sys.exit(main())
