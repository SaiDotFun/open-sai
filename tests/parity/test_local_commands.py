"""C9b parity against Python bdc11da6c9938aa0371835d921f27d45daa77c36."""

from __future__ import annotations

import json
import os
import random
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


@pytest.fixture
def legacy_logs() -> bool:
    return bool(os.environ.get("SAI_C9B_PARITY_PYTHON"))


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
        assert location.is_absolute() and location.is_dir()
        for page in SKILL.rglob("*.md"):
            assert (
                location / page.relative_to(SKILL)
            ).read_bytes() == page.read_bytes()
        repeated = cli_json(cli, root, "skill", "source")
        assert Path(repeated["path"]) == location
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


def test_skill_force_keeps_previous_contents(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    _, rust = local_pair
    target = tmp_path / "skill"
    shutil.copytree(SKILL, target)
    (target / "editor-note.txt").write_bytes(b"unpublished edit")

    report = cli_json(
        rust, tmp_path, "skill", "install", "--dir", str(target), "--force"
    )
    assert report["action"] == "replaced"
    [previous] = tmp_path.glob(".sai-previous-*/skill")
    assert (previous / "editor-note.txt").read_bytes() == b"unpublished edit"
    assert (target / "SKILL.md").read_bytes() == (SKILL / "SKILL.md").read_bytes()
    assert not (target / "editor-note.txt").exists()


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


def test_logs_tail_redacts_retained_endpoints(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    legacy_logs: bool,
) -> None:
    origin = "https://fixture.invalid:8443"
    endpoint = (
        "https://user:fixture@fixture.invalid:8443/private?token=fixture#fragment"
    )
    ws_origin = "wss://ws.fixture.invalid"
    ws_endpoint = ws_origin + "/private-ws?session=fixture"
    for index, cli in enumerate(local_pair):
        preserves_urls = legacy_logs and index == 0
        expected = endpoint if preserves_urls else origin
        expected_ws = ws_endpoint if preserves_urls else ws_origin
        root = tmp_path / str(index)
        seed_logs(
            root,
            {
                "cli": [
                    event(
                        "command.start",
                        1,
                        endpoint=endpoint,
                        argv=[
                            f"--endpoint={endpoint}",
                            "--ws-endpoint",
                            ws_endpoint,
                            f"{origin}/private with spaces",
                        ],
                        detail={endpoint: [{"probe": f"{endpoint} or {ws_endpoint}"}]},
                    )
                ]
            },
        )
        for flag in (("--json",), ()):
            result = invoke(cli, root, *flag, "logs", "tail")
            assert result.returncode == 0 and result.stderr == b""
            endpoint_arg = "[redacted]" if index else expected
            ws_arg = "[redacted]" if index else expected_ws
            positional = f"{origin}/private with spaces" if preserves_urls else origin
            if flag:
                row = json.loads(result.stdout)
                assert row["endpoint"] == expected
                assert row["argv"] == [
                    f"--endpoint={endpoint_arg}",
                    "--ws-endpoint",
                    ws_arg,
                    "[redacted]" if index else positional,
                ]
                assert row["detail"] == {
                    expected: [{"probe": f"{expected} or {expected_ws}"}]
                }
            else:
                assert f"endpoint={expected}".encode() in result.stdout
                assert f'"--endpoint={endpoint_arg}"'.encode() in result.stdout
                assert f'"{ws_arg}"'.encode() in result.stdout
            if not preserves_urls:
                assert b"/private" not in result.stdout
                assert b"user:fixture" not in result.stdout
                assert b"with spaces" not in result.stdout


@pytest.mark.parametrize("flags", [(), ("--json",)], ids=["text", "json"])
def test_logs_tail_applies_argv_and_error_allowlists_to_retained_rows(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    flags: tuple[str, ...],
) -> None:
    _, rust = local_pair
    key = "rev6-retained-secret"
    seed_logs(
        tmp_path,
        {
            "cli": [
                event(
                    "command.start",
                    1,
                    argv=[
                        f"--endpoint=//127.0.0.1/private?api_key={key}",
                        "--ws-endpoint",
                        f"wss:/127.0.0.1/private?api_key={key}",
                        "trade",
                        "close",
                        "42",
                        "--side=long",
                        "--price",
                        "2.5",
                        f"--key=key={key}",
                        f"--{key}=1",
                    ],
                    endpoint=f"https://127.0.0.1:7331/private?key={key}",
                    websocket=f"//127.0.0.1/private?key={key}",
                    error=f'Rejected input: "key={key}"',
                )
            ]
        },
    )
    result = invoke(rust, tmp_path, *flags, "logs", "tail")
    assert result.returncode == 0 and not result.stderr
    assert key.encode() not in result.stdout
    if flags:
        row = json.loads(result.stdout)
        assert row["argv"] == [
            "--endpoint=[redacted]",
            "--ws-endpoint",
            "[redacted]",
            "trade",
            "close",
            "42",
            "--side=long",
            "--price",
            "2.5",
            "--key=[redacted]",
            "[redacted]",
        ]
        assert row["endpoint"] == "https://127.0.0.1:7331"
        assert row["websocket"] == row["error"] == "[redacted]"
    else:
        assert b"error=[redacted]" in result.stdout
        assert b"websocket=[redacted]" in result.stdout


def test_logs_run_keeps_events_with_non_utf8_argv(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    python, _ = local_pair
    result = invoke(
        python,
        tmp_path,
        "config",
        "get",
        "invalid-" + os.fsdecode(b"\xff"),
        logging="1",
    )
    assert result.returncode == 2
    rows = [
        row
        for path in (tmp_path / "sai-home/logs").glob("cli-*.jsonl")
        for row in path.read_bytes().splitlines()
    ]
    start = next(row for row in rows if json.loads(row)["event"] == "command.start")
    assert b"\\udcff" in start
    run_id = json.loads(start)["runId"]
    for cli in local_pair:
        report = cli_json(cli, tmp_path, "logs", "run", run_id)
        events = report["events"]
        assert [row["event"] for row in events] == ["command.start", "command.end"]
        assert "argv" not in events[0]


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


def test_logs_intent_cursor_keeps_events_at_the_same_timestamp(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    legacy_logs: bool,
) -> None:
    for index, cli in enumerate(local_pair):
        keeps_ties = index == 1 or not legacy_logs
        root = tmp_path / str(index)
        fields = {"intentKey": INTENT_KEY, "addr": ADDR, "endpointId": ENDPOINT_ID}
        upper = STAMP.replace(".000Z", ".001Z")
        logs = seed_logs(
            root,
            {
                "cli": [
                    event("command.start", 1, commandId=COMMAND),
                    *(
                        event("intent.status", seq, commandId=COMMAND, **fields)
                        for seq in range(2, 130, 2)
                    ),
                    event("intent.status", 130, ts=upper, commandId=COMMAND, **fields),
                ],
                "mcp": [
                    event("intent.status", seq, surface="mcp", callId=CALL, **fields)
                    for seq in range(3, 130, 2)
                ],
            },
        )
        args = ("logs", "intent", INTENT_KEY, "--offline")
        first = cli_json(cli, root, *args, "--before", upper)
        assert [row["seq"] for row in first["events"]] == list(range(2, 130))
        if keeps_ties:
            # A new conflicting anchor must stay outside this continuation.
            conflict = event(
                "intent.status", 131, **{**fields, "addr": "0x" + "34" * 20}
            )
            with (logs / f"cli-{STAMP[:10]}.jsonl").open("a") as stream:
                stream.write(json.dumps(conflict) + "\n")
        second = cli_json(cli, root, *args, "--before", first["nextBefore"])
        assert [row["seq"] for row in second["events"]] == ([1] if keeps_ties else [])
        assert "nextBefore" not in second
        assert cli_json(cli, root, *args, "--before", STAMP)["events"] == []


def test_logs_intent_cursor_keeps_cross_day_attribution(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        logs = seed_logs(root, {"cli": [event("command.start", 1, commandId=COMMAND)]})
        next_day = "2026-11-24T00:00:00.000Z"
        padding = event("command.start", 2, ts=next_day, run=OTHER_RUN)
        anchor = event(
            "intent.reserved",
            3,
            ts=next_day,
            commandId=COMMAND,
            intentKey=INTENT_KEY,
            addr=ADDR,
            endpointId=ENDPOINT_ID,
        )
        encoded = (json.dumps(padding) + "\n") * 20_001 + json.dumps(anchor) + "\n"
        (logs / "cli-2026-11-24.jsonl").write_bytes(encoded.encode())
        args = ("logs", "intent", INTENT_KEY, "--offline")
        first = cli_json(cli, root, *args)
        assert first["truncated"]
        seen = [row["seq"] for row in first["events"]]
        if 1 not in seen:
            second = cli_json(cli, root, *args, "--before", first["nextBefore"])
            seen.extend(row["seq"] for row in second["events"])
            assert "nextBefore" not in second
        assert sorted(seen) == [1, 3]


@pytest.mark.parametrize("count", [127, 128, 129, 255, 256, 257, 300, 385])
@pytest.mark.parametrize("anchors", [1, 2])
def test_logs_intent_cursor_keeps_sparse_attribution(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    count: int,
    anchors: int,
) -> None:
    # The pinned Python cursor uses an exclusive timestamp, so only Rust can
    # traverse multiple pages when every event shares a millisecond.
    _, rust = local_pair
    streams: dict[str, list[dict[str, object]]] = {"cli": [], "mcp": []}
    for seq in range(1, count + 1):
        surface = "mcp" if anchors == 2 and seq % 2 else "cli"
        invocation = {"commandId": COMMAND} if surface == "cli" else {"callId": CALL}
        anchor = seq > count - anchors
        evidence = (
            {"intentKey": INTENT_KEY, "addr": ADDR, "endpointId": ENDPOINT_ID}
            if anchor
            else {}
        )
        streams[surface].append(
            event(
                "intent.status" if anchor else "keeper.request",
                seq,
                surface=surface,
                **invocation,
                **evidence,
            )
        )
    seed_logs(tmp_path, streams)
    args = ("logs", "intent", INTENT_KEY, "--offline")
    report = cli_json(rust, tmp_path, *args)
    for end in range(count, 0, -128):
        assert [row["seq"] for row in report["events"]] == list(
            range(max(1, end - 127), end + 1)
        )
        if end > 128:
            report = cli_json(rust, tmp_path, *args, "--before", report["nextBefore"])
        else:
            assert "nextBefore" not in report


@pytest.mark.parametrize("anchor_in_first_window", [False, True])
def test_logs_intent_cursor_keeps_anchors_discovered_after_scan_limit(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    anchor_in_first_window: bool,
) -> None:
    _, rust = local_pair
    other_command = "6f" * 16
    scope = {"intentKey": INTENT_KEY, "addr": ADDR, "endpointId": ENDPOINT_ID}
    initial = [
        event(
            "keeper.request",
            seq,
            commandId=other_command if anchor_in_first_window and seq % 2 else COMMAND,
        )
        for seq in range(1, 300)
    ]
    first_hash = "ab" * 32
    latest_hash = "cd" * 32 if anchor_in_first_window else first_hash
    initial.append(
        event("intent.submitted", 300, commandId=COMMAND, txHash=first_hash, **scope)
    )
    if anchor_in_first_window:
        initial.append(
            event(
                "intent.submitted", 300, commandId=COMMAND, txHash=latest_hash, **scope
            )
        )
    logs = seed_logs(tmp_path, {"cli": initial})
    filler = event("command.start", 301, run=OTHER_RUN)
    with (logs / f"cli-{STAMP[:10]}.jsonl").open("a") as stream:
        stream.write((json.dumps(filler) + "\n") * 20_001)
        if anchor_in_first_window:
            stream.write(
                json.dumps(
                    event("intent.status", 302, commandId=other_command, **scope)
                )
                + "\n"
            )
    args = ("logs", "intent", INTENT_KEY, "--offline")
    report = cli_json(rust, tmp_path, *args)
    seen = [row["seq"] for row in report["events"]]
    assert seen == ([302] if anchor_in_first_window else [])
    for size in (128, 128, 44 + anchor_in_first_window):
        report = cli_json(rust, tmp_path, *args, "--before", report["nextBefore"])
        assert len(report["events"]) == size
        assert report["txHash"] == latest_hash
        seen.extend(row["seq"] for row in report["events"])
    assert "nextBefore" not in report
    expected = list(range(1, 301))
    if anchor_in_first_window:
        expected.extend([300, 302])
    assert sorted(seen) == expected


@pytest.mark.parametrize("count", [1024, 1025])
def test_logs_intent_cursor_keeps_dense_attribution_after_scan_limit(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path, count: int
) -> None:
    from datetime import datetime, timedelta

    start = datetime.fromisoformat(STAMP)

    def stamp(seq: int) -> str:
        return (
            (start + timedelta(milliseconds=seq))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    anchors = [
        event(
            "intent.status",
            seq,
            ts=stamp(seq),
            commandId=f"{seq:032x}",
            intentKey=INTENT_KEY,
            addr=ADDR,
            endpointId=ENDPOINT_ID,
        )
        for seq in range(2, count + 2)
    ]
    linked = event("keeper.request", 1, ts=stamp(1), commandId=f"{count + 1:032x}")
    filler = event("command.start", count + 2, ts=stamp(10_000), run=OTHER_RUN)
    args = ("logs", "intent", INTENT_KEY, "--offline")
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        logs = seed_logs(root, {"cli": [linked, *anchors]})
        with (logs / f"cli-{STAMP[:10]}.jsonl").open("a") as stream:
            stream.write((json.dumps(filler) + "\n") * 20_500)

        page = cli_json(cli, root, *args)
        assert page["events"] == []
        seen = []
        for step in range(count // 128 + 3):
            page = cli_json(cli, root, *args, "--before", page["nextBefore"])
            if index and count == 1025 and step == 0:
                assert len(page["nextBefore"]) < 512
                prefix = page["nextBefore"].split("~intent~", 1)[0]
                oversized = f"{prefix}~intent~{STAMP[:10]}:0:0:{2**64 - 1}"
                rejected = invoke(cli, root, "--json", *args, "--before", oversized)
                assert rejected.returncode == 2
                conflict = event(
                    "intent.status",
                    count + 3,
                    ts=stamp(10_001),
                    intentKey=INTENT_KEY,
                    addr="0x" + "34" * 20,
                    endpointId=ENDPOINT_ID,
                )
                with (logs / f"cli-{STAMP[:10]}.jsonl").open("a") as stream:
                    stream.write(json.dumps(conflict) + "\n")
            seen.extend(row["seq"] for row in page["events"])
            if "nextBefore" not in page:
                break
        assert "nextBefore" not in page
        assert sorted(seen) == list(range(1, count + 2))


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
        stderr = result.stderr
        if index == 0 and args == ("logs", "intent"):
            stderr = stderr.replace(
                b"Read earlier events before this UTC timestamp.",
                b"Resume from a cursor or read before a UTC timestamp.",
            )
        if args[:2] == ("logs", "tail"):
            option = args[2].encode()
            lines = stderr.splitlines(keepends=True)
            assert lines[-1].startswith(b"Error: Invalid value for '" + option + b"'")
            if index == 0:
                lines[-1] = b"Error: Invalid value for '" + option + b"'.\n"
            stderr = b"".join(lines)
        outputs.append((result.returncode, result.stdout, stderr))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    "args, mode",
    [
        (("logs", "tail", "--surface"), "text"),
        (("--json", "logs", "tail", "-n"), "json"),
        (("logs", "tail"), "text"),
        (("--json", "skill", "source"), "json"),
        (("config", "get"), "text"),
        ((), "text"),
    ],
)
def test_usage_errors_hide_rejected_endpoint_credentials(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    args: tuple[str, ...],
    mode: str,
) -> None:
    # Accepted divergence: Python v0.1.2 prints the rejected URL.
    endpoint = (
        "https://fixture-user:fixture-password@fixture.invalid/"
        "fixture-private?api_key=fixture-key#fixture-fragment"
    )
    markers = (
        b"fixture-password",
        b"fixture-private",
        b"fixture-key",
        b"fixture-fragment",
    )
    for index, cli in enumerate(local_pair):
        result = invoke(cli, tmp_path / str(index), *args, endpoint)
        assert result.returncode == 2
        if mode == "json":
            assert result.stderr == b""
            assert json.loads(result.stdout)["error"]["message"]
        else:
            assert result.stdout == b""
            assert b"Error:" in result.stderr
        output = result.stdout + result.stderr
        if index:
            assert all(marker not in output for marker in markers)
        else:
            assert all(marker in output for marker in markers)


@pytest.mark.parametrize("flags", [(), ("--json",)], ids=["text", "json"])
def test_skill_page_error_hides_endpoint_credentials(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    flags: tuple[str, ...],
) -> None:
    _, rust = local_pair
    rejected = "https://fixture.invalid/page?api_key=fixture-page-key"
    result = invoke(
        rust, tmp_path, *flags, "skill", "show", "--page", rejected, logging="1"
    )
    assert result.returncode == 1
    assert b"fixture-page-key" not in result.stdout + result.stderr
    message = (
        json.loads(result.stdout)["error"]["message"]
        if flags
        else result.stderr.decode()
    )
    assert (
        "Skill page" in message
        and "Pages: " in message
        and "references/contracts" in message
    )
    assert not list((tmp_path / "sai-home/logs").glob("*.jsonl"))


@pytest.mark.parametrize(
    "args",
    [
        ("{}",),
        ("config", "get", "{}"),
        ("config", "set", "logs.enabled", "{}"),
        ("config", "unset", "{}"),
        ("logs", "tail", "--surface", "{}"),
        ("logs", "tail", "-n", "{}"),
        ("logs", "intent", "{}", "--offline"),
        ("logs", "run", "{}"),
        ("logs", "stats", "--since", "{}"),
        ("skill", "show", "--page", "{}"),
        ("skill", "source", "{}"),
        ("skill", "install", "--force={}"),
        ("auth", "login", "fixture", "--addr", "{}"),
        ("auth", "import", "{}"),
        ("auth", "renew", "{}"),
        ("auth", "use", "{}"),
        ("auth", "logout", ADDR, "{}"),
        ("account", "positions", ADDR, "{}"),
        ("account", "history", ADDR, "{}"),
        ("account", "balances", "{}"),
        ("watch", "positions", "{}", "--invalid"),
        ("watch", "balances", "{}", "--invalid"),
        ("watch", "price", "{}"),
        ("intent", "local", ADDR, "{}"),
        ("intent", "status", "{}"),
        ("mcp", "install", "--scope", "{}"),
        ("mcp", "install", "--client", "{}"),
        ("doctor", "--no-fix={}"),
        ("trade", "open", "--dry-run", "--side", "{}"),
        ("trade", "close", "--dry-run", "{}"),
        ("trade", "cancel", "--dry-run", "{}"),
        ("trade", "tpsl", "--dry-run", "{}"),
        ("trade", "leverage", "--dry-run", "{}", "--leverage", "1"),
    ],
)
def test_rejected_values_hide_credentials_in_output_and_logs(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    args: tuple[str, ...],
) -> None:
    _, rust = local_pair
    key = "fixture-rejected-key"
    cases = (
        f"https://fixture.invalid/private?api_key={key}",
        f"https:/fixture.invalid/private?api_key={key}",
        f"https:fixture.invalid/private?api_key={key}",
        f"wss:/fixture.invalid/{key}",
        f"endpoint=fixture.invalid/private?api_key={key}",
        f"//127.0.0.1/private?key={key}",
        f"//127.0.0.1/private?api_key={key}",
        f"https:/127.0.0.1/private?key={key}",
        *rejected_value_samples(),
    )
    loopback = (
        "--endpoint",
        "http://127.0.0.1:1/graphql",
        "--ws-endpoint",
        "ws://127.0.0.1:1/graphql",
        "--lcd-endpoint",
        "http://127.0.0.1:1",
    )
    for index, rejected in enumerate(cases):
        for flags, mode in [((), "text"), (("--json",), "json")]:
            root = tmp_path / f"{index}-{mode}"
            result = invoke(
                rust,
                root,
                *flags,
                *loopback,
                *(arg.format(rejected) for arg in args),
                logging="1",
            )
            assert result.returncode != 0, (index, mode)
            for marker in (key.encode(), b"rev6-property-secret"):
                assert marker not in result.stdout + result.stderr, (index, mode)
                for log in (root / "sai-home/logs").rglob("*"):
                    if log.is_file():
                        assert marker not in log.read_bytes(), (index, mode, log.name)


def rejected_value_samples() -> tuple[str, ...]:
    randomizer = random.Random(126)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789:/?&= ._'\"[]+-"
    return tuple(
        "".join(randomizer.choices(alphabet, k=12))
        + "key=rev6-property-secret"
        + "".join(randomizer.choices(alphabet, k=12))
        for _ in range(8)
    )


@pytest.mark.parametrize(
    "command",
    [
        ("config", "list"),
        ("auth", "status"),
        ("auth", "list"),
        ("market", "list"),
        ("market", "prices"),
        ("logs", "path"),
        ("mcp", "list"),
        ("mcp", "serve"),
    ],
)
def test_unexpected_flags_do_not_echo_generated_credentials(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    command: tuple[str, ...],
) -> None:
    _, rust = local_pair
    for index, value in enumerate(rejected_value_samples()):
        root = tmp_path / str(index)
        result = invoke(rust, root, *command, "--unexpected=" + value, logging="1")
        assert result.returncode == 2
        assert b"rev6-property-secret" not in result.stdout + result.stderr
        for path in (root / "sai-home/logs").rglob("*"):
            if path.is_file():
                assert b"rev6-property-secret" not in path.read_bytes()


@pytest.mark.parametrize(
    "args",
    [
        ("--json", "config", "set", "keeper.endpoint", "https://fixture.invalid:abc"),
        ("--json", "config", "set", "keeper.endpoint", "https://fixture.invalid:65536"),
        ("--json", "config", "set", "keeper.endpoint", "https://host space.invalid"),
        ("--json", "config", "set", "keeper.endpoint", "https://999.1.1.1"),
        ("--json", "config", "set", "keeper.ws_endpoint", "wss://fixture@localhost"),
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


def read_terminal(master: int, pending: bytearray, needle: bytes) -> None:
    import errno
    import select
    import time

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


def test_repl_keeps_terminal_history_and_recovers_from_interrupt(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    import pty

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
                read_terminal(master, pending, "❯".encode())
                os.write(master, b"discard-me\x03")
                read_terminal(master, pending, "❯".encode())
                os.write(master, b"config get keeper.endpoint\r")
                read_terminal(master, pending, b"https://sai-keeper.nibiru.fi/query")
                read_terminal(master, pending, "❯".encode())
                os.write(master, b"config get keeper.end")
                read_terminal(master, pending, b"point")
                os.write(master, b"\x1b[C\r")
                read_terminal(master, pending, b"https://sai-keeper.nibiru.fi/query")
                read_terminal(master, pending, "❯".encode())
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


def test_repl_history_matches_typed_prefix(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    import pty

    prompt = "❯".encode()
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
                read_terminal(master, pending, prompt)
                for command, output in (
                    (b"config get logs.enabled", b"false\r\n"),
                    (b"config get logs.keep_days", b"14\r\n"),
                    (
                        b"config get keeper.endpoint",
                        b"https://sai-keeper.nibiru.fi/query",
                    ),
                    (b"config get logs.\x1b[A\x1b[A\x1b[B", b"14\r\n"),
                    (
                        b"config get keeper.endpoint",
                        b"https://sai-keeper.nibiru.fi/query",
                    ),
                    (b"config get logs.\x1b[A", b"14\r\n"),
                    (
                        b"config get logs.\x1b[A --help",
                        b"Usage: sai config get [OPTIONS] KEY",
                    ),
                ):
                    os.write(master, command + b"\r")
                    read_terminal(master, pending, output)
                    read_terminal(master, pending, prompt)
                os.write(master, b"quit\r")
                assert process.wait(timeout=8) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                os.close(master)


def wait_for_open_path(
    process: subprocess.Popen[bytes],
    path: Path,
    master: int,
    pending: bytearray,
    command: bytes,
) -> None:
    import time

    fds = Path(f"/proc/{process.pid}/fd")
    if not fds.is_dir():
        read_terminal(master, pending, command)
        time.sleep(0.25)
        return
    deadline = time.monotonic() + 5
    while not any(fd.resolve() == path for fd in fds.iterdir()):
        assert time.monotonic() < deadline, path
        time.sleep(0.01)


def test_repl_interrupt_cancels_config_lock_wait_after_lookup(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    import fcntl
    import pty
    import signal
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread

    looked_up = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            response = b'{"data":{"userIntents":[]}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            looked_up.set()

        def log_message(self, format: str, *args: object) -> None:
            pass

    prompt = "❯".encode()
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for index, cli in enumerate(local_pair):
                looked_up.clear()
                root = tmp_path / str(index)
                home = root / "sai-home"
                home.mkdir(parents=True)
                lock_path = home / ".config.lock"
                master, slave = pty.openpty()
                endpoint = f"http://127.0.0.1:{server.server_port}/graphql"
                with subprocess.Popen(
                    [*cli, "--endpoint", endpoint],
                    cwd=root,
                    env={**case_env(root, logging="0"), "TERM": "xterm-256color"},
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                ) as process:
                    os.close(slave)
                    pending = bytearray()
                    try:
                        read_terminal(master, pending, prompt)
                        os.write(
                            master, f"logs intent {INTENT_KEY} --addr {ADDR}\r".encode()
                        )
                        assert looked_up.wait(timeout=8)
                        read_terminal(master, pending, b"No matching log events.")
                        read_terminal(master, pending, prompt)
                        with lock_path.open("a+b") as lock:
                            fcntl.flock(lock, fcntl.LOCK_EX)
                            command = b"config set logs.keep_days 30"
                            os.write(master, command + b"\r")
                            wait_for_open_path(
                                process, lock_path, master, pending, command
                            )
                            process.send_signal(signal.SIGINT)
                            read_terminal(master, pending, b"Interrupted")
                            read_terminal(master, pending, prompt)
                        assert not (home / "config.toml").exists()
                        os.write(master, b"config get logs.keep_days\r")
                        read_terminal(master, pending, b"14\r\n")
                        read_terminal(master, pending, prompt)
                        os.write(master, b"quit\r")
                        assert process.wait(timeout=8) == 0
                        assert not (home / "config.toml").exists()
                    finally:
                        if process.poll() is None:
                            process.kill()
                        os.close(master)
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize("mode", ["install", "source"])
def test_repl_interrupt_cancels_skill_lock_wait(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path, mode: str
) -> None:
    import fcntl
    import pty
    import signal

    _, rust = local_pair
    if mode == "install":
        parent = tmp_path / "skills"
        command = b"skill install --dir skills/sai"
        output = b"Installed the sai skill"
    else:
        parent = tmp_path / ".cache/sai/skills"
        command = b"skill source"
        output = b"Skill:"
    parent.mkdir(parents=True)
    prompt = "❯".encode()
    master, slave = pty.openpty()
    with subprocess.Popen(
        rust,
        cwd=tmp_path,
        env={**case_env(tmp_path, logging="0"), "TERM": "xterm-256color"},
        stdin=slave,
        stdout=slave,
        stderr=slave,
    ) as process:
        os.close(slave)
        pending = bytearray()
        try:
            read_terminal(master, pending, prompt)
            lock = os.open(parent, os.O_RDONLY)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                os.write(master, command + b"\r")
                wait_for_open_path(process, parent, master, pending, command)
                process.send_signal(signal.SIGINT)
                read_terminal(master, pending, b"Interrupted")
                read_terminal(master, pending, prompt)
            finally:
                os.close(lock)
            assert list(parent.iterdir()) == []
            os.write(master, command + b"\r")
            read_terminal(master, pending, output)
            read_terminal(master, pending, prompt)
            os.write(master, b"quit\r")
            assert process.wait(timeout=8) == 0
        finally:
            if process.poll() is None:
                process.kill()
            os.close(master)
    [target] = parent.iterdir()
    assert (target / "SKILL.md").read_bytes() == (SKILL / "SKILL.md").read_bytes()


@pytest.mark.parametrize("flags", [(), ("--endpoint", "http://127.0.0.1:1/graphql")])
def test_repl_commands_share_run_but_keep_command_ids(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    flags: tuple[str, ...],
) -> None:
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        result = invoke(
            cli,
            root,
            *flags,
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
        shell_command = None if flags else "repl"
        assert [row.get("command") for row in rows] == [
            shell_command,
            "config get",
            "config get",
            "config get",
            "config get",
            shell_command,
        ]
        ids = [row["commandId"] for row in rows]
        assert ids[0] == ids[-1] and ids[1] == ids[2] and ids[3] == ids[4]
        assert len(set(ids)) == 3
        assert len({row["runId"] for row in rows}) == 1
