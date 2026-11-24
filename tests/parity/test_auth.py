"""Compare Rust and Python auth using isolated tools and loopback Keeper."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sai_cli.core import keeper, storage
from sai_cli.tests.test_auth_connect import (
    ADDR,
    NONCE,
    OTHER,
    SIGNATURE,
    UD_SECRET,
    Env,
    fake_cast,
    fake_ud,
    keystore,
    typed_data,
)
from sai_cli.tests.test_auth_connect import keeper_auth as keeper_auth  # noqa: PLC0414

CLI = tuple[str, ...]


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
        ("--json", "auth", "import", "unexpected"),
    ],
)
def test_auth_usage(tmp_path: Path, cli_pair: tuple[CLI, CLI], args: CLI) -> None:
    python, rust = homes(tmp_path, cli_pair)
    expected = observed(python.run(*args))
    assert expected[0] == (0 if "--help" in args else 2)
    assert observed(rust.run(*args)) == expected


@pytest.mark.parametrize(
    ("case", "expected_code", "challenges", "exchanges"),
    [
        ("cast", 0, 1, 1),
        ("ud", 0, 1, 1),
        ("missing", 1, 0, 0),
        ("mismatch", 1, 0, 0),
        ("collision", 1, 0, 0),
        ("address-disambiguates", 0, 1, 1),
        ("unavailable", 1, 0, 0),
        ("substituted", 1, 1, 0),
        ("empty-domain", 1, 1, 0),
        ("ud-locked", 3, 1, 0),
        ("terminal-fallback", 0, 2, 1),
        ("rotated-keystore", 1, 1, 0),
        ("wrong-session-address", 1, 1, 1),
    ],
)
def test_auth_login(
    tmp_path: Path,
    cli_pair: tuple[CLI, CLI],
    keeper_auth: dict[str, Any],
    case: str,
    expected_code: int,
    challenges: int,
    exchanges: int,
) -> None:
    python, rust = homes(tmp_path, cli_pair)
    addr = OTHER if case == "wrong-session-address" else ADDR
    keeper_auth["typed"] = typed_data(message__account=addr)
    if case == "substituted":
        keeper_auth["typed"]["primaryType"] = "Permit"
    if case == "empty-domain":
        keeper_auth["typed"]["domain"]["chainId"] = None
    args = ["--json", "--endpoint", keeper_auth["endpoint"], "auth", "login", "dev"]
    if case == "mismatch":
        args.extend(["--addr", OTHER])
    elif case == "address-disambiguates":
        args.extend(["--addr", ADDR])
    results = []
    for env in (python, rust):
        if case != "missing":
            fake_cast(env)
            keystore(env, addr=None if case == "cast" else addr)
        if case in {"ud", "ud-locked", "terminal-fallback", "rotated-keystore"}:
            fake_ud(env, locked=case != "ud", rotate=case == "rotated-keystore")
        elif case == "unavailable":
            fake_ud(env, down=True)
        elif case in {"collision", "address-disambiguates"}:
            fake_ud(env, state="name-conflict", addr=OTHER)
        keeper_auth["requests"].clear()
        if case in {"terminal-fallback", "rotated-keystore"}:
            result = env.run_tty(*args, typed=[])
        else:
            result = observed(env.run(*args))
        results.append(result)
        assert result[0] == expected_code
        lookup_addr = (
            addr.upper().replace("0X", "0x")
            if case in {"ud", "ud-locked", "terminal-fallback", "rotated-keystore"}
            else addr
        )
        assert (
            keeper_auth["requests"]
            == [{"query": keeper.CHALLENGE, "variables": {"address": lookup_addr}}]
            * challenges
            + [
                {
                    "query": keeper.SESSION,
                    "variables": {"challenge": NONCE, "signature": SIGNATURE},
                }
            ]
            * exchanges
        )
        assert_unexposed(
            env, result[1] + result[2], keeper_auth["token"], SIGNATURE, UD_SECRET
        )
        if case in {"substituted", "empty-domain"}:
            assert "wallet sign" not in env.log.read_text()
        assert bool(session_state(env)["sessions"]) == (expected_code == 0)
    assert results[0] == results[1]
    python_state, rust_state = session_state(python), session_state(rust)
    if expected_code == 0:
        assert rust_state["sessions"][addr].pop("keyName") == "dev"
    assert python_state == rust_state


@pytest.mark.parametrize("entry", ["clipboard", "hidden", "enter", "changed"])
def test_auth_import_and_account_selection(
    tmp_path: Path, cli_pair: tuple[CLI, CLI], entry: str
) -> None:
    python, rust = homes(tmp_path, cli_pair)
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
    tmp_path: Path, cli_pair: tuple[CLI, CLI], case: str, code: int
) -> None:
    results = []
    for env in homes(tmp_path, cli_pair):
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


@pytest.fixture
def renewal_pair(cli_pair: tuple[CLI, CLI]) -> tuple[CLI, CLI]:
    path = os.environ.get("SAI_PARITY_RENEW_PYTHON")
    if path is None:
        pytest.skip("set SAI_PARITY_RENEW_PYTHON to the Python executable with PR 61")
    executable = shutil.which(path)
    if executable is None:
        pytest.fail("SAI_PARITY_RENEW_PYTHON must name an executable")
    return (executable,), cli_pair[1]


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
        results.append(observed(env.run(*args, "renew")))
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
                "variables": {"challenge": NONCE, "signature": SIGNATURE},
            },
        ]
        after = session_state(env)
        assert after["active"] == OTHER
        assert before["sessions"][OTHER] == after["sessions"][OTHER]
        assert after["sessions"][ADDR]["token"] == renewed_token
        assert_unexposed(env, results[-1][1] + results[-1][2], renewed_token)
    assert results[0:2] == results[2:4]
    assert session_state(python) == session_state(rust)


def test_renew_does_not_restore_a_concurrent_logout(
    tmp_path: Path, renewal_pair: tuple[CLI, CLI], keeper_auth: dict[str, Any]
) -> None:
    results = []
    for env in homes(tmp_path, renewal_pair):
        fake_cast(env)
        keystore(env, addr=ADDR)
        args = ("--json", "--endpoint", keeper_auth["endpoint"], "auth")
        assert env.run(*args, "login", "dev").returncode == 0
        env.tool(
            "cast",
            "import subprocess\n"
            f"subprocess.run({[*env.command, '--json', 'auth', 'logout', ADDR]!r},"
            " check=True, stdout=subprocess.DEVNULL)\n"
            f"print({SIGNATURE!r})\n",
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
    for env in homes(tmp_path, renewal_pair):
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
        hint = json.loads(structured.stdout)["error"]["hint"]
        command = f"renew {ADDR}" if source == "keystore" else "import"
        assert hint == f"sai --endpoint {keeper_auth['endpoint']} auth {command}"
        results.append((observed(text), observed(structured)))
    assert results[0] == results[1]
