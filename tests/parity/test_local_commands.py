"""C9b parity against Python bdc11da6c9938aa0371835d921f27d45daa77c36."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "sai"


@pytest.fixture
def local_pair(
    cli_pair: tuple[tuple[str, ...], tuple[str, ...]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    # CI's C4 reference predates these commands. Its current checkout
    # retains the Python C9b implementation and installs it alongside pytest.
    reference = os.environ.get("SAI_C9B_PARITY_PYTHON") or shutil.which(
        "sai", path=str(Path(sys.executable).parent)
    )
    assert reference is not None
    return (reference,), cli_pair[1]


def case_env(root: Path, *, logging: str) -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": str(root),
        "SAI_HOME": str(root / "sai-home"),
        "CI": "1",
        "NO_COLOR": "1",
        "PYTHONUTF8": "1",
        "SAI_LOG": logging,
    }


def invoke(
    cli: tuple[str, ...],
    root: Path,
    *args: str,
    stdin: bytes = b"",
    logging: str = "0",
) -> subprocess.CompletedProcess[bytes]:
    root.mkdir(mode=0o700, exist_ok=True)
    return subprocess.run(
        [*cli, *args],
        cwd=root,
        env=case_env(root, logging=logging),
        input=stdin,
        capture_output=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize(
    "args,page",
    [
        (("--skill",), "SKILL.md"),
        (("skill", "show"), "SKILL.md"),
        (("--json", "skill", "show", "--page", "SKILL.md"), "SKILL.md"),
        (("--json", "skill", "show", "--page", "contracts"), "references/contracts.md"),
        (("skill", "show", "--page", "examples/prompts.md"), "examples/prompts.md"),
    ],
)
def test_skill_show(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    args: tuple[str, ...],
    page: str,
) -> None:
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        result = invoke(cli, root, *args, logging="1")
        assert result.returncode == 0 and result.stderr == b""
        assert result.stdout == (SKILL / page).read_bytes()
        assert not (root / "sai-home").exists()


def test_skill_source(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    reports = []
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        result = invoke(cli, root, "--json", "skill", "source")
        assert result.returncode == 0 and result.stderr == b""
        report = json.loads(result.stdout)
        location = Path(report.pop("path"))
        if index == 0:
            assert (location / "SKILL.md").read_bytes() == (
                SKILL / "SKILL.md"
            ).read_bytes()
        else:
            assert location.samefile(cli[0])
        assert report.pop("cliVersion")
        assert Path(report["install"].pop("defaultDir")) == root / ".agents/skills/sai"
        reports.append(report)
    assert reports[0] == reports[1]


@pytest.mark.parametrize("mode", ["default", "modified", "symlink", "unrelated"])
def test_skill_install(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path, mode: str
) -> None:
    package = {
        path.relative_to(SKILL): path.read_bytes()
        for path in SKILL.rglob("*")
        if path.is_file()
    }
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        root.mkdir()
        target = root / ".agents/skills/sai" if mode == "default" else root / "skill"
        options = [] if mode == "default" else ["--dir", str(target)]
        if mode == "modified":
            shutil.copytree(SKILL, target)
            (target / "cli.md").write_bytes(b"edited")
        elif mode == "symlink":
            target.symlink_to("external", target_is_directory=True)
        elif mode == "unrelated":
            target.mkdir()
            (target / "SKILL.md").write_bytes(b"---\nname: another\n")
        if mode in {"modified", "symlink"}:
            refused = invoke(cli, root, "--json", "skill", "install", *options)
            assert refused.returncode == 1 and refused.stderr == b""
            assert "--force" in json.loads(refused.stdout)["error"]["message"]
        force = [] if mode == "default" else ["--force"]
        result = invoke(cli, root, "--json", "skill", "install", *options, *force)
        if mode == "unrelated":
            assert result.returncode == 1
            assert (target / "SKILL.md").read_bytes() == b"---\nname: another\n"
            continue
        assert result.returncode == 0 and result.stderr == b""
        report = json.loads(result.stdout)
        assert report["action"] == ("installed" if mode == "default" else "replaced")
        assert report["files"] == len(package)
        assert Path(report["dir"]) == target
        assert report["commit"] == json.loads(package[Path(".source.json")])["commit"]
        assert stat.S_IMODE(target.stat().st_mode) == 0o755
        assert not target.is_symlink()
        assert not (root / "external").exists()
        assert {
            path.relative_to(target): path.read_bytes()
            for path in target.rglob("*")
            if path.is_file()
        } == package
        repeated = invoke(cli, root, "--json", "skill", "install", *options)
        assert repeated.returncode == 0 and repeated.stderr == b""
        assert json.loads(repeated.stdout) == {**report, "action": "unchanged"}


RUN = "1a" * 16
OTHER_RUN = "2b" * 16
COMMAND = "3c" * 16
CALL = "4d" * 6
ADDR = "0x" + "12" * 20
ENDPOINT_ID = "h1_" + "5e" * 32
STAMP = "2026-11-23T00:00:00.000Z"
INTENT_KEY = "20261123T000000000Z_0123456789ABCDEF"


def event(
    name: str,
    seq: int,
    *,
    surface: str = "cli",
    run: str = RUN,
    **fields: object,
) -> dict[str, object]:
    return {
        "schema": 2,
        "ts": STAMP,
        "level": "info",
        "event": name,
        "surface": surface,
        "pid": 42,
        "version": "0.1.1",
        "runId": run,
        "seq": seq,
        **fields,
    }


def seed_logs(root: Path, streams: dict[str, list[dict[str, object]]]) -> Path:
    logs = root / "sai-home" / "logs"
    logs.mkdir(parents=True)
    for surface, rows in streams.items():
        (logs / f"{surface}-{STAMP[:10]}.jsonl").write_bytes(
            b"".join(json.dumps(row).encode() + b"\n" for row in rows)
        )
    return logs


def cli_json(
    cli: tuple[str, ...], root: Path, *args: str, logging: str = "1"
) -> dict[str, object]:
    result = invoke(cli, root, "--json", *args, logging=logging)
    assert result.returncode == 0 and result.stderr == b"", result
    return json.loads(result.stdout)


def test_log_path_and_tail(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    outputs = []
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        absent = cli_json(cli, root, "logs", "path")
        assert absent == {
            "dir": str(root / "sai-home/logs"),
            "enabled": True,
            "keepDays": 14,
            "files": [],
        }
        assert not (root / "sai-home").exists()
        logs = seed_logs(
            root,
            {
                "cli": [
                    event(
                        "keeper.request",
                        2,
                        keeperErrors=[{"message": "private"}],
                        operation="overview_trades",
                        durationMs=3,
                    )
                ],
                "mcp": [
                    event("command.start", 1, surface="mcp"),
                    event(
                        "keeper.request",
                        3,
                        surface="mcp",
                        operation="pending_market_orders",
                        durationMs=5,
                    ),
                ],
            },
        )
        (logs / "update-2026-11-23.jsonl").symlink_to("cli-2026-11-23.jsonl")
        assert cli_json(cli, root, "logs", "path", logging="0")["files"] == [
            "cli-2026-11-23.jsonl",
            "mcp-2026-11-23.jsonl",
        ]
        before = {
            path.name: path.read_bytes()
            for path in logs.iterdir()
            if not path.is_symlink()
        }
        result = invoke(
            cli,
            root,
            "--json",
            "logs",
            "tail",
            "-n",
            "2",
            "--event",
            "keeper",
            logging="1",
        )
        assert result.returncode == 0 and result.stderr == b"", result
        rows = [json.loads(row) for row in result.stdout.splitlines()]
        assert [row["seq"] for row in rows] == [2, 3]
        assert rows[0]["keeperErrors"] == [{"code": "KEEPER_ERROR"}]
        assert rows[0]["keeperErrorCount"] == 1
        assert cli_json(cli, root, "logs", "stats")["keeper"] == {
            "overview_trades": {"count": 1, "p50Ms": 3, "p95Ms": 3},
            "pending_market_orders": {"count": 1, "p50Ms": 5, "p95Ms": 5},
        }
        assert invoke(cli, root, "logs", "tail", "--surface", "mcp").stdout.startswith(
            f"{STAMP} mcp info command.start\n".encode()
        )
        assert {
            path.name: path.read_bytes()
            for path in logs.iterdir()
            if not path.is_symlink()
        } == before
        outputs.append(rows)
    assert outputs[0] == outputs[1]


def test_logs_run_same_stamp_cursor(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    outputs = []
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        seed_logs(
            root,
            {
                "cli": [
                    event("command.end", seq, error="private")
                    for seq in range(1, 161, 2)
                ],
                "mcp": [
                    event("mcp.tool_result", seq, surface="mcp", callId=CALL)
                    for seq in range(2, 161, 2)
                ],
            },
        )
        first = cli_json(cli, root, "logs", "run", RUN)
        cursor = first["nextBefore"]
        assert isinstance(cursor, str) and cursor.startswith(f"{STAMP}~")
        second = cli_json(cli, root, "logs", "run", RUN, "--before", cursor)
        assert "nextBefore" not in second
        assert [
            row["seq"] for page in (second, first) for row in page["events"]
        ] == list(range(1, 161))
        assert all("error" not in row for row in first["events"])
        assert len(cli_json(cli, root, "logs", "run", CALL)["events"]) == 80
        assert (
            cli_json(cli, root, "logs", "run", RUN, "--before", STAMP)["events"] == []
        )
        outputs.append([first, second])
    assert outputs[0] == outputs[1]


def test_logs_stats_continuation_counts_each_request_once(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    outputs = []
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        seed_logs(
            root,
            {
                "cli": [event("log.dropped", seq, count=1) for seq in range(1, 261, 2)],
                "mcp": [
                    event(
                        "keeper.request",
                        seq,
                        surface="mcp",
                        operation="prices",
                        durationMs=seq,
                        code="KEEPER_ERROR",
                    )
                    for seq in range(2, 261, 2)
                ],
            },
        )
        first = cli_json(cli, root, "logs", "stats")
        cursor = first["nextBefore"]
        assert isinstance(cursor, str)
        second = cli_json(cli, root, "logs", "stats", "--before", cursor)
        assert "nextBefore" not in second
        assert [len(page["gaps"]) for page in (first, second)] == [128, 2]
        assert [page["keeper"]["prices"]["count"] for page in (first, second)] == [
            129,
            1,
        ]
        assert [page["errors"]["KEEPER_ERROR"] for page in (first, second)] == [129, 1]
        outputs.append([first, second])
    assert outputs[0] == outputs[1]


def test_logs_intent_scopes_and_projects(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    reports = []
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        seed_logs(
            root,
            {
                "cli": [
                    event(
                        "command.start", 1, commandId=COMMAND, argv=["fixture-private"]
                    ),
                    event(
                        "intent.status",
                        2,
                        commandId=COMMAND,
                        intentKey=INTENT_KEY,
                        addr=ADDR,
                        endpointId=ENDPOINT_ID,
                        status="pending",
                    ),
                    event("command.end", 3, commandId=COMMAND),
                    event("log.dropped", 4, count=3),
                    event("log.dropped", 5, run=OTHER_RUN, count=7),
                    event(
                        "intent.status",
                        6,
                        intentKey=INTENT_KEY,
                        addr=ADDR,
                        endpoint="https://fixture.invalid/private",
                    ),
                ],
            },
        )
        report = cli_json(cli, root, "logs", "intent", INTENT_KEY, "--offline")
        assert report["intentKey"] == INTENT_KEY
        assert [row["seq"] for row in report["events"]] == [1, 2, 3]
        assert report["gaps"][0]["count"] == 3
        assert report["unattributedGaps"][0]["count"] == 7
        assert "Older events" in report["scopeWarning"]
        assert "fixture-private" not in json.dumps(report)
        reports.append(report)
    assert reports[0] == reports[1]


def test_logs_intent_reads_keeper_once(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    import hashlib
    import hmac
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    requests: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            response = json.dumps(
                {
                    "data": {
                        "userIntents": [
                            {"intentKey": INTENT_KEY, "evmAddress": "0x" + "34" * 20},
                            {
                                "intentKey": INTENT_KEY,
                                "evmAddress": ADDR,
                                "kind": "OPEN_TRADE",
                                "lifecycleStep": "exec_pass",
                                "txHash": "F0" * 32,
                                "unused": "private fixture",
                            },
                        ]
                    }
                }
            ).encode()
            if request["variables"]["key"] == "missing-key":
                response = b'{"data":{"userIntents":[]}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            pass

    reports = []
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for index, cli in enumerate(local_pair):
                root = tmp_path / str(index)
                endpoint = f"http://127.0.0.1:{server.server_port}/graphql"
                report = cli_json(
                    cli,
                    root,
                    "--endpoint",
                    endpoint,
                    "logs",
                    "intent",
                    INTENT_KEY,
                    "--addr",
                    ADDR,
                )
                assert report["txHash"] == "f0" * 32
                assert report["keeper"]["evmAddress"] == ADDR
                assert "unused" not in report["keeper"]
                assert not (root / "sai-home").exists()
                reports.append(report)
                cli_json(
                    cli,
                    root,
                    "--endpoint",
                    endpoint,
                    "intent",
                    "status",
                    ADDR,
                    "--key",
                    "missing-key",
                )
                logs = root / "sai-home/logs"
                profile_key = logs / ".endpoint-key"
                assert stat.S_IMODE(profile_key.stat().st_mode) == 0o600
                assert len(profile_key.read_bytes()) == 32
                identity = (
                    "h1_"
                    + hmac.digest(
                        profile_key.read_bytes(),
                        hashlib.sha256((endpoint + "?").encode()).digest(),
                        "sha256",
                    ).hex()
                )
                observed = cli_json(
                    cli,
                    root,
                    "--endpoint",
                    endpoint,
                    "logs",
                    "intent",
                    "missing-key",
                    "--offline",
                )
                events = observed["events"]
                assert [row["event"] for row in events] == [
                    "command.start",
                    "keeper.request",
                    "intent.keeper",
                    "command.end",
                ]
                assert {row["commandId"] for row in events} == {events[0]["commandId"]}
                assert all(row["schema"] == 2 for row in events)
                assert all(
                    row["endpointId"] == identity for row in events if "endpoint" in row
                )
        finally:
            server.shutdown()
            thread.join()
    assert len(requests) == 4
    assert requests[:2] == requests[2:]
    assert requests[0]["variables"] == {"key": INTENT_KEY, "user": ADDR}
    assert reports[0] == reports[1]


def test_command_log_v2(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    runs = []
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        cli_json(cli, root, "config", "list")
        logs = root / "sai-home/logs"
        rows = [
            json.loads(line)
            for file in logs.glob("*.jsonl")
            for line in file.read_bytes().splitlines()
        ]
        assert [row["event"] for row in rows] == ["command.start", "command.end"]
        assert all(row["schema"] == 2 for row in rows)
        assert rows[0]["runId"] == rows[1]["runId"]
        assert rows[0]["commandId"] == rows[1]["commandId"]
        assert len(rows[0]["commandId"]) == 32
        for row in rows:
            for volatile in (
                "ts",
                "pid",
                "version",
                "runId",
                "commandId",
                "durationMs",
            ):
                row.pop(volatile, None)
        runs.append(rows)
    assert runs[0] == runs[1]


@pytest.mark.parametrize("command", ["run", "stats"])
def test_cursor_resumes_after_an_empty_byte_budget_page(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    command: str,
) -> None:
    from datetime import UTC, datetime, timedelta

    start = datetime.fromisoformat(STAMP).astimezone(UTC)
    args = ("logs", command, *((RUN,) if command == "run" else ()))
    field = "events" if command == "run" else "keeper"

    def ts(milliseconds: int) -> str:
        return (
            (start + timedelta(milliseconds=milliseconds))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    outputs = []
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        logs = seed_logs(root, {})
        sample = event(
            "keeper.request", 0, operation="prices", durationMs=7, extra="x" * 1800
        )
        with (logs / f"cli-{STAMP[:10]}.jsonl").open("wb") as stream:
            stream.writelines(
                json.dumps({**sample, "seq": seq, "ts": ts(seq)}).encode() + b"\n"
                for seq in range(9500)
            )

        page = cli_json(cli, root, *args, "--before", ts(300))
        assert page["truncated"] and page[field] in ([], {})
        pages = [page]
        while cursor := page.get("nextBefore"):
            assert isinstance(cursor, str)
            page = cli_json(cli, root, *args, "--before", cursor)
            pages.append(page)
            assert len(pages) <= 4

        if command == "run":
            assert sorted(row["seq"] for page in pages for row in page[field]) == list(
                range(300)
            )
        else:
            assert (
                sum(page[field].get("prices", {}).get("count", 0) for page in pages)
                == 300
            )
        outputs.append(pages)
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    "args",
    [
        ("skill", "--help"),
        ("skill", "install", "--help"),
        ("logs",),
        ("logs", "intent"),
        ("logs", "tail", "-n", "-1"),
        ("logs", "tail", "--surface", "wrong"),
        ("logs", "stats", "--since", "0h"),
        ("--json", "logs", "run", "process", "--before", "bad~cursor"),
    ],
)
def test_local_usage_matches_click(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    args: tuple[str, ...],
) -> None:
    outputs = []
    for index, cli in enumerate(local_pair):
        result = invoke(cli, tmp_path / str(index), *args)
        outputs.append((result.returncode, result.stdout, result.stderr))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    "args",
    [
        ("--json", "config", "set", "keeper.endpoint", "https://fixture.invalid:abc"),
        ("--json", "config", "set", "keeper.endpoint", "https://fixture.invalid:65536"),
        ("--json", "config", "set", "keeper.endpoint", "https://host space.invalid"),
        ("--json", "--endpoint", "https://fixture.invalid:abc", "config", "list"),
        ("--endpoint", "https://fixture.invalid/#fragment", "config", "list"),
    ],
)
def test_malformed_endpoints(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    args: tuple[str, ...],
) -> None:
    outputs = []
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        result = invoke(cli, root, *args)
        assert result.returncode == 2
        assert not (root / "sai-home").exists()
        outputs.append((result.stdout, result.stderr))
    assert outputs[0] == outputs[1]


def test_repl_pipe(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    outputs = []
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        endpoint = "http://127.0.0.1:1/graphql"
        result = invoke(
            cli,
            root,
            "--endpoint",
            endpoint,
            stdin=b"help config get\nconfig get 'keeper.endpoint'\nconfig get 'bad\nquit\n",
        )
        assert result.returncode == 0
        assert b"Sai " in result.stdout
        assert b"Usage: sai config get [OPTIONS] KEY" in result.stdout
        assert endpoint.encode() in result.stdout
        assert result.stderr == b"Error: No closing quotation\n"
        trader = root / "sai-home/trader"
        assert stat.S_IMODE(trader.stat().st_mode) == 0o700
        assert not (trader / "history").exists()
        stdout = result.stdout.split(b"Enter a command", 1)[1]
        outputs.append((stdout, result.stderr))
        assert invoke(cli, root, stdin=b"").stdout.endswith("> sai ❯ \n".encode())
        missing = invoke(cli, root, "--json")
        assert missing.returncode == 2 and missing.stderr == b""
        assert (
            json.loads(missing.stdout)["error"]["message"]
            == "Pass a command when using --json."
        )
    assert outputs[0] == outputs[1]


def test_repl_keeps_terminal_history_and_recovers_from_interrupt(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    import errno
    import pty
    import select
    import time

    def recv(master: int, pending: bytearray, needle: bytes) -> None:
        deadline = time.monotonic() + 8
        while needle not in pending and time.monotonic() < deadline:
            if not select.select([master], [], [], 0.2)[0]:
                continue
            try:
                pending.extend(os.read(master, 4096))
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
        assert needle in pending, bytes(pending)
        end = pending.index(needle) + len(needle)
        del pending[:end]

    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        root.mkdir(mode=0o700)
        master, slave = pty.openpty()
        with subprocess.Popen(
            cli,
            cwd=root,
            env={**case_env(root, logging="0"), "TERM": "xterm-256color"},
            stdin=slave,
            stdout=slave,
            stderr=slave,
        ) as process:
            os.close(slave)
            pending = bytearray()

            try:
                recv(master, pending, "❯".encode())
                os.write(master, b"discard-me\x03")
                recv(master, pending, "❯".encode())
                os.write(master, b"config get keeper.endpoint\r")
                recv(master, pending, b"https://sai-keeper.nibiru.fi/query")
                recv(master, pending, "❯".encode())
                os.write(master, b"\x04")
                assert process.wait(timeout=8) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                os.close(master)
        history = root / "sai-home/trader/history"
        assert stat.S_IMODE(history.stat().st_mode) == 0o600
        assert b"+config get keeper.endpoint\n" in history.read_bytes()
        assert b"discard-me" not in history.read_bytes()


def test_repl_commands_share_run_but_keep_command_ids(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        result = invoke(
            cli,
            root,
            stdin=b"config get logs.enabled\nconfig get logs.keep_days\nquit\n",
            logging="1",
        )
        assert result.returncode == 0 and not result.stderr
        rows = [
            json.loads(line)
            for path in sorted((root / "sai-home/logs").glob("cli-*.jsonl"))
            for line in path.read_bytes().splitlines()
        ]
        assert [row["seq"] for row in rows] == list(range(1, 7))
        assert [row["command"] for row in rows] == [
            "repl",
            "config get",
            "config get",
            "config get",
            "config get",
            "repl",
        ]
        ids = [row["commandId"] for row in rows]
        assert ids[0] == ids[-1] and ids[1] == ids[2] and ids[3] == ids[4]
        assert len(set(ids)) == 3
        assert len({row["runId"] for row in rows}) == 1
