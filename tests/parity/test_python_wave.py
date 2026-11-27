"""Check Rust CLI fixes against the Python CLI from the checkout."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from mcp_clients import SECRET, seed_clients
from scenarios import loopback


@pytest.fixture
def wave_clis(cli_pair: tuple[tuple[str, ...], tuple[str, ...]]) -> tuple[str, str]:
    return str(Path(sys.executable).with_name("sai")), cli_pair[1][0]


def run_cli(
    binary: str,
    home: Path,
    *args: str,
    overrides: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(home),
        "SAI_HOME": str(home / "sai"),
        "PATH": os.defpath,
        "SAI_UPDATE_AUTO": "false",
        "SAI_LOG": "false",
        "PYTHONUTF8": "1",
    }
    env.update(overrides or {})
    return subprocess.run(
        [binary, *args],
        cwd=home if cwd is None else cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )


def test_auth_logs_omit_rejected_root_operands(
    wave_clis: tuple[str, str], tmp_path: Path
) -> None:
    marker = "synthetic-rejected-auth-operand"
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        home.mkdir()
        result = run_cli(
            binary,
            home,
            "--",
            "--as",
            marker,
            "auth",
            "use",
            overrides={"SAI_LOG": "true"},
        )
        assert result.returncode == 2
        assert not result.stdout
        assert "Invalid arguments to auth." in result.stderr
        log = "".join(path.read_text() for path in (home / "sai/logs").glob("*.jsonl"))
        assert marker not in result.stderr + log
        events = [json.loads(line) for line in log.splitlines()]
        [start] = [event for event in events if event["event"] == "command.start"]
        assert start["argv"] == []


@pytest.mark.parametrize(
    "command",
    [("config", "list"), ("mcp", "serve"), ("doctor", "--offline", "--no-fix")],
    ids=["config", "mcp", "doctor"],
)
@pytest.mark.parametrize("flags", [(), ("--json",)], ids=["text", "json"])
def test_invalid_update_environment_exits_2(
    wave_clis: tuple[str, str],
    tmp_path: Path,
    command: tuple[str, ...],
    flags: tuple[str, ...],
) -> None:
    invalid = "synthetic-invalid-update-setting"
    message = "SAI_UPDATE_AUTO: Invalid value for update.auto: expected true or false"
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        home.mkdir()
        result = run_cli(
            binary, home, *flags, *command, overrides={"SAI_UPDATE_AUTO": invalid}
        )
        assert result.returncode == 2
        assert invalid not in result.stdout + result.stderr
        if flags:
            assert not result.stderr
            error = json.loads(result.stdout)["error"]
            assert error["code"] == "INVALID_REQUEST"
            assert message in error["message"]
        else:
            assert not result.stdout
            assert message in result.stderr


@pytest.mark.parametrize("dry_run", [(), ("--dry-run",)], ids=["apply", "dry-run"])
def test_install_reports_differing_claude_entry(
    wave_clis: tuple[str, str], tmp_path: Path, dry_run: tuple[str, ...]
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        home.mkdir()
        overrides = {"PATH": os.defpath}
        original = seed_clients(home, overrides, "retired")
        result = run_cli(
            binary,
            home,
            "--json",
            "mcp",
            "install",
            "claude",
            *dry_run,
            overrides=overrides,
        )
        assert (home / ".claude.json").read_bytes() == original[".claude.json"]
        assert not (home / "argv.jsonl").exists()
        assert result.returncode == 1
        assert SECRET not in result.stdout + result.stderr
        assert "token=private" not in result.stdout + result.stderr
        details = json.loads(result.stdout)["error"]["details"]
        assert details["reconnectNeeded"] is False
        [report] = details["results"]
        assert report["action"] == "failed"
        assert report["changed"] is False
        assert report["scope"] == "user"
        assert report["path"] == str(home / ".claude.json")
        assert "differs in command, args, env." in report["fix"]
        assert "test -f" in report["fix"]
        assert "claude mcp add --scope user sai" in report["fix"]


def test_doctor_validates_codex_repair_before_writing(
    wave_clis: tuple[str, str], tmp_path: Path
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        home.mkdir()
        overrides = {"PATH": os.defpath}
        original = seed_clients(home, overrides, "partial")
        config = home / ".codex/config.toml"
        before = config.stat()
        result = run_cli(
            binary, home, "--json", "doctor", "--offline", overrides=overrides
        )
        assert result.returncode == 1
        assert config.read_bytes() == original[".codex/config.toml"]
        after = config.stat()
        assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
            before.st_ino,
            before.st_mode,
            before.st_mtime_ns,
        )
        assert set(config.parent.iterdir()) == {config}
        calls = (home / "argv.jsonl").read_text()
        assert SECRET not in calls + result.stdout + result.stderr
        assert "token=private" not in calls + result.stdout + result.stderr
        previews = [
            call for line in calls.splitlines() if "-c" in (call := json.loads(line))
        ]
        assert len(previews) == 1
        assert 'mcp_servers.sai.args=["mcp", "serve"]' in previews[0]
        checks = json.loads(result.stdout)["error"]["details"]["checks"]
        [codex] = [check for check in checks if check["id"] == "mcp.codex"]
        assert codex["status"] == "fail"
        assert (
            "Codex rejected the proposed repair. The original Sai entry is unchanged."
            in codex["summary"]
        )
        assert "codex mcp add sai" in codex["fix"]
        assert "<original-launcher>" in codex["fix"]


@pytest.mark.parametrize("profile", ["config-only", "fake-ledger", "dedicated"])
@pytest.mark.parametrize("no_fix", [(), ("--no-fix",)], ids=["apply", "no-fix"])
def test_doctor_needs_trader_data_to_tighten_sai_home(
    wave_clis: tuple[str, str], tmp_path: Path, profile: str, no_fix: tuple[str, ...]
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        sai_home = home / "sai directory"
        sai_home.mkdir(parents=True, mode=0o755)
        config = sai_home / "config.toml"
        config.write_text('[unrelated]\nname = "fixture"\n')
        trader = sai_home / "trader"
        ledger_file = trader / "intents.sqlite3"
        if profile != "config-only":
            trader.mkdir(mode=0o755)
            with sqlite3.connect(ledger_file) as ledger:
                if profile == "dedicated":
                    ledger.execute(
                        "CREATE TABLE intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL,"
                        " account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,"
                        " entity TEXT NOT NULL)"
                    )
                else:
                    ledger.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        if profile == "fake-ledger":
            ledger_file.chmod(0o644)
            original_ledger = ledger_file.read_bytes()
        result = run_cli(
            binary,
            home,
            "--json",
            "doctor",
            "--offline",
            *no_fix,
            overrides={"SAI_HOME": str(sai_home)},
        )
        report = json.loads(result.stdout)
        checks = (
            report["error"]["details"]["checks"]
            if result.returncode
            else report["checks"]
        )
        [check] = [check for check in checks if check["id"] == "home"]
        assert config.read_text() == '[unrelated]\nname = "fixture"\n'
        if profile == "fake-ledger":
            assert ledger_file.read_bytes() == original_ledger
            assert stat.S_IMODE(ledger_file.stat().st_mode) == 0o644
            [intents] = [check for check in checks if check["id"] == "intents"]
            assert intents["status"] == "fail"
        if profile == "dedicated" and not no_fix:
            assert stat.S_IMODE(sai_home.stat().st_mode) == 0o700
            assert stat.S_IMODE(trader.stat().st_mode) == 0o700
            assert check["status"] == "fixed"
        else:
            assert stat.S_IMODE(sai_home.stat().st_mode) == 0o755
            assert check["status"] == "warn"
            if profile == "dedicated":
                assert check["fix"].startswith(f"chmod go-rwx '{sai_home}'; ")
                assert stat.S_IMODE(trader.stat().st_mode) == 0o755
            else:
                assert "moving the profile to a dedicated directory" in check["fix"]


@pytest.mark.parametrize("writable", [True, False], ids=["writable", "read-only"])
def test_doctor_preserves_shared_trader_modes_during_migration(
    wave_clis: tuple[str, str], tmp_path: Path, writable: bool
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        trader = home / "sai/trader"
        trader.mkdir(parents=True)
        trader.chmod(0o755)
        notes = trader / "notes.txt"
        notes.write_text("another application")
        path = trader / "intents.sqlite3"
        with sqlite3.connect(path) as ledger:
            ledger.execute(
                "CREATE TABLE intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL,"
                " account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,"
                " entity TEXT NOT NULL)"
            )
        mode = 0o644 if writable else 0o400
        path.chmod(mode)
        before = path.read_bytes()

        result = run_cli(binary, home, "--json", "doctor", "--offline")
        report = json.loads(result.stdout)
        checks = report if writable else report["error"]["details"]
        by_id = {check["id"]: check for check in checks["checks"]}
        assert by_id["home"]["status"] == "warn"
        assert "dedicated directory" in by_id["home"]["fix"]
        if not writable:
            assert stat.S_IMODE(path.stat().st_mode) == mode
        assert stat.S_IMODE(trader.stat().st_mode) == 0o755
        assert notes.read_text() == "another application"
        if writable:
            assert stat.S_IMODE(path.stat().st_mode) == mode
            assert result.returncode == 0
            assert by_id["intents"]["status"] == "fixed"
            assert by_id["intents"]["details"]["userVersion"] == 1
        else:
            assert result.returncode == 1
            assert by_id["intents"]["status"] == "fail"
            assert path.read_bytes() == before


def test_doctor_lcd_keeps_the_query_after_the_node_info_path(
    wave_clis: tuple[str, str], tmp_path: Path
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        home.mkdir()
        with loopback("normal", lcd_query=True) as endpoints:
            result = run_cli(
                binary,
                home,
                "--json",
                "doctor",
                "--no-fix",
                overrides={
                    "SAI_ENDPOINT": endpoints.keeper,
                    "SAI_WS_ENDPOINT": endpoints.ws,
                    "SAI_LCD_ENDPOINT": endpoints.http + "/?project=fixture",
                },
            )
            assert result.returncode == 0, result.stdout
            [lcd] = [
                check
                for check in json.loads(result.stdout)["checks"]
                if check["id"] == "lcd"
            ]
            assert lcd["status"] == "pass"
            assert sum(request.label == "node_info" for request in endpoints.wire) == 1
            assert not endpoints.errors
            assert "project=fixture" not in result.stdout + result.stderr


@pytest.mark.parametrize("logging", ["true", "false"])
@pytest.mark.parametrize("no_fix", [(), ("--no-fix",)], ids=["apply", "no-fix"])
def test_doctor_preserves_foreign_logs(
    wave_clis: tuple[str, str], tmp_path: Path, logging: str, no_fix: tuple[str, ...]
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        root = home / "sai/logs"
        root.mkdir(parents=True, mode=0o755)
        (root.parent / "config.toml").write_text('[unrelated]\napp = "notes"\n')
        notes = root / "notes.txt"
        notes.write_text("Other application data\n")
        result = run_cli(
            binary,
            home,
            "--json",
            "doctor",
            "--offline",
            *no_fix,
            overrides={"SAI_LOG": logging},
        )
        assert result.returncode == 0
        assert stat.S_IMODE(root.stat().st_mode) == 0o755
        assert list(root.iterdir()) == [notes]
        [logs] = [
            check
            for check in json.loads(result.stdout)["checks"]
            if check["id"] == "logs"
        ]
        assert logs["status"] == "warn"
        assert "moving the profile to a dedicated directory" in logs["fix"]


@pytest.mark.parametrize("command", [("config", "list"), ("mcp", "serve")])
def test_invalid_update_override_wins_over_invalid_file_value(
    wave_clis: tuple[str, str], tmp_path: Path, command: tuple[str, ...]
) -> None:
    private = "synthetic-invalid-update-value"
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        (home / "sai").mkdir(parents=True)
        config = home / "sai/config.toml"
        config.write_text('[update]\nauto = "bad-file-value"\n')
        result = run_cli(
            binary, home, "--json", *command, overrides={"SAI_UPDATE_AUTO": private}
        )
        assert result.returncode == 2
        assert private not in result.stdout + result.stderr
        message = json.loads(result.stdout)["error"]["message"]
        assert message.endswith(
            "SAI_UPDATE_AUTO: Invalid value for update.auto: expected true or false"
        )
        assert str(config) not in message
        valid = run_cli(binary, home, "--json", *command)
        assert valid.returncode == 0


def test_doctor_reports_invalid_update_override_before_invalid_file_value(
    wave_clis: tuple[str, str], tmp_path: Path
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        (home / "sai").mkdir(parents=True)
        config = home / "sai/config.toml"
        invalid_file = '[update]\nauto = "bad-file-value"\n'
        config.write_text(invalid_file)

        command = ("--json", "doctor", "--offline", "--no-fix")
        result = run_cli(
            binary, home, *command, overrides={"SAI_UPDATE_AUTO": "bad-override"}
        )
        assert result.returncode == 2
        [check] = [
            check
            for check in json.loads(result.stdout)["error"]["details"]["checks"]
            if check["id"] == "config"
        ]
        assert check["summary"] == (
            "SAI_UPDATE_AUTO: Invalid value for update.auto: expected true or false"
        )
        assert config.read_text() == invalid_file


def test_repl_omits_rejected_auth_and_imports_from_history(
    wave_clis: tuple[str, str], tmp_path: Path
) -> None:
    import pty

    from test_local_commands import read_terminal

    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        home.mkdir()
        master, slave = pty.openpty()
        with subprocess.Popen(
            [binary],
            cwd=home,
            env={
                "HOME": str(home),
                "SAI_HOME": str(home / "sai"),
                "SAI_LOG": "false",
                "SAI_UPDATE_AUTO": "false",
                "PATH": os.defpath,
                "TERM": "xterm-256color",
            },
            stdin=slave,
            stdout=slave,
            stderr=slave,
        ) as process:
            os.close(slave)
            pending = bytearray()
            prompt = "❯".encode()
            try:
                read_terminal(master, pending, prompt)
                for line, message in (
                    (
                        b"auth import synthetic-repl-private-value",
                        b"Unexpected arguments",
                    ),
                    (b"auth use synthetic-rejected-account", b"Requested account"),
                    (b"auth import --help", b"Usage:"),
                    (b"auth use --help synthetic-unused-help-operand", b"Usage:"),
                    (b"auth list", b"accounts"),
                ):
                    os.write(master, line + b"\r")
                    read_terminal(master, pending, message)
                    read_terminal(master, pending, prompt)
                os.write(master, b"\x04")
                assert process.wait(timeout=8) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                os.close(master)
        history = (home / "sai/trader/history").read_text()
        assert "synthetic-" not in history
        assert "auth import" not in history
        assert "+auth list\n" in history


@pytest.mark.parametrize("exists", [False, True], ids=["missing", "non-executable"])
def test_mcp_install_rejects_unusable_absolute_launcher(
    wave_clis: tuple[str, str], tmp_path: Path, exists: bool
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        home.mkdir()
        overrides = {"PATH": os.defpath}
        original = seed_clients(home, overrides, "current")
        launcher = home / "unusable-launcher"
        if exists:
            launcher.touch(mode=0o600)
        result = run_cli(
            binary,
            home,
            "--json",
            "mcp",
            "install",
            "cursor",
            "--",
            str(launcher),
            "mcp",
            "serve",
            overrides=overrides,
        )
        error = json.loads(result.stdout)["error"]
        if index == 0:
            assert result.returncode == 1
            [cursor] = error["details"]["results"]
            assert (
                cursor["error"]
                == "The requested server launcher is missing or not executable"
            )
            assert cursor["changed"] is False
        else:
            assert result.returncode == 2
            assert error["code"] == "INVALID_REQUEST"
            assert str(launcher) not in result.stdout + result.stderr
        assert (home / ".cursor/mcp.json").read_bytes() == original[".cursor/mcp.json"]


@pytest.mark.parametrize(
    "launcher",
    [
        "OPENAI_API_KEY=synthetic-private",
        "--endpoint=https://fixture.invalid/path?value=synthetic-private",
        "-synthetic-private",
        "synthetic-private-missing-launcher",
    ],
    ids=["assignment", "endpoint-option", "dash-prefix", "missing-on-path"],
)
def test_mcp_install_rejects_explicit_launcher_before_reporting(
    wave_clis: tuple[str, str], tmp_path: Path, launcher: str
) -> None:
    _, rust = wave_clis
    overrides = {"PATH": os.defpath}
    seed_clients(tmp_path, overrides, "empty")

    for options in (("claude", "--dry-run"), ("--dry-run",), ("claude",)):
        result = run_cli(
            rust,
            tmp_path,
            "--json",
            "mcp",
            "install",
            *options,
            "--",
            launcher,
            "mcp",
            "serve",
            overrides=overrides,
        )
        assert result.returncode == 2
        assert "synthetic-private" not in result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert set(report) == {"error"}
        assert report["error"]["code"] == "INVALID_REQUEST"
        assert "existing executable" in report["error"]["message"]
        assert "details" not in report["error"]
        assert not (tmp_path / ".claude.json").exists()

    text = run_cli(
        rust,
        tmp_path,
        "mcp",
        "install",
        "claude",
        "--dry-run",
        "--",
        launcher,
        overrides=overrides,
    )
    assert text.returncode == 2
    assert not text.stdout
    assert "synthetic-private" not in text.stderr
    assert "existing executable" in text.stderr


@pytest.mark.parametrize(
    "relative", ["slot=staging/launcher", "bin/-launcher", "bin/slot=staging"]
)
def test_mcp_install_accepts_resolved_explicit_launcher(
    wave_clis: tuple[str, str], tmp_path: Path, relative: str
) -> None:
    _, rust = wave_clis
    overrides = {"PATH": os.defpath}
    seed_clients(tmp_path, overrides, "empty")
    executable = tmp_path / relative
    executable.parent.mkdir(exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    launcher = (
        executable.name if executable.parent == tmp_path / "bin" else str(executable)
    )
    path = tmp_path / ".cursor/mcp.json"
    for options in (("--dry-run",), ()):
        result = run_cli(
            rust,
            tmp_path,
            "--json",
            "mcp",
            "install",
            "cursor",
            *options,
            "--",
            launcher,
            "mcp",
            "serve",
            overrides=overrides,
        )
        assert result.returncode == 0
        [report] = json.loads(result.stdout)["results"]
        assert report["command"] == [
            launcher,
            "<argument hidden 1 of 2>",
            "<argument hidden 2 of 2>",
        ]
        if options:
            assert not path.exists()
        else:
            entry = json.loads(path.read_text())["mcpServers"]["sai"]
            assert entry["command"] == launcher
            assert entry["args"] == ["mcp", "serve"]


@pytest.mark.parametrize(
    ("launcher_name", "operands"),
    [
        (
            "docker",
            ("run", "-ieDATABASE_URL=postgresql://fixture:synthetic-private@host/db"),
        ),
        ("/bin/sh", ("-c", "exec curl -H 'Authorization: Bearer synthetic-private'")),
        (
            "/usr/bin/env",
            (
                "SESSION_COOKIE=synthetic-private-cookie",
                "APP_MODE=synthetic-private-mode",
                "sai",
                "mcp",
                "serve",
            ),
        ),
        (
            "docker",
            ("run", "--env=OPENAI_API_KEY=synthetic-private=key"),
        ),
        (
            "docker",
            (
                "run",
                "--env",
                "APP_LABEL=synthetic-private-label",
                "-e",
                "TERM=synthetic-private-term",
                "-e=REGION=synthetic-private-region",
                "-eAPP.MODE=synthetic-private-mode",
                "--env",
                "PASSTHROUGH",
            ),
        ),
        (
            "sai",
            (
                "mcp",
                "serve",
                "--token=synthetic-private-token",
                "--CLIENT_SECRET",
                "synthetic-private-client-secret",
                "--api-key",
                "-synthetic-private-key",
                "--bearer-token=https://fixture.invalid/synthetic-private",
                "--header=Authorization: Bearer synthetic-private-header",
                "--refreshToken",
                "synthetic-private-refresh-token",
                "--endpoint",
                "https://fixture.invalid/path?value=synthetic-private",
                "--endpoint=https://fixture.invalid/path?value=synthetic-private",
                "--label",
                "public-label",
            ),
        ),
        ("sai", ()),
    ],
    ids=[
        "clustered-short-option",
        "shell-command",
        "bare-assignments",
        "inline-env-option",
        "generic-env-options",
        "explicit-sai",
        "launcher-only",
    ],
)
def test_mcp_install_hides_every_user_launcher_argument(
    wave_clis: tuple[str, str],
    tmp_path: Path,
    launcher_name: str,
    operands: tuple[str, ...],
) -> None:
    _, rust = wave_clis
    overrides = {"PATH": os.defpath}
    seed_clients(tmp_path, overrides, "empty")
    launcher = launcher_name
    if launcher_name == "docker":
        docker = tmp_path / "bin/docker"
        docker.write_text("#!/bin/sh\nexit 0\n")
        docker.chmod(0o700)
        launcher = str(docker)
    if launcher_name == "sai":
        launcher = str(tmp_path / "bin/sai")
    prefix = ("--json", "mcp", "install", "claude")
    command = (launcher, *operands)
    expected = [
        launcher,
        *(
            f"<argument hidden {i} of {len(operands)}>"
            for i in range(1, len(operands) + 1)
        ),
    ]
    for flags in (("--dry-run",), ()):
        result = run_cli(
            rust, tmp_path, *prefix, *flags, "--", *command, overrides=overrides
        )
        assert result.returncode == 0
        assert "synthetic-private" not in result.stdout + result.stderr
        [report] = json.loads(result.stdout)["results"]
        assert report["command"] == expected
        assert report["argv"][0][-len(expected) :] == expected
        assert "--env=SAI_HOME=[redacted]" in report["argv"][0]
        if flags:
            assert not (tmp_path / ".claude.json").exists()

    entry = json.loads((tmp_path / ".claude.json").read_text())["mcpServers"]["sai"]
    assert entry["command"] == launcher
    assert entry.get("args", []) == list(operands)


@pytest.mark.parametrize("custom", [False, True], ids=["generated", "no-client"])
def test_mcp_install_reports_only_generated_serve_arguments(
    wave_clis: tuple[str, str], tmp_path: Path, custom: bool
) -> None:
    _, rust = wave_clis
    overrides = {"PATH": os.defpath}
    seed_clients(tmp_path, overrides, "empty")
    launcher = str(tmp_path / "bin/sai")
    args = (
        ("--", launcher, "mcp", "serve", "synthetic-private") if custom else ("claude",)
    )
    result = run_cli(
        rust,
        tmp_path,
        "--json",
        "mcp",
        "install",
        "--dry-run",
        *args,
        overrides=overrides,
    )
    assert result.returncode == 0
    assert "synthetic-private" not in result.stdout + result.stderr
    report = json.loads(result.stdout)
    if custom:
        assert report["command"] == [
            launcher,
            "<argument hidden 1 of 3>",
            "<argument hidden 2 of 3>",
            "<argument hidden 3 of 3>",
        ]
    else:
        [report] = report["results"]
        assert report["command"] == [launcher, "mcp", "serve"]
        assert report["argv"][0][-3:] == [launcher, "mcp", "serve"]


@pytest.mark.parametrize("flags", [(), ("--json",)], ids=["text", "json"])
def test_noninteractive_mcp_install_listing_hides_launcher_arguments(
    wave_clis: tuple[str, str], tmp_path: Path, flags: tuple[str, ...]
) -> None:
    _, rust = wave_clis
    overrides = {"PATH": os.defpath}
    original = seed_clients(tmp_path, overrides, "current")
    launcher = "/bin/sh"
    args = ["-c", "APP_MODE=synthetic-private-wrapper exec sai mcp serve"]
    path = tmp_path / ".cursor/mcp.json"
    config = json.loads(path.read_bytes())
    config["mcpServers"]["sai"].update(command=launcher, args=args)
    original[".cursor/mcp.json"] = json.dumps(config).encode()
    path.write_bytes(original[".cursor/mcp.json"])

    hidden = ["<argument hidden 1 of 2>", "<argument hidden 2 of 2>"]
    for options in ((), ("--dry-run",)):
        result = run_cli(
            rust,
            tmp_path,
            *flags,
            "mcp",
            "install",
            *options,
            "--",
            launcher,
            *args,
            overrides=overrides,
        )
        assert result.returncode == 0
        assert "synthetic-private-wrapper" not in result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["command"] == [launcher, *hidden]
        clients = report["clients"]
        assert {row["client"] for row in clients} == {"claude", "codex", "cursor"}
        for row in clients:
            matches = row["client"] == "cursor"
            shown = [launcher if matches else "sai", *hidden]
            assert row["current"] is matches
            assert row["registeredCommand"] == shown
            assert row["registrations"]
            assert all(entry["command"] == shown for entry in row["registrations"])
        for relative, source in original.items():
            assert (tmp_path / relative).read_bytes() == source


@pytest.mark.parametrize("flags", [(), ("--json",)], ids=["text", "json"])
def test_mcp_reports_sai_environment_names_and_counts_other_entries(
    wave_clis: tuple[str, str], tmp_path: Path, flags: tuple[str, ...]
) -> None:
    _, rust = wave_clis
    overrides = {"PATH": os.defpath}
    seed_clients(tmp_path, overrides, "current")
    marker = "synthetic-private-env"
    bare_token = "fixtureBareToken_71"
    codex = tmp_path / ".codex/config.toml"
    forwarded = [
        f"OPENAI_API_KEY={marker}=suffix",
        "OPENAI_API_KEY",
        f"invalid name {marker}",
        f"={marker}",
        bare_token,
        bare_token,
        f"SAI_LCD_ENDPOINT={marker}",
        "SAI_LCD_ENDPOINT",
    ]
    codex.write_text(
        codex.read_text().replace(
            'args = ["mcp", "serve"]',
            f'args = ["mcp", "serve"]\nenv_vars = {json.dumps(forwarded)}',
        )
    )
    paths = [codex, tmp_path / ".claude.json", tmp_path / ".cursor/mcp.json"]
    for path in paths[1:]:
        config = json.loads(path.read_bytes())
        config["mcpServers"]["sai"]["env"].update(
            {
                f"APP_TOKEN={marker}": marker,
                "APP_MODE": marker,
                bare_token: marker,
                f"SAI_ENDPOINT={marker}": marker,
            }
        )
        path.write_text(json.dumps(config))
    original = {path: path.read_bytes() for path in paths}

    launcher = str(tmp_path / "bin/sai")
    for invocation in (
        ("list",),
        ("install", "--", launcher, "mcp", "serve"),
        ("install", "--dry-run", "--", launcher, "mcp", "serve"),
    ):
        result = run_cli(
            rust, tmp_path, *flags, "mcp", *invocation, overrides=overrides
        )
        assert result.returncode == 0
        assert marker not in result.stdout + result.stderr
        assert bare_token not in result.stdout + result.stderr
        clients = json.loads(result.stdout)["clients"]
        assert {row["client"] for row in clients} == {"claude", "codex", "cursor"}
        for row in clients:
            expected = (
                ["SAI_LCD_ENDPOINT", "6 other entries"]
                if row["client"] == "codex"
                else ["SAI_ENDPOINT", "3 other entries"]
            )
            assert not row["current"]
            assert row["staleEnv"] == expected
            assert row["registrations"]
            assert all(entry["staleEnv"] == expected for entry in row["registrations"])
        assert {path: path.read_bytes() for path in paths} == original


@pytest.mark.parametrize("client", ["claude", "codex", "cursor"])
@pytest.mark.parametrize("launcher_style", ["assignment", "unresolved", "plain-path"])
def test_saved_mcp_registrations_hide_non_path_launchers_and_arguments(
    wave_clis: tuple[str, str], tmp_path: Path, client: str, launcher_style: str
) -> None:
    _, rust = wave_clis
    overrides = {"PATH": os.defpath}
    seed_clients(tmp_path, overrides, "current")
    launcher = str(tmp_path / "bin/sai")
    saved_launchers = {
        "assignment": "OPENAI_API_KEY=synthetic-private-launcher",
        "unresolved": "synthetic-private-bare-token",
        "plain-path": launcher,
    }
    command = saved_launchers[launcher_style]
    args = ["--env", "APP_SETTING=synthetic-private-operand"]
    path = (
        tmp_path
        / {
            "claude": ".claude.json",
            "codex": ".codex/config.toml",
            "cursor": ".cursor/mcp.json",
        }[client]
    )
    if client == "codex":
        source = path.read_text().replace(
            f"command = {json.dumps(launcher)}", f"command = {json.dumps(command)}"
        )
        path.write_text(
            source.replace('args = ["mcp", "serve"]', f"args = {json.dumps(args)}")
        )
    else:
        config = json.loads(path.read_text())
        config["mcpServers"]["sai"].update(command=command, args=args)
        path.write_text(json.dumps(config))
    original = path.read_bytes()

    visible = launcher_style == "plain-path"
    hidden = len(args) + (not visible)
    expected = [
        *(["sai"] if visible else []),
        *(f"<argument hidden {i} of {hidden}>" for i in range(1, hidden + 1)),
    ]
    for invocation in (
        ("list",),
        ("install", "--dry-run", "--", launcher, "mcp", "serve"),
    ):
        result = run_cli(
            rust, tmp_path, "--json", "mcp", *invocation, overrides=overrides
        )
        assert result.returncode == 0
        assert "synthetic-private" not in result.stdout + result.stderr
        clients = json.loads(result.stdout)["clients"]
        assert {row["client"] for row in clients} == {"claude", "codex", "cursor"}
        for row in clients:
            displayed = (
                expected
                if row["client"] == client
                else [
                    launcher,
                    "<argument hidden 1 of 2>",
                    "<argument hidden 2 of 2>",
                ]
            )
            assert row["registeredCommand"] == displayed
            assert row["registrations"]
            for registration in row["registrations"]:
                assert registration["command"] == displayed
        assert path.read_bytes() == original


def test_mcp_install_legacy_report_hides_saved_launcher_starting_with_dash(
    wave_clis: tuple[str, str], tmp_path: Path
) -> None:
    _, rust = wave_clis
    overrides = {"PATH": os.defpath}
    seed_clients(tmp_path, overrides, "legacy-retired")
    path = tmp_path / ".cursor/mcp.json"
    config = json.loads(path.read_text())
    config["mcpServers"]["sai" + "-mcp"].update(
        command="-synthetic-private-launcher",
        args=["APP_SETTING=synthetic-private-operand"],
    )
    path.write_text(json.dumps(config))
    original = path.read_bytes()
    result = run_cli(
        rust,
        tmp_path,
        "--json",
        "mcp",
        "install",
        "cursor",
        "--dry-run",
        overrides=overrides,
    )
    assert result.returncode == 1
    assert "synthetic-private" not in result.stdout + result.stderr
    [row] = json.loads(result.stdout)["error"]["details"]["results"]
    assert row["command"] == ["<argument hidden 1 of 2>", "<argument hidden 2 of 2>"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", ["cwd", "remote"])
def test_mcp_install_reports_claude_extra_fields(
    wave_clis: tuple[str, str], tmp_path: Path, kind: str
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        home.mkdir()
        overrides = {"PATH": os.defpath}
        seed_clients(home, overrides, "current")
        config = home / ".claude.json"
        data = json.loads(config.read_text())
        entry = data["mcpServers"]["sai"]
        if kind == "cwd":
            entry["cwd"] = "synthetic-old-working-directory"
        else:
            data["mcpServers"]["sai"] = {
                "type": "http",
                "url": "https://fixture.invalid/private-path?token=synthetic-remote-value",
                "headers": {"Authorization": SECRET},
            }
        config.write_text(json.dumps(data))
        original = config.read_bytes()
        result = run_cli(
            binary, home, "--json", "mcp", "install", "claude", overrides=overrides
        )
        assert result.returncode == 1
        assert config.read_bytes() == original
        [report] = json.loads(result.stdout)["error"]["details"]["results"]
        assert report["changed"] is False
        if kind == "cwd":
            assert "differs in cwd." in report["fix"]
        else:
            assert all(field in report["fix"] for field in ["url", "headers", "type"])
            assert SECRET not in result.stdout + result.stderr
            assert "synthetic-remote-value" not in result.stdout + result.stderr
            if index == 1:
                assert "http at https://fixture.invalid" in report["fix"]
                assert "claude mcp add" not in report["fix"]


@pytest.mark.parametrize("flags", [(), ("--json",)], ids=["text", "json"])
def test_mcp_install_dry_run_masks_profile_in_claude_repair_hint(
    wave_clis: tuple[str, str], tmp_path: Path, flags: tuple[str, ...]
) -> None:
    _, rust = wave_clis
    overrides = {"PATH": os.defpath}
    seed_clients(tmp_path, overrides, "current")
    config = tmp_path / ".claude.json"
    data = json.loads(config.read_text())
    entry = data["mcpServers"]["sai"]
    profile = str(tmp_path / "private-profile")
    entry["env"]["SAI_HOME"] = profile
    overrides["SAI_HOME"] = profile
    entry["args"] = ["mcp", "serve", "--endpoint", "https://fixture.invalid/api"]
    config.write_text(json.dumps(data))
    original = config.read_bytes()
    for options in (("--dry-run",), ()):
        result = run_cli(
            rust,
            tmp_path,
            *flags,
            "mcp",
            "install",
            "claude",
            *options,
            overrides=overrides,
        )
        assert result.returncode == 1
        output = result.stdout + result.stderr
        if options:
            assert profile not in output
            assert "--env=SAI_HOME=<original-SAI_HOME>" in output
        else:
            assert f"--env=SAI_HOME={profile}" in output
        assert config.read_bytes() == original


@pytest.mark.parametrize("scope", ["user", "local"])
def test_claude_install_uses_checkout_key_for_disablement(
    wave_clis: tuple[str, str], tmp_path: Path, scope: str
) -> None:
    for index, binary in enumerate(wave_clis):
        home = tmp_path / str(index)
        checkout = home / "checkout"
        child = checkout / "nested"
        child.mkdir(parents=True)
        subprocess.run(
            ["git", "-C", str(checkout), "init", "--quiet"],
            env={"HOME": str(home), "PATH": os.defpath},
            check=True,
            capture_output=True,
            timeout=5,
        )
        overrides = {"PATH": os.defpath}
        seed_clients(home, overrides, "current")
        config = home / ".claude.json"
        data = json.loads(config.read_text())
        data["projects"] = {
            str(checkout): {
                "disabledMcpServers": ["sai"],
                "mcpServers": data["mcpServers"],
            }
        }
        config.write_text(json.dumps(data))
        original = config.read_bytes()
        result = run_cli(
            binary,
            home,
            "--json",
            "mcp",
            "install",
            "claude",
            "--scope",
            scope,
            overrides=overrides,
            cwd=child,
        )
        assert result.returncode == 1
        assert config.read_bytes() == original
        assert not (home / "argv.jsonl").exists()
        [report] = json.loads(result.stdout)["error"]["details"]["results"]
        assert f"disabled the {scope} sai registration" in report["error"]
