"""Compare Rust and Python auth using isolated tools and loopback Keeper."""

from __future__ import annotations

import base64
import fcntl
import inspect
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sai_cli.core import keeper, storage
from sai_cli.tests.test_auth_connect import (
    ADDR,
    OTHER,
    UD_SECRET,
    Env,
    fake_cast,
    fake_ud,
    fixture_exchange,
    fixture_signature,
    keystore,
    typed_data,
)
from sai_cli.tests.test_auth_connect import keeper_auth as keeper_auth  # noqa: PLC0414

CLI = tuple[str, ...]


@pytest.fixture
def auth_pair(cli_pair: tuple[CLI, CLI]) -> tuple[CLI, CLI]:
    path = os.environ.get("SAI_PARITY_AUTH_PYTHON")
    if path is None:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("native parity CI must supply SAI_PARITY_AUTH_PYTHON")
        pytest.skip("set SAI_PARITY_AUTH_PYTHON to the Python executable from main")
    executable = shutil.which(path)
    if executable is None:
        pytest.fail("SAI_PARITY_AUTH_PYTHON must name an executable")
    return (executable,), cli_pair[1]


@pytest.fixture
def renewal_pair(cli_pair: tuple[CLI, CLI]) -> tuple[CLI, CLI]:
    # Renewal output changes in both implementations, so use the Python checkout.
    return (str(Path(sys.executable).with_name("sai")),), cli_pair[1]


def homes(root: Path, commands: tuple[CLI, CLI]) -> tuple[Env, Env]:
    result = []
    for name, command in zip(("python", "rust"), commands, strict=True):
        path = root / name
        path.mkdir()
        result.append(Env(path, command))
    return result[0], result[1]


def observed(result: subprocess.CompletedProcess[str]) -> tuple[int, str, str]:
    return result.returncode, result.stdout, result.stderr


def session_state(env: Env) -> dict[str, Any]:
    path = env.home / "trader" / "sessions.json"
    if not path.exists():
        return {"active": None, "sessions": {}}
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    return storage._decrypt(json.loads(path.read_text()), storage._AAD)


def fixture_token(addr: str, expiry: float = 4070908800) -> str:
    claims = {"sub": addr, "exp": expiry, "purpose": "user_intent"}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return f"fixture.{encoded.rstrip('=')}.signature"


def assert_unexposed(env: Env, output: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in output
        assert secret.encode() not in env.stored_bytes()


@pytest.mark.parametrize(
    "args",
    [
        ("auth", "login", "--help"),
        ("auth", "login"),
        ("auth", "use"),
        ("--json", "auth", "login"),
        ("--json", "auth", "login", "--addr"),
        ("--json", "auth", "login", "--account", "dev"),
    ],
)
def test_auth_usage(tmp_path: Path, auth_pair: tuple[CLI, CLI], args: CLI) -> None:
    python, rust = homes(tmp_path, auth_pair)
    expected = observed(python.run(*args))
    assert expected[0] == (0 if "--help" in args else 2)
    assert observed(rust.run(*args)) == expected


@pytest.mark.parametrize(
    ("case", "expected_code", "challenges", "exchanges"),
    [
        ("cast", 0, 1, 1),
        ("ud", 0, 1, 1),
        ("ud-inaccessible", 0, 1, 1),
        ("missing", 1, 0, 0),
        ("mismatch", 1, 0, 0),
        ("collision", 1, 0, 0),
        ("address-disambiguates", 0, 1, 1),
        ("unavailable", 1, 0, 0),
        ("substituted", 1, 1, 0),
        ("empty-domain", 1, 1, 0),
        ("wrong-challenge-address", 1, 1, 0),
        ("wrong-typed-address", 1, 1, 0),
        ("wrong-typed-nonce", 1, 1, 0),
        ("malformed-signature", 1, 1, 0),
        ("keeper-rejects-signature", 1, 1, 1),
        ("expired-challenge", 1, 1, 1),
        ("wrong-typed-chain", 1, 1, 1),
        ("ud-locked", 3, 1, 0),
        ("terminal-fallback", 0, 2, 1),
        ("rotated-keystore", 1, 1, 0),
        ("wrong-session-address", 1, 1, 1),
    ],
)
def test_auth_login(
    tmp_path: Path,
    auth_pair: tuple[CLI, CLI],
    keeper_auth: dict[str, Any],
    case: str,
    expected_code: int,
    challenges: int,
    exchanges: int,
) -> None:
    python, rust = homes(tmp_path, auth_pair)
    if case == "ud-locked":
        # Use a path whose non-interactive recovery hint must keep the URL private.
        keeper_auth["endpoint"] = keeper_auth["endpoint"].replace(
            "/query", "/fixture-path-key/query"
        )
    addr = OTHER if case == "wrong-session-address" else ADDR
    chain_id = 6900 if case in {"empty-domain", "wrong-typed-chain"} else None
    keeper_auth["typed"] = typed_data(chain_id=chain_id, message__account=addr)
    if case == "substituted":
        keeper_auth["typed"]["primaryType"] = "Permit"
    if case == "empty-domain":
        keeper_auth["typed"]["domain"]["chainId"] = None
    if case == "wrong-challenge-address":
        keeper_auth["challenge_addr"] = OTHER
    if case == "wrong-typed-address":
        keeper_auth["typed"]["message"]["account"] = OTHER
    if case == "wrong-typed-nonce":
        keeper_auth["typed"]["message"]["challenge"] = "0x" + "34" * 32
    if case == "expired-challenge":
        keeper_auth["challenge_expires_at"] = "2000-01-01T00:00:00Z"
    if case == "wrong-typed-chain":
        keeper_auth["typed"]["domain"]["chainId"] = 6901
    signer_output = {
        "malformed-signature": "0x0",
        "keeper-rejects-signature": "0x" + "1" * 130,
    }.get(case)
    args = ["--json", "--endpoint", keeper_auth["endpoint"], "auth", "login", "dev"]
    if case == "mismatch":
        args.extend(["--addr", OTHER])
    elif case == "address-disambiguates":
        args.extend(["--addr", ADDR])
    results = []
    for env in (python, rust):
        if case != "missing":
            fake_cast(env, diagnostics=case == "cast", signature=signer_output)
            keystore(env, addr=None if case == "cast" else addr)
        if case in {"ud", "ud-locked", "terminal-fallback", "rotated-keystore"}:
            fake_ud(env, locked=case != "ud", rotate=case == "rotated-keystore")
        elif case == "ud-inaccessible":
            fake_ud(env)
            tool = env.bin / "ud"
            tool.chmod(0o001)
            if os.access(tool, os.X_OK):
                pytest.skip("fixture requires an invoking user without execute access")
        elif case == "unavailable":
            fake_ud(env, down=True)
        elif case in {"collision", "address-disambiguates"}:
            fake_ud(env, state="name-conflict", addr=OTHER)
        keeper_auth["requests"].clear()
        if case in {"terminal-fallback", "rotated-keystore"}:
            result = env.run_tty(*args, typed=[])
        else:
            result = observed(env.run(*args))
        assert result[0] == expected_code
        if case == "ud-locked" and env is rust:
            origin = keeper_auth["endpoint"].split("/fixture-path-key")[0]
            location = f" at {origin}"
            assert location in json.loads(result[1])["error"]["hint"]
            # The origin-only diagnostic includes context alongside the URL variable.
            result = (result[0], result[1].replace(location, ""), result[2])
        results.append(result)
        if case == "cast":
            assert result[2] == "Fixture Cast address\nFixture Cast sign\n"
        lookup_addr = (
            addr.upper().replace("0X", "0x")
            if case in {"ud", "ud-locked", "terminal-fallback", "rotated-keystore"}
            else addr
        )
        expected = [
            {"query": keeper.CHALLENGE, "variables": {"address": lookup_addr}}
        ] * challenges
        private = [keeper_auth["token"], UD_SECRET]
        if exchanges:
            exchange = fixture_exchange(keeper_auth)
            if signer_output is not None:
                exchange["signature"] = signer_output
            private.append(exchange["signature"])
            expected.append({"query": keeper.SESSION, "variables": exchange})
        assert keeper_auth["requests"] == expected
        assert_unexposed(env, result[1] + result[2], *private)
        if case in {
            "substituted",
            "empty-domain",
            "wrong-challenge-address",
            "wrong-typed-address",
            "wrong-typed-nonce",
        }:
            assert "wallet sign" not in env.log.read_text()
        if case in {
            "keeper-rejects-signature",
            "expired-challenge",
            "wrong-typed-chain",
        }:
            assert "Keeper rejected the request" in result[1]
        assert bool(session_state(env)["sessions"]) == (expected_code == 0)
    assert results[0] == results[1]
    python_state, rust_state = session_state(python), session_state(rust)
    if expected_code == 0:
        assert rust_state["sessions"][addr]["keyName"] == "dev"
    assert python_state == rust_state


def original_keeper_argument(origin: str | None = None) -> str:
    location = f" at {origin}" if origin else ""
    return (
        '"${SAI_RECONCILE_ENDPOINT:?'
        f'Set SAI_RECONCILE_ENDPOINT to the original Keeper URL{location}}}"'
    )


def reuse_keeper_guidance(origin: str) -> str:
    return f"Reuse the Keeper endpoint at {origin} used to sign in."


def save_expiry_fixture(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    *,
    addr: str,
    endpoint: str,
    expiry: datetime,
    source: str,
) -> None:
    session = {
        "evmAddress": addr,
        "endpoint": endpoint,
        "token": fixture_token(addr, expiry.timestamp()),
        "expiresAt": expiry.isoformat(),
    }
    if source == "keystore":
        session["keyName"] = "dev"
    elif source == "imported":
        session["source"] = source
    with monkeypatch.context() as ctx:
        ctx.setenv("SAI_HOME", str(env.home))
        storage.save_session(session)


@pytest.mark.parametrize(
    ("seconds_left", "when", "source", "keeper_source", "custom_endpoint"),
    [
        (45, "under 1 min", "imported", "flag", True),
        (100, "1 min", "imported", "flag", False),
        (119 * 60 + 30, "119 min", "keystore", "flag", True),
        (120 * 60 + 40, "2 h", "legacy", "flag", True),
        (71 * 3600 + 1800, "71 h", "keystore", "flag", False),
        (72 * 3600 + 120, None, "imported", "flag", False),
        (-60, None, "imported", "flag", False),
        (3650, "60 min", "imported", "default", False),
        (3650, "60 min", "imported", "file", True),
        (3650, "60 min", "imported", "env", True),
    ],
)
def test_auth_expiry_warning_matches_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    renewal_pair: tuple[CLI, CLI],
    seconds_left: int,
    when: str | None,
    source: str,
    keeper_source: str,
    custom_endpoint: bool,
) -> None:
    origin = "http://127.0.0.1:9"
    endpoint = (
        origin + "/fixture-path-key/query?access=fixture-query-key"
        if custom_endpoint
        else origin
    )
    if keeper_source == "default":
        endpoint = keeper.MAINNET_HTTP
        origin = endpoint.removesuffix("/query")
    expiry = datetime.now(UTC) + timedelta(seconds=seconds_left)
    option = original_keeper_argument() if custom_endpoint else endpoint
    prefix = f"sai --endpoint {option} auth"
    command = {
        "imported": f"{prefix} import",
        "keystore": f"{prefix} renew {ADDR}",
        "legacy": f"{prefix} import --addr {ADDR}",
    }[source]
    guidance = []
    if custom_endpoint:
        guidance.append(reuse_keeper_guidance(origin))
    if source == "legacy":
        guidance.append("You can also sign in again with your Foundry keystore.")
    fields = (
        {
            "expiryWarning": f"Sai session for {ADDR} expires in {when}.",
            "renewalCommand": command,
        }
        if when
        else {}
    )
    if when and guidance:
        fields["renewalGuidance"] = " ".join(guidance)
    results = []
    for env in homes(tmp_path, renewal_pair):
        if keeper_source == "file":
            assert env.run("config", "set", "keeper.endpoint", endpoint).returncode == 0
        elif keeper_source == "env":
            env.extra_env["SAI_ENDPOINT"] = endpoint
        save_expiry_fixture(
            env, monkeypatch, addr=ADDR, endpoint=endpoint, expiry=expiry, source=source
        )
        runs = []
        for auth in ("status", "list"):
            args = ("auth", auth)
            if keeper_source == "flag":
                args = ("--endpoint", endpoint, *args)
            result = env.run("--json", *args)
            assert result.stderr == ""
            data = json.loads(result.stdout)
            if auth == "status" and seconds_left < 0:
                assert result.returncode == 6
                assert data["error"]["code"] == "SESSION_EXPIRED"
                assert "expiryWarning" not in result.stdout
                assert "renewalCommand" not in result.stdout
            else:
                assert result.returncode == 0
                row = data if auth == "status" else data["accounts"][0]
                assert row["endpoint"] == origin
                assert {
                    key: row[key]
                    for key in ("expiryWarning", "renewalCommand", "renewalGuidance")
                    if key in row
                } == fields
                if auth == "list":
                    assert row["expired"] == (seconds_left < 0)
                    assert "expiryWarning" not in data and "renewalCommand" not in data
                piped = env.run(*args)
                assert piped.returncode == 0
                assert piped.stderr == ""
                assert json.loads(piped.stdout) == data
                runs.append(observed(piped))
            assert "fixture-path-key" not in result.stdout
            assert "fixture-query-key" not in result.stdout
            runs.append(observed(result))
        if source == "legacy":
            env.clipboard()
            token = fixture_token(OTHER)
            env.clip.write_text(token)
            before = session_state(env)
            refused = env.run("--endpoint", endpoint, "auth", "import", "--addr", ADDR)
            assert refused.returncode == 1
            assert "different account than --addr" in refused.stderr
            assert session_state(env) == before
            assert env.clip.read_text() == token
        if keeper_source == "default":
            env.clipboard()
            env.clip.write_text(fixture_token(ADDR))
            (env.bin / "sai").symlink_to(env.command[0])
            renewed = env.run_shell(command, SAI_ENDPOINT="http://127.0.0.1:9")
            assert renewed.returncode == 0, renewed.stderr
            assert session_state(env)["sessions"][ADDR]["endpoint"] == endpoint
        results.append(runs)
    assert results[0] == results[1]


def run_expiry_notice_on_terminal(env: Env, *args: str) -> tuple[int, str, str]:
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            [*env.command, *args],
            stdin=slave,
            stderr=slave,
            stdout=subprocess.PIPE,
            env=env.vars(),
            text=True,
            check=False,
            timeout=15,
        )
        os.set_blocking(master, False)
        try:
            stderr = os.read(master, 4096).decode().replace("\r\n", "\n")
        except BlockingIOError:
            stderr = ""
        return result.returncode, result.stdout, stderr
    finally:
        os.close(slave)
        os.close(master)


@pytest.mark.parametrize(
    ("auth", "json_output", "active_endpoint_matches"),
    [
        ("status", False, True),
        ("status", True, True),
        ("list", False, True),
        ("list", True, True),
        ("list", False, False),
    ],
)
def test_auth_expiry_notice_on_terminal_matches_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    renewal_pair: tuple[CLI, CLI],
    keeper_auth: dict[str, Any],
    auth: str,
    json_output: bool,
    active_endpoint_matches: bool,
) -> None:
    origin = keeper_auth["endpoint"].removesuffix("/query")
    active_endpoint = origin + "/fixture-path-key/query?access=fixture-query-key"
    endpoint = active_endpoint if active_endpoint_matches else "http://127.0.0.1:10"
    expiry = datetime.now(UTC) + timedelta(hours=24, minutes=30)
    addr = ADDR[:-4] + "\n\r\x1b\u2028"
    warning = rf"Sai session for {ADDR[:-4]}\n\r\x1b\u2028 expires in 24 h."
    renewal = f"sai --endpoint {original_keeper_argument()} auth import"
    guidance = reuse_keeper_guidance(origin)
    args = ("--endpoint", endpoint, "auth", auth)
    if json_output:
        args = ("--json", *args)
    results = []
    for env in homes(tmp_path, renewal_pair):
        save_expiry_fixture(
            env,
            monkeypatch,
            addr=OTHER,
            endpoint=endpoint,
            expiry=expiry,
            source="imported",
        )
        env.clipboard()
        env.clip.write_text(fixture_token(addr, expiry.timestamp()))
        assert env.run("--endpoint", active_endpoint, "auth", "import").returncode == 0
        result = run_expiry_notice_on_terminal(env, *args)
        assert result[0] == 0
        data = json.loads(result[1])
        row = data if auth == "status" else data["accounts"][0]
        assert row["addr"] == addr
        assert row["expiryWarning"] == warning
        assert row["renewalCommand"] == renewal
        assert row["renewalGuidance"] == guidance
        if auth == "list":
            assert (
                data["accounts"][1]["expiryWarning"]
                == f"Sai session for {OTHER} expires in 24 h."
            )
        expected = (
            f"{warning} {guidance} Run {renewal}\n"
            if active_endpoint_matches and not json_output
            else ""
        )
        assert result[2] == expected
        if expected:
            assert len(result[2].splitlines()) == 1
            command = result[2].split(" Run ", 1)[1].removesuffix("\n")
            (env.bin / "sai").symlink_to(env.command[0])
            env.clip.write_text(fixture_token(ADDR))
            renewed = subprocess.run(
                ["/bin/sh", "-c", command],
                env={**env.vars(), "SAI_RECONCILE_ENDPOINT": active_endpoint},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            assert json.loads(renewed.stdout)["addr"] == ADDR
            assert session_state(env)["sessions"][ADDR]["endpoint"] == active_endpoint
        results.append(result)
    assert results[0] == results[1]


@pytest.mark.parametrize("path", ["", "/query", "/fixture-path-key/query"])
def test_signed_out_recovery_hint_only_shows_the_keeper_origin(
    tmp_path: Path, auth_pair: tuple[CLI, CLI], path: str
) -> None:
    python, rust = homes(tmp_path, auth_pair)
    origin = "http://127.0.0.1:9"
    errors = []
    for env in (python, rust):
        result = env.run("--json", "--endpoint", origin + path, "auth", "status")
        assert result.returncode == 6
        assert result.stderr == ""
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "SIGNED_OUT"
        option = (
            original_keeper_argument(origin if env is rust else None)
            if path
            else origin
        )
        assert error.pop("hint") == f"sai --endpoint {option} auth login NAME"
        errors.append(error)
    assert errors[0] == errors[1]


def test_auth_summaries_hide_keeper_path(
    tmp_path: Path, auth_pair: tuple[CLI, CLI], keeper_auth: dict[str, Any]
) -> None:
    path = "/fixture-path-key/query"
    origin = keeper_auth["endpoint"].removesuffix("/query")
    endpoint = origin + path
    expiry = int((datetime.now(UTC) + timedelta(days=30)).timestamp())
    results = []
    for env in homes(tmp_path, auth_pair):
        fake_cast(env)
        keystore(env, addr=ADDR)
        env.clipboard()
        env.clip.write_text(fixture_token(OTHER, expiry))
        keeper_auth["paths"].clear()
        args = ("--json", "--endpoint", endpoint, "auth")
        outputs = []
        for command, operand in [
            ("login", ("dev",)),
            ("status", ()),
            ("import", ()),
            ("list", ()),
            ("use", (ADDR,)),
        ]:
            result = env.run(*args, command, *operand)
            assert result.returncode == 0
            payload = json.loads(result.stdout)
            summaries = payload["accounts"] if command == "list" else [payload]
            assert all(summary["endpoint"] == origin for summary in summaries)
            assert path not in result.stdout + result.stderr
            outputs.append(observed(result))
        assert keeper_auth["paths"] == [path, path]
        assert all(
            session["endpoint"] == endpoint
            for session in session_state(env)["sessions"].values()
        )
        results.append(outputs)
    assert results[0] == results[1]


@pytest.mark.parametrize("form", ["operand", "escaped", "option-value"])
@pytest.mark.parametrize("json_output", [False, True])
def test_import_usage_keeps_rejected_token_private(
    tmp_path: Path, cli_pair: tuple[CLI, CLI], json_output: bool, form: str
) -> None:
    _, rust = homes(tmp_path, cli_pair)
    token = fixture_token(ADDR)
    args = ("--json",) if json_output else ()
    operands = {
        "operand": (token,),
        "escaped": ("--", f"--{token}"),
        "option-value": (f"--help={token}",),
    }[form]
    result = rust.run(*args, "auth", "import", *operands)
    assert result.returncode == 2
    assert_unexposed(rust, result.stdout + result.stderr, token)
    assert not session_state(rust)["sessions"]
    if json_output:
        assert json.loads(result.stdout)["error"] == {
            "code": "INVALID_REQUEST",
            "message": "Unexpected arguments to auth import.",
            "retryable": False,
        }
        assert result.stderr == ""
    else:
        assert "Error: Unexpected arguments to auth import." in result.stderr
        assert result.stdout == ""


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize(
    "args",
    [
        ("auth", "import", "--json"),
        ("auth", "--json", "import"),
        ("--json", "auth", "import", "--json"),
    ],
)
def test_import_rejects_nonroot_json_before_reading_clipboard(
    tmp_path: Path, auth_pair: tuple[CLI, CLI], args: CLI, alias: bool
) -> None:
    results = []
    python, rust = homes(tmp_path, auth_pair)
    if alias:
        launcher = tmp_path / "sai-under-review"
        launcher.symlink_to(Path(rust.command[0]))
        rust.command = (str(launcher),)
    for env in (python, rust):
        env.clipboard()
        token = fixture_token(ADDR)
        env.clip.write_text(token)
        result = env.run(*args)
        assert result.returncode == 2
        assert env.clip.read_text() == token
        assert env.log.read_text() == ""
        assert not session_state(env)["sessions"]
        results.append(observed(result))
    assert results[0] == results[1]


def test_import_discards_clipboard_clear_output(
    tmp_path: Path, auth_pair: tuple[CLI, CLI]
) -> None:
    results = []
    for env in homes(tmp_path, auth_pair):
        env.clipboard()
        env.clip.write_text(fixture_token(ADDR))
        env.tool(
            "pbcopy",
            "import os, stat\n"
            "assert stat.S_ISCHR(os.fstat(1).st_mode)\n"
            "assert stat.S_ISCHR(os.fstat(2).st_mode)\n"
            "open(CLIP, 'w').write(sys.stdin.read())\n",
        )
        result = env.run("--json", "auth", "import")
        assert result.returncode == 0
        assert not env.clip.read_text()
        assert result.stderr == ""
        results.append(observed(result))
    assert results[0] == results[1]


def test_import_disables_echo_before_showing_prompt(
    tmp_path: Path, cli_pair: tuple[CLI, CLI]
) -> None:
    _, rust = homes(tmp_path, cli_pair)
    cc = shutil.which("cc")
    if sys.platform == "linux" and cc:
        # Make the old prompt-before-echo-off window deterministic. The
        # subprocess still reads and writes through a real pseudo-terminal.
        source = tmp_path / "delay-termios.c"
        source.write_text(
            "#include <dlfcn.h>\n"
            "#include <termios.h>\n"
            "#include <unistd.h>\n"
            "int tcsetattr(int fd, int action, const struct termios *mode) {\n"
            "    int (*apply)(int, int, const struct termios *) = "
            'dlsym(RTLD_NEXT, "tcsetattr");\n'
            "    if (!(mode->c_lflag & ECHO)) usleep(250000);\n"
            "    return apply(fd, action, mode);\n"
            "}\n"
        )
        library = tmp_path / "delay-termios.so"
        subprocess.run(
            [cc, "-shared", "-fPIC", str(source), "-o", str(library), "-ldl"],
            check=True,
            timeout=20,
        )
        rust.extra_env["LD_PRELOAD"] = str(library)

    token = fixture_token(ADDR)
    transcript = bytearray()
    result = rust.run_tty(
        "--json",
        "auth",
        "import",
        typed=[token],
        immediate_prompt=True,
        transcript=transcript,
    )
    assert result[0] == 0
    assert token.encode() not in transcript
    assert_unexposed(rust, result[1] + result[2], token)


def interrupt_prompt(
    env: Env,
    args: CLI,
    terminal_ctrl_c: bool,
    *,
    raw_mode: bool = False,
    before_interrupt: Callable[[int], None] | None = None,
    directed_signal: signal.Signals = signal.SIGINT,
) -> tuple[int, str, str]:
    master, slave = pty.openpty()
    if raw_mode:
        tty.setraw(slave)
    original = termios.tcgetattr(master)
    original_flags = fcntl.fcntl(slave, fcntl.F_GETFL)
    # Attach a controlling PTY so the terminal itself can deliver VINTR.
    launch = (
        "import fcntl, os, sys, termios; "
        "fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
        "os.execv(sys.argv[1], sys.argv[1:])"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", launch, *env.command, *args],
        stdin=slave,
        stderr=slave,
        stdout=subprocess.PIPE,
        env=env.vars(),
        start_new_session=True,
    )
    transcript = bytearray()
    try:
        while not transcript.endswith(b": "):
            assert select.select([master], [], [], 5)[0], "import prompt did not appear"
            transcript.extend(os.read(master, 4096))
        assert b"Paste the Sai session token" in transcript
        assert not termios.tcgetattr(master)[3] & termios.ECHO
        if before_interrupt is not None:
            before_interrupt(master)
        if terminal_ctrl_c:
            os.write(master, b"\x03")
        else:
            proc.send_signal(directed_signal)
        stdout, _ = proc.communicate(timeout=3)
        assert termios.tcgetattr(master) == original
        assert fcntl.fcntl(slave, fcntl.F_GETFL) == original_flags
        os.set_blocking(master, False)
        with suppress(OSError):
            while chunk := os.read(master, 4096):
                transcript.extend(chunk)
        return (
            proc.returncode,
            stdout.decode(),
            transcript.decode().replace("\r\n", "\n"),
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        os.close(slave)
        os.close(master)


@pytest.mark.parametrize("terminal_ctrl_c", [False, True])
@pytest.mark.parametrize("json_output", [False, True])
def test_interrupting_import_prompt_restores_the_terminal(
    tmp_path: Path, auth_pair: tuple[CLI, CLI], terminal_ctrl_c: bool, json_output: bool
) -> None:
    args = ("--json", "auth", "import") if json_output else ("auth", "import")
    results = []
    for env in homes(tmp_path, auth_pair):
        result = interrupt_prompt(env, args, terminal_ctrl_c)
        assert result[0] == 1
        assert not session_state(env)["sessions"]
        results.append(result)
    assert results[0] == results[1]


def test_import_ctrl_c_restores_an_inherited_raw_terminal(
    tmp_path: Path, cli_pair: tuple[CLI, CLI]
) -> None:
    _, rust = homes(tmp_path, cli_pair)
    code, stdout, stderr = interrupt_prompt(
        rust, ("auth", "import"), terminal_ctrl_c=True, raw_mode=True
    )
    assert code == 1
    assert stdout == ""
    assert stderr.endswith("\nAborted!\n")


@pytest.mark.parametrize("raw_mode", [False, True])
def test_terminating_import_prompt_restores_the_terminal(
    tmp_path: Path, cli_pair: tuple[CLI, CLI], raw_mode: bool
) -> None:
    _, rust = homes(tmp_path, cli_pair)
    code, stdout, _ = interrupt_prompt(
        rust,
        ("auth", "import"),
        terminal_ctrl_c=False,
        raw_mode=raw_mode,
        directed_signal=signal.SIGTERM,
    )
    assert code == -signal.SIGTERM
    assert stdout == ""
    assert not session_state(rust)["sessions"]


@pytest.mark.parametrize("json_output", [False, True])
def test_interrupting_import_clipboard_handoff_saves_nothing(
    tmp_path: Path, auth_pair: tuple[CLI, CLI], json_output: bool
) -> None:
    args = ("--json", "auth", "import") if json_output else ("auth", "import")
    results = []
    for env in homes(tmp_path, auth_pair):
        started = env.clip.with_suffix(".started")
        release = env.clip.with_suffix(".release")
        token = fixture_token(ADDR)
        env.clip.write_text(token)
        env.tool(
            "pbpaste",
            "import time\n"
            "from pathlib import Path\n"
            f"Path({str(started)!r}).touch()\n"
            "deadline = time.monotonic() + 8\n"
            f"while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "sys.stdout.write(Path(CLIP).read_text())\n",
        )

        def wait_for_reader(master: int, started: Path = started) -> None:
            os.write(master, b"\n")
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert started.exists(), "clipboard reader did not start"

        try:
            result = interrupt_prompt(
                env, args, terminal_ctrl_c=False, before_interrupt=wait_for_reader
            )
        finally:
            release.touch()
        assert result[0] == 1
        assert not session_state(env)["sessions"]
        assert env.clip.read_text() == token
        results.append(result)
    assert results[0] == results[1]


def test_interrupting_import_while_session_store_is_locked_saves_nothing(
    tmp_path: Path, cli_pair: tuple[CLI, CLI]
) -> None:
    _, rust = homes(tmp_path, cli_pair)
    token = fixture_token(ADDR)
    trader = rust.home / "trader"
    trader.mkdir(parents=True, mode=0o700)

    def submit_token(master: int) -> None:
        os.write(master, (token + "\n").encode())
        assert select.select([master], [], [], 5)[0], "prompt did not complete"
        assert os.read(master, 4096) == b"\r\n"

    with (trader / "sessions.lock").open("wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        code, stdout, stderr = interrupt_prompt(
            rust,
            ("auth", "import"),
            terminal_ctrl_c=False,
            before_interrupt=submit_token,
        )
    assert code == 1
    assert stdout == ""
    assert stderr.endswith("\nAborted!\n")
    assert not session_state(rust)["sessions"]


@pytest.mark.parametrize("entry", ["clipboard", "hidden", "enter", "changed"])
def test_auth_import_and_account_selection(
    tmp_path: Path, auth_pair: tuple[CLI, CLI], entry: str
) -> None:
    python, rust = homes(tmp_path, auth_pair)
    endpoint = "http://127.0.0.1:9/query"
    token = fixture_token(ADDR, 4070908800.125)
    other = fixture_token(OTHER)
    outputs = []
    for env in (python, rust):
        env.clipboard()
        env.clip.write_text(token)
        args = ("--json", "--endpoint", endpoint, "auth", "import")
        if entry == "clipboard":
            imported = observed(env.run(*args, stdin=other + "\n"))
        else:
            typed = "" if entry == "enter" else token
            if entry == "changed":
                imported = env.run_tty(
                    *args, typed=[lambda env=env: env.clip.write_text(other), typed]
                )
            else:
                imported = env.run_tty(*args, typed=[typed])
        assert imported[0] == 0
        assert json.loads(imported[1])["addr"] == ADDR
        assert env.clip.read_text() == (other if entry == "changed" else "")
        env.clip.write_text(other)
        assert env.run(*args).returncode == 0
        runs = [imported]
        for auth in [
            ("list",),
            ("use", ADDR),
            ("status",),
            ("logout",),
            ("status",),
            ("list",),
            ("logout", OTHER),
            ("logout",),
        ]:
            runs.append(
                observed(env.run("--json", "--endpoint", endpoint, "auth", *auth))
            )
        assert [run[0] for run in runs] == [0, 0, 0, 0, 0, 1, 0, 0, 0]
        assert session_state(env)["sessions"] == {}
        assert_unexposed(env, "".join(out + err for _, out, err in runs), token, other)
        outputs.append(runs)
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    ("case", "code"), [("unavailable", 3), ("empty", 1), ("invalid", 1), ("expired", 6)]
)
def test_import_rejects_bad_clipboard(
    tmp_path: Path, auth_pair: tuple[CLI, CLI], case: str, code: int
) -> None:
    results = []
    for env in homes(tmp_path, auth_pair):
        if case != "unavailable":
            token = {
                "empty": "",
                "invalid": "bad.token",
                "expired": fixture_token(ADDR, 0),
            }[case]
            env.clipboard()
            env.clip.write_text(token)
        result = env.run("--json", "auth", "import", stdin=fixture_token(ADDR))
        assert result.returncode == code
        if case != "unavailable":
            assert env.clip.read_text() == token
        assert not session_state(env)["sessions"]
        results.append(observed(result))
    assert results[0] == results[1]


def assert_truncated_store_parity(
    tmp_path: Path, pair: tuple[CLI, CLI], commands: list[tuple[CLI, int, str | None]]
) -> None:
    observed_results = []
    for env in homes(tmp_path, pair):
        path = env.home / "trader" / "sessions.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"version":')
        results = []
        for command, code, error_code in commands:
            result = env.run("--json", "auth", *command)
            assert result.returncode == code
            payload = json.loads(result.stdout)
            if error_code is None:
                assert payload["removed"] is True
                assert not path.exists()
            else:
                assert payload["error"]["code"] == error_code
                assert path.read_bytes() == b'{"version":'
            results.append(observed(result))
        observed_results.append(results)
    assert observed_results[0] == observed_results[1]


def test_auth_commands_keep_truncated_store_error_codes(
    tmp_path: Path, auth_pair: tuple[CLI, CLI]
) -> None:
    assert_truncated_store_parity(
        tmp_path,
        auth_pair,
        [
            (("list",), 1, "INVALID_REQUEST"),
            (("use", ADDR), 1, "INVALID_REQUEST"),
            (("logout", ADDR), 1, "INVALID_REQUEST"),
            (("status",), 1, "SESSION_UNAVAILABLE"),
            (("logout",), 0, None),
        ],
    )


def test_renew_keeps_truncated_store_error_code(
    tmp_path: Path, auth_pair: tuple[CLI, CLI]
) -> None:
    assert_truncated_store_parity(
        tmp_path, auth_pair, [(("renew",), 1, "INVALID_REQUEST")]
    )


def test_renew_matches_python_without_switching_active_account(
    tmp_path: Path,
    renewal_pair: tuple[CLI, CLI],
    keeper_auth: dict[str, Any],
) -> None:
    python, rust = homes(tmp_path, renewal_pair)
    results = []
    endpoint = keeper_auth["endpoint"]
    renewed_token = fixture_token(ADDR)
    old_expiry = "2000-01-01T00:00:00+00:00"
    for env in (python, rust):
        keeper_auth["token"] = fixture_token(ADDR, 0)
        keeper_auth["expires_at"] = old_expiry
        fake_cast(env)
        keystore(env, addr=ADDR)
        args = ("--json", "--endpoint", endpoint, "auth")
        assert env.run(*args, "login", "dev").returncode == 0
        env.clipboard()
        env.clip.write_text(fixture_token(OTHER))
        assert env.run(*args, "import").returncode == 0
        rejected = env.run(*args, "renew")
        assert rejected.returncode == 1
        error = json.loads(rejected.stdout)["error"]
        assert error["code"] == "INVALID_REQUEST"
        origin = endpoint.removesuffix("/query")
        recovery = f"sai --endpoint {original_keeper_argument()} auth import"
        assert (
            error["message"]
            == f"This session was imported. Run `{recovery}` with a fresh token. "
            f"{reuse_keeper_guidance(origin)}"
        )
        before = session_state(env)
        assert before["sessions"][ADDR]["expiresAt"] == old_expiry
        assert before["active"] == OTHER
        keeper_auth["requests"].clear()
        keeper_auth["token"] = renewed_token
        keeper_auth["expires_at"] = "2099-01-01T00:00:00+00:00"
        results.append(observed(env.run(*args, "renew", ADDR)))
        assert results[-1][0] == 0
        assert keeper_auth["requests"] == [
            {"query": keeper.CHALLENGE, "variables": {"address": ADDR}},
            {
                "query": keeper.SESSION,
                "variables": fixture_exchange(keeper_auth),
            },
        ]
        after = session_state(env)
        assert after["active"] == OTHER
        assert before["sessions"][OTHER] == after["sessions"][OTHER]
        assert after["sessions"][ADDR]["token"] == renewed_token
        assert_unexposed(env, results[-1][1] + results[-1][2], renewed_token)
    assert results[0] == results[1]
    assert session_state(python) == session_state(rust)


def test_use_rejects_a_malformed_expiry_written_by_python(
    tmp_path: Path, auth_pair: tuple[CLI, CLI], keeper_auth: dict[str, Any]
) -> None:
    python, rust = homes(tmp_path, auth_pair)
    python.extra_env["SAI_HOME"] = str(rust.home)
    python.extra_env["SAI_LOG"] = "0"
    token = fixture_token(ADDR)
    keeper_auth.update(token=token, expires_at=token)
    fake_cast(python)
    keystore(python, addr=ADDR)
    args = ("--json", "--endpoint", keeper_auth["endpoint"], "auth")
    assert python.run(*args, "login", "dev").returncode == 0
    python.clipboard()
    python.clip.write_text(fixture_token(OTHER))
    assert python.run(*args, "import").returncode == 0
    path = rust.home / "trader" / "sessions.json"
    before = path.read_bytes()

    result = rust.run(*args, "use", ADDR)

    assert_unexposed(rust, result.stdout + result.stderr, token)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "SESSION_UNAVAILABLE"
    assert path.read_bytes() == before
    assert session_state(rust)["active"] == OTHER


def test_renew_rejects_malformed_expiry_without_replacing_the_session(
    tmp_path: Path, cli_pair: tuple[CLI, CLI], keeper_auth: dict[str, Any]
) -> None:
    _, rust = homes(tmp_path, cli_pair)
    fake_cast(rust)
    keystore(rust, addr=ADDR)
    args = ("--json", "--endpoint", keeper_auth["endpoint"], "auth")
    keeper_auth["expires_at"] = "2099-01-01T00:00:00+00:00"
    assert rust.run(*args, "login", "dev").returncode == 0
    path = rust.home / "trader" / "sessions.json"
    before = path.read_bytes()
    previous_status = observed(rust.run(*args, "status"))
    assert previous_status[0] == 0

    token = fixture_token(ADDR)
    keeper_auth["token"] = token
    keeper_auth["expires_at"] = token
    result = rust.run(*args, "renew")
    assert_unexposed(rust, result.stdout + result.stderr, token)
    assert result.returncode == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "KEEPER_ERROR"
    assert error["message"] == "Keeper returned an invalid session expiry"
    assert path.read_bytes() == before
    assert observed(rust.run(*args, "status")) == previous_status


def test_renew_rejects_expired_replacement_without_replacing_the_session(
    tmp_path: Path, cli_pair: tuple[CLI, CLI], keeper_auth: dict[str, Any]
) -> None:
    _, rust = homes(tmp_path, cli_pair)
    fake_cast(rust)
    keystore(rust, addr=ADDR)
    args = ("--json", "--endpoint", keeper_auth["endpoint"], "auth")
    keeper_auth["expires_at"] = "2099-01-01T00:00:00+00:00"
    assert rust.run(*args, "login", "dev").returncode == 0
    path = rust.home / "trader" / "sessions.json"
    before = path.read_bytes()
    previous_status = observed(rust.run(*args, "status"))
    assert previous_status[0] == 0

    keeper_auth.update(token=fixture_token(ADDR, 0), expires_at="2000-01-01T00:00:00Z")
    result = rust.run(*args, "renew")

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["message"] == (
        "Keeper returned an already expired session"
    )
    assert path.read_bytes() == before
    assert observed(rust.run(*args, "status")) == previous_status


def test_renew_does_not_restore_a_concurrent_logout(
    tmp_path: Path, auth_pair: tuple[CLI, CLI], keeper_auth: dict[str, Any]
) -> None:
    results = []
    for env in homes(tmp_path, auth_pair):
        fake_cast(env)
        keystore(env, addr=ADDR)
        args = ("--json", "--endpoint", keeper_auth["endpoint"], "auth")
        assert env.run(*args, "login", "dev").returncode == 0
        env.tool(
            "cast",
            "import hashlib, json, subprocess\n"
            + inspect.getsource(fixture_signature)
            + f"subprocess.run({[*env.command, '--json', 'auth', 'logout', ADDR]!r},"
            " check=True, stdout=subprocess.DEVNULL)\n"
            "print(fixture_signature(json.load(open(sys.argv[-1]))))\n",
        )
        result = env.run(*args, "renew")
        assert result.returncode == 1
        assert "Stored session changed during renewal" in result.stdout
        assert not session_state(env)["sessions"]
        results.append(observed(result))
    assert results[0] == results[1]


@pytest.mark.parametrize("source", ["imported", "keystore"])
def test_expired_session_recovery_matches_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    renewal_pair: tuple[CLI, CLI],
    keeper_auth: dict[str, Any],
    source: str,
) -> None:
    args = ("--endpoint", keeper_auth["endpoint"])
    results = []
    python, rust = homes(tmp_path, renewal_pair)
    for env in (python, rust):
        keeper_auth["expires_at"] = "2000-01-01T00:00:00+00:00"
        keeper_auth["token"] = fixture_token(ADDR, 0)
        fake_cast(env)
        keystore(env, addr=ADDR)
        assert env.run(*args, "auth", "login", "dev").returncode == 0
        if source == "imported":
            accounts = session_state(env)
            session = accounts["sessions"][ADDR]
            del session["keyName"]
            session["source"] = "imported"
            with monkeypatch.context() as context:
                context.setenv("SAI_HOME", str(env.home))
                storage.save_session(session)
        text = env.run(*args, "auth", "status")
        structured = env.run("--json", *args, "auth", "status")
        assert text.returncode == structured.returncode == 6
        body = json.loads(structured.stdout)["error"]
        command = f"renew {ADDR}" if source == "keystore" else "import"
        origin = keeper_auth["endpoint"].removesuffix("/query")
        assert (
            body["hint"]
            == f"sai --endpoint {original_keeper_argument()} auth {command}"
        )
        assert reuse_keeper_guidance(origin) in body["message"]
        results.append(
            (observed(text), (structured.returncode, body, structured.stderr))
        )
    assert results[0] == results[1]


@pytest.mark.parametrize(
    "suffix", ["", "/fixture-path-key/query?access=fixture-query-key"]
)
def test_import_recovery_keeps_the_one_off_keeper_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_pair: tuple[CLI, CLI],
    suffix: str,
) -> None:
    _, rust = homes(tmp_path, cli_pair)
    origin = "http://127.0.0.1:9"
    endpoint = origin + suffix
    rust.extra_env["SAI_ENDPOINT"] = "http://127.0.0.1:10"
    rust.clipboard()
    rust.clip.write_text(fixture_token(ADDR))
    (rust.bin / "sai").symlink_to(rust.command[0])
    with monkeypatch.context() as context:
        context.setenv("SAI_HOME", str(rust.home))
        storage.save_session(
            {
                "evmAddress": ADDR,
                "endpoint": endpoint,
                "token": fixture_token(ADDR, 0),
                "expiresAt": "2000-01-01T00:00:00Z",
                "source": "imported",
            }
        )

    result = rust.run("--json", "--endpoint", endpoint, "auth", "status")
    assert result.returncode == 6
    error = json.loads(result.stdout)["error"]
    hint = error["hint"]
    assert "fixture-path-key" not in hint
    assert "fixture-query-key" not in hint
    arg = original_keeper_argument() if suffix else origin
    assert hint == f"sai --endpoint {arg} auth import"
    if suffix:
        assert reuse_keeper_guidance(origin) in error["message"]
    if suffix:
        missing = subprocess.run(
            ["/bin/sh", "-c", hint],
            stdin=subprocess.DEVNULL,
            env=rust.vars(),
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert missing.returncode != 0
        assert (
            session_state(rust)["sessions"][ADDR]["expiresAt"] == "2000-01-01T00:00:00Z"
        )
        rust.extra_env["SAI_RECONCILE_ENDPOINT"] = endpoint
    recovered = subprocess.run(
        ["/bin/sh", "-c", hint],
        stdin=subprocess.DEVNULL,
        env=rust.vars(),
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert recovered.returncode == 0
    assert session_state(rust)["sessions"][ADDR]["endpoint"] == endpoint
    assert rust.run("--endpoint", endpoint, "auth", "status").returncode == 0
