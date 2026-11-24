"""Connect the installed CLI through the clipboard, a terminal, and cast.

Each test runs the CLI as a subprocess with a temp SAI_HOME and a PATH that
holds only fake clipboard and cast programs, so no real clipboard or keystore
is touched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pty
import shlex
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sai_cli.core import keeper

ADDR = "0x" + "b" * 40
OTHER = "0x" + "c" * 40
# Keeper sends the nonce as bare hex and the typed-data challenge with 0x.
NONCE = "12" * 32
SIGNATURE = "0x" + "1" * 130


def make_token(addr: str = ADDR, hours: int = 720) -> str:
    def part(value: dict) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    exp = int((datetime.now(UTC) + timedelta(hours=hours)).timestamp())
    claims = {"purpose": "user_intent", "sub": addr, "exp": exp}
    return f"{part({'alg': 'HS256'})}.{part(claims)}.c2lnbmF0dXJl"


class Env:
    """A temp SAI_HOME, a fake clipboard file, and a PATH of fake tools."""

    def __init__(self, root: Path) -> None:
        self.home = root / "sai-home"
        self.bin = root / "bin"
        self.clip = root / "clipboard.txt"
        self.log = root / "calls.log"
        self.bin.mkdir()
        self.log.touch()

    def tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"CLIP = {str(self.clip)!r}\n"
            f"open({str(self.log)!r}, 'a').write(' '.join(sys.argv) + '\\n')\n" + body,
            encoding="utf-8",
        )
        path.chmod(0o755)

    def clipboard(self, reader: str = "pbpaste", writer: str = "pbcopy") -> None:
        self.tool(reader, "sys.stdout.write(open(CLIP).read())\n")
        self.tool(writer, "open(CLIP, 'w').write(sys.stdin.read())\n")

    def vars(self) -> dict[str, str]:
        # Built from scratch so no credentials from the parent shell leak in.
        return {
            "SAI_HOME": str(self.home),
            "PATH": str(self.bin),
            "HOME": str(self.home),
        }

    def run(
        self, *args: str, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sai_cli", *args],
            input=stdin,
            check=False,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=self.vars(),
            timeout=30,
        )

    def run_shell(
        self, command: str, **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", "-c", command],
            env={
                **self.vars(),
                "PATH": f"{self.bin}{os.pathsep}{Path(sys.executable).parent}",
                **overrides,
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )

    def run_tty(
        self, *args: str, typed: list[str | Callable[[], object]]
    ) -> tuple[int, str, str]:
        """Run with a pseudo-terminal on stdin and answer each hidden prompt.

        `getpass` turns echo off with TCSAFLUSH, which drops input typed
        earlier. Each line therefore waits until stderr has grown since the
        previous answer, which `getpass` only does after reading it, and echo
        is off again for the next prompt. A callable entry runs once that
        next prompt is showing, before the line that answers it.
        """
        primary, secondary = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, "-m", "sai_cli", *args],
            stdin=secondary,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.vars(),
            start_new_session=True,
        )
        os.close(secondary)
        errors = bytearray()

        def drain() -> None:
            assert proc.stderr
            while chunk := os.read(proc.stderr.fileno(), 4096):
                errors.extend(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            mark = 0
            for line in typed:
                deadline = time.monotonic() + 20
                while proc.poll() is None and time.monotonic() < deadline:
                    echo = termios.tcgetattr(primary)[3] & termios.ECHO
                    if len(errors) > mark and not echo:
                        break
                    time.sleep(0.01)
                if callable(line):
                    line()
                    continue
                os.write(primary, (line + "\n").encode())
                mark = len(errors)
            assert proc.stdout
            stdout = proc.stdout.read()
            proc.wait(timeout=30)
            reader.join(timeout=5)
        finally:
            proc.kill()
            os.close(primary)
        return proc.returncode, stdout.decode(), errors.decode()

    def stored_bytes(self) -> bytes:
        return b"".join(
            path.read_bytes() for path in self.home.rglob("*") if path.is_file()
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def assert_connected(env: Env, token: str, result_stdout: str) -> None:
    output = json.loads(result_stdout)
    assert output == {
        "addr": ADDR,
        "expiresAt": output["expiresAt"],
        "endpoint": "https://sai-keeper.nibiru.fi",
    }
    assert token.encode() not in env.stored_bytes()
    status = env.run("--json", "auth", "status")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["addr"] == ADDR
    assert token not in status.stdout + status.stderr


def test_agent_import_reads_and_clears_clipboard(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token + "\n")
    result = env.run("--json", "auth", "import")
    assert result.returncode == 0, result.stderr
    assert token not in result.stdout + result.stderr
    assert env.clip.read_text() == ""
    assert_connected(env, token, result.stdout)


def test_logout_deletes_the_session_without_network(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token)
    assert env.run("auth", "import").returncode == 0
    # An endpoint nothing listens on: logout must not contact Keeper.
    result = env.run("--json", "--endpoint", "http://127.0.0.1:9/q", "auth", "logout")
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["removed"] is True
    assert "cannot revoke" in output["note"]
    assert token not in result.stdout + result.stderr
    assert not (env.home / "trader" / "sessions.json").exists()
    status = env.run("auth", "status")
    assert status.returncode != 0
    assert "No Sai session" in status.stderr
    again = env.run("--json", "auth", "logout")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["removed"] is False


def test_agent_import_ignores_piped_stdin(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token)
    piped = make_token(OTHER)
    result = env.run("--json", "auth", "import", stdin=piped + "\n")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["addr"] == ADDR
    assert piped not in result.stdout + result.stderr


def test_agent_import_skips_failing_clipboard_tool(env: Env) -> None:
    token = make_token()
    env.tool("wl-paste", "sys.exit(1)\n")
    # One fake xclip both reads (-o) and writes (stdin), as the real one does.
    env.tool(
        "xclip",
        "if '-o' in sys.argv:\n"
        "    sys.stdout.write(open(CLIP).read())\n"
        "else:\n"
        "    open(CLIP, 'w').write(sys.stdin.read())\n",
    )
    env.clip.write_text(token)
    result = env.run("--json", "auth", "import")
    assert result.returncode == 0, result.stderr
    assert env.clip.read_text() == ""
    calls = env.log.read_text()
    assert "wl-paste --no-newline" in calls
    assert "xclip -selection clipboard -o" in calls


def test_agent_import_skips_binary_clipboard(env: Env) -> None:
    token = make_token()
    env.tool("pbpaste", "sys.stdout.buffer.write(b'\\x89PNG\\r\\n\\x1a\\n\\xff')\n")
    env.tool(
        "xclip",
        "if '-o' in sys.argv:\n"
        "    sys.stdout.write(open(CLIP).read())\n"
        "else:\n"
        "    open(CLIP, 'w').write(sys.stdin.read())\n",
    )
    env.clip.write_text(token)
    result = env.run("--json", "auth", "import")
    assert result.returncode == 0, result.stderr
    assert "codec" not in result.stderr
    assert env.clip.read_text() == ""


def test_agent_import_without_clipboard_asks_for_a_terminal(env: Env) -> None:
    result = env.run("auth", "import")
    assert result.returncode == 3
    assert "Ask the person to run `sai auth import`" in result.stderr
    data = env.run("--json", "auth", "import")
    assert data.returncode == 3 and data.stderr == ""
    error = json.loads(data.stdout)["error"]
    assert error["code"] == "INVALID_REQUEST" and error["hint"] == "sai auth import"
    assert not (env.home / "trader" / "sessions.json").exists()


def test_invalid_clipboard_is_not_echoed_or_cleared(env: Env) -> None:
    secret = "not-a-jwt-but-maybe-a-password"
    expired = make_token(hours=-1)
    env.clipboard()
    for content in (secret, expired):
        env.clip.write_text(content)
        result = env.run("auth", "import")
        assert result.returncode != 0
        assert "Invalid or expired Sai session token" in result.stderr
        assert content not in result.stdout + result.stderr
        assert env.clip.read_text() == content


def test_terminal_import_reads_hidden_prompt(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text("unrelated clipboard text")
    code, stdout, stderr = env.run_tty("--json", "auth", "import", typed=[token])
    assert code == 0, stderr
    assert token not in stdout + stderr
    # The clipboard held something else, so the CLI leaves it alone.
    assert env.clip.read_text() == "unrelated clipboard text"
    assert_connected(env, token, stdout)


def test_terminal_import_clears_pasted_token(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token)
    code, _, stderr = env.run_tty("--json", "auth", "import", typed=[token])
    assert code == 0, stderr
    assert env.clip.read_text() == ""


def test_terminal_import_enter_reads_clipboard(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token)
    code, stdout, stderr = env.run_tty("--json", "auth", "import", typed=[""])
    assert code == 0, stderr
    assert token not in stdout + stderr
    assert env.clip.read_text() == ""
    assert_connected(env, token, stdout)


def session_for(token: str) -> dict[str, str]:
    return {
        "token": token,
        "evmAddress": ADDR,
        "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "endpoint": keeper.MAINNET_HTTP,
    }


def write_envelope(home: Path, envelope: dict[str, Any]) -> None:
    root = home / "trader"
    root.mkdir(parents=True, exist_ok=True)
    (root / "session.json").write_text(json.dumps(envelope))


def random_b64(size: int) -> str:
    return base64.b64encode(os.urandom(size)).decode()


@pytest.mark.parametrize(
    "envelope",
    [
        # Shipped before keyring backends: always passphrase-encrypted.
        {"version": 1, "salt": random_b64(16)},
        # Keyring backend `file`, removed with the session passphrase.
        {"version": 2, "backend": "file", "salt": random_b64(16)},
    ],
)
def test_passphrase_session_asks_to_sign_in_again(env: Env, envelope) -> None:
    envelope |= {"nonce": random_b64(12), "ciphertext": random_b64(64)}
    write_envelope(env.home, envelope)
    # A terminal on stdin proves the CLI fails instead of prompting.
    code, stdout, stderr = env.run_tty("auth", "status", typed=[])
    assert code != 0
    assert "passphrase-encrypted" in stderr
    assert "run `sai auth import` or `sai auth login NAME` again" in stderr
    assert "assphrase:" not in stderr
    assert stdout == ""


def test_removed_passphrase_options_are_rejected(env: Env) -> None:
    for flags in (["--keyring-backend", "file"], ["--passphrase-fd", "0"]):
        result = env.run(*flags, "auth", "status")
        assert result.returncode == 2
        assert "No such option" in result.stderr


def test_session_envelope_uses_the_built_in_key(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token)
    result = env.run("--json", "auth", "import")
    assert result.returncode == 0, result.stderr
    path = env.home / "trader" / "sessions.json"
    assert path.stat().st_mode & 0o777 == 0o600
    envelope = json.loads(path.read_text())
    assert set(envelope) == {"version", "nonce", "ciphertext"}
    assert envelope["version"] == 3
    assert token not in path.read_text()


def write_v2_session(home: Path, session: dict[str, str]) -> None:
    """Store SESSION the way sai did before multiple accounts."""
    nonce = os.urandom(12)
    key = hashlib.sha256(b"sai-cli keyring-backend test; not a secret").digest()
    ciphertext = AESGCM(key).encrypt(
        nonce, json.dumps(session).encode(), b"sai-session-v2-test"
    )
    write_envelope(
        home,
        {
            "version": 2,
            "backend": "test",
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        },
    )


def test_single_session_from_before_accounts_migrates(env: Env) -> None:
    write_v2_session(env.home, session_for(make_token()))
    status = env.run("--json", "auth", "status")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["addr"] == ADDR
    # The next sign-in rewrites it as version 3 and keeps the old account.
    other = "0x" + "ab" * 20
    env.clipboard()
    env.clip.write_text(make_token(addr=other))
    assert env.run("auth", "import").returncode == 0
    trader = env.home / "trader"
    assert not (trader / "session.json").exists()
    rows = json.loads(env.run("--json", "auth", "list").stdout)["accounts"]
    assert {r["addr"] for r in rows} == {ADDR, other}


def import_as(env: Env, addr: str) -> str:
    token = make_token(addr=addr)
    env.clipboard()
    env.clip.write_text(token)
    result = env.run("--json", "auth", "import")
    assert result.returncode == 0, result.stderr
    return token


def test_accounts_connect_switch_and_disconnect(env: Env) -> None:
    first, second = ADDR, "0x" + "ab" * 20
    tokens = [import_as(env, first), import_as(env, second)]

    def rows() -> dict[str, bool]:
        result = env.run("--json", "auth", "list")
        assert result.returncode == 0, result.stderr
        assert not any(t in result.stdout for t in tokens)
        return {r["addr"]: r["active"] for r in json.loads(result.stdout)["accounts"]}

    # Signing in adds an account and makes it active.
    assert rows() == {first: False, second: True}
    status = env.run("--json", "auth", "status")
    assert json.loads(status.stdout)["addr"] == second

    # --as acts as another connected account for one command.
    status = env.run(
        "--json", "--as", first.upper().replace("0X", "0x"), "auth", "status"
    )
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["addr"] == first
    assert rows() == {first: False, second: True}

    used = env.run("--json", "auth", "use", first)
    assert used.returncode == 0, used.stderr
    assert rows() == {first: True, second: False}

    unknown = env.run("auth", "use", "0x" + "cd" * 20)
    assert unknown.returncode != 0
    assert "is not connected" in unknown.stderr
    assert env.run("--as", "0x" + "cd" * 20, "auth", "status").returncode != 0

    # Logging out of one account keeps the other.
    out = env.run("--json", "auth", "logout", second)
    assert json.loads(out.stdout) | {"note": ""} == {
        "removed": True,
        "addr": second,
        "note": "",
    }
    assert rows() == {first: True}

    # Removing the active account leaves none active.
    import_as(env, second)
    assert env.run("auth", "logout").returncode == 0
    assert rows() == {first: False}
    status = env.run("auth", "status")
    assert status.returncode != 0
    assert "No active Sai account" in status.stderr


def test_tampered_session_cannot_be_unlocked(env: Env) -> None:
    write_envelope(
        env.home,
        {
            "version": 2,
            "backend": "test",
            "nonce": random_b64(12),
            "ciphertext": random_b64(64),
        },
    )
    result = env.run("auth", "status")
    assert result.returncode != 0
    assert "Cannot unlock session; run `sai auth import`" in result.stderr


def typed_data(**overrides) -> dict:
    data: dict[str, Any] = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "UserTradingSession": [
                {"name": "account", "type": "address"},
                {"name": "challenge", "type": "bytes32"},
                {"name": "action", "type": "string"},
                {"name": "expiresAt", "type": "string"},
            ],
        },
        "primaryType": "UserTradingSession",
        "domain": {"name": "Sai", "version": "1", "chainId": 6900},
        "message": {
            "account": ADDR,
            "challenge": "0x" + NONCE,
            "action": "Enable Sai trading",
            "expiresAt": "2026-11-17T00:05:00Z",
        },
    }
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            data[section][field] = value
        else:
            data[section] = value
    return data


@pytest.mark.parametrize(
    "overrides",
    [
        {"primaryType": "Permit"},
        {"domain__name": "Uniswap"},
        {"domain__version": "2"},
        {"message__account": OTHER},
        {"message__challenge": "0x" + "34" * 32},
        {"message": {}},
    ],
)
def test_check_session_typed_data_rejects_other_requests(overrides) -> None:
    with pytest.raises(keeper.KeeperError, match="not a Sai trading session"):
        keeper.check_session_typed_data(typed_data(**overrides), ADDR, NONCE)


def test_check_session_typed_data_accepts_keeper_challenge() -> None:
    keeper.check_session_typed_data(typed_data(), ADDR.upper(), NONCE.upper())


@pytest.fixture
def keeper_auth():
    """Serve Keeper's challenge and session mutations on loopback."""
    state: dict[str, Any] = {
        "typed": typed_data(),
        "token": make_token(),
        "signatures": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "createUserIntentAuthChallenge" in body["query"]:
                data = {
                    "createUserIntentAuthChallenge": {
                        "nonce": NONCE,
                        "evmAddress": body["variables"]["address"],
                        "expiresAt": "2026-11-17T00:05:00Z",
                        "chainId": 6900,
                        "typedData": state["typed"],
                    }
                }
            else:
                state["signatures"].append(body["variables"])
                data = {
                    "createUserIntentSession": {
                        "token": state["token"],
                        "evmAddress": ADDR,
                        "expiresAt": "2026-12-17T00:00:00+00:00",
                    }
                }
            encoded = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["endpoint"] = f"http://127.0.0.1:{server.server_port}/query"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def fake_cast(env: Env) -> None:
    env.tool(
        "cast",
        "import json\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['wallet', 'address']:\n"
        f"    print({ADDR!r})\n"
        "elif args[:2] == ['wallet', 'sign']:\n"
        "    data = json.load(open(args[-1]))\n"
        "    assert data['primaryType'] == 'UserTradingSession'\n"
        f"    print({SIGNATURE!r})\n"
        "else:\n"
        "    sys.exit(2)\n",
    )


def keystore(env: Env, name: str = "dev", addr: str | None = None) -> None:
    """Create the Foundry keystore file NAME; fake cast holds the key.

    With ADDR, the file records it in plaintext like Foundry's own files.
    """
    root = env.home / ".foundry" / "keystores"
    root.mkdir(parents=True, exist_ok=True)
    value = {} if addr is None else {"address": addr[2:].lower(), "crypto": {}}
    (root / name).write_text(json.dumps(value))


def test_cast_login_derives_addr_and_signs(env: Env, keeper_auth) -> None:
    fake_cast(env)
    keystore(env)
    result = env.run(
        "--json",
        "--endpoint",
        keeper_auth["endpoint"],
        "auth",
        "login",
        "dev",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["addr"] == ADDR
    assert keeper_auth["token"] not in result.stdout + result.stderr
    calls = env.log.read_text().splitlines()
    assert calls[0].endswith("cast wallet address --account dev")
    assert " wallet sign --account dev --data --from-file " in calls[1]
    assert keeper_auth["signatures"] == [{"signature": SIGNATURE, "challenge": NONCE}]
    status = env.run("--json", "--endpoint", keeper_auth["endpoint"], "auth", "status")
    assert status.returncode == 0, status.stderr


def test_keystore_addr_skips_the_cast_address_prompt(env: Env, keeper_auth) -> None:
    fake_cast(env)
    keystore(env, addr=ADDR)
    result = login(env, keeper_auth, "dev")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["addr"].lower() == ADDR.lower()
    calls = env.log.read_text().splitlines()
    assert len(calls) == 1
    assert " wallet sign --account dev --data --from-file " in calls[0]


@pytest.mark.parametrize("recorded", [True, False])
def test_keystore_login_checks_addr_before_signing(
    env: Env, keeper_auth, recorded: bool
) -> None:
    fake_cast(env)
    keystore(env, addr=ADDR if recorded else None)
    result = login(env, keeper_auth, "dev", "--addr", OTHER)
    assert result.returncode != 0
    assert "does not match the wallet addr" in login_error(result)
    assert "wallet sign" not in env.log.read_text()
    assert keeper_auth["signatures"] == []


def test_cast_login_refuses_foreign_typed_data(env: Env, keeper_auth) -> None:
    fake_cast(env)
    keystore(env, addr=ADDR)
    keeper_auth["typed"] = typed_data(primaryType="Permit")
    result = env.run(
        "--endpoint",
        keeper_auth["endpoint"],
        "auth",
        "login",
        "dev",
        "--addr",
        ADDR,
    )
    assert result.returncode != 0
    assert "not a Sai trading session request" in result.stderr
    assert env.log.read_text() == ""
    assert keeper_auth["signatures"] == []


def test_login_requires_a_name_and_drops_the_old_flags(env: Env) -> None:
    missing = env.run("auth", "login", "--addr", ADDR)
    assert missing.returncode == 2
    assert "Missing argument 'NAME'" in missing.stderr
    for flag in ("--account", "--wallet"):
        old = env.run("auth", "login", flag, "dev")
        assert old.returncode == 2
        assert "No such option" in old.stderr


def test_login_help_explains_keys_and_import(env: Env) -> None:
    result = env.run("auth", "login", "--help")
    assert result.returncode == 0
    for text in ("~/.foundry/keystores", "`ud`", "sai auth import", "Examples:"):
        assert text in result.stdout


def test_login_without_a_key_named_name_says_where_it_looked(
    env: Env, keeper_auth
) -> None:
    fake_cast(env)
    result = login(env, keeper_auth, "nope")
    assert result.returncode != 0
    assert "no Foundry keystore ~/.foundry/keystores/nope" in login_error(result)
    assert "sai auth import" in login_error(result)
    assert env.log.read_text() == ""
    assert keeper_auth["signatures"] == []


# Stand-in for a value that ud keeps in `pass` and must never reach output.
UD_SECRET = "ud-keystore-password"


def fake_ud(
    env: Env,
    known: str = "dev",
    state: str = "matching",
    addr: str = ADDR,
    locked: bool = False,
    down: bool = False,
    rotate: bool = False,
) -> None:
    """A ud that knows wallet KNOWN as ADDR, whose Cast keystore is in STATE.

    `show` prints ud's real JSON shape, plus a log line and a secret on
    stderr, as a noisy real tool might. Like real ud, `run` signs only when
    the state is `matching`. With LOCKED, its password does not open the
    keystore, so `run` fails even then; with ROTATE, the keystore file is
    also replaced by OTHER's while `run` is busy. With DOWN, `show` fails
    the way a credential store that will not open does.
    """
    shown = {
        "name": known,
        "address": addr.upper().replace("0X", "0x"),
        "notes": "",
        "cast": {
            "name": known,
            "address": addr.upper().replace("0X", "0x"),
            "state": state,
            "keystorePath": f"/keystores/{known}",
            "duplicateNames": [],
        },
    }
    env.tool(
        "ud",
        "import json\n"
        "args = sys.argv[1:]\n"
        f"if args[:3] == ['evm', 'wallet', 'show'] and {down!r}:\n"
        "    sys.exit('Error: gpg: decryption failed: No secret key')\n"
        f"elif args[:3] == ['evm', 'wallet', 'show'] and args[3] == {known!r}:\n"
        f"    sys.stderr.write({UD_SECRET!r} + '\\n')\n"
        "    print('Reading wallet document')\n"
        f"    print(json.dumps({shown!r}))\n"
        f"elif args[:4] == ['evm', 'wallet', 'run', {known!r}] and args[4:7] == "
        "['--', 'wallet', 'sign']:\n"
        f"    if {state!r} != 'matching':\n"
        "        sys.exit('wallet is not synchronized with Cast')\n"
        f"    if {rotate!r}:\n"
        f"        open({str(env.home / '.foundry' / 'keystores' / known)!r}, 'w')"
        f".write(json.dumps({{'address': {OTHER[2:]!r}, 'crypto': {{}}}}))\n"
        f"    if {locked!r}:\n"
        "        sys.exit('cast could not decrypt the keystore')\n"
        "    assert args[7:9] == ['--data', '--from-file']\n"
        "    data = json.load(open(args[-1]))\n"
        "    assert data['primaryType'] == 'UserTradingSession'\n"
        f"    print({SIGNATURE!r})\n"
        "elif args[:3] == ['evm', 'wallet', 'show']:\n"
        "    sys.exit(f'Error: wallet {args[3]} is not in pass entry evm/wallets')\n"
        "else:\n"
        "    sys.exit(1)\n",
    )


def login(env: Env, keeper_auth, *args: str) -> subprocess.CompletedProcess[str]:
    return env.run(
        "--json", "--endpoint", keeper_auth["endpoint"], "auth", "login", *args
    )


def login_error(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode != 0
    return json.loads(result.stdout)["error"]["message"]


def test_renew_uses_saved_name_and_preserves_active_account(env: Env, keeper_auth):
    fake_cast(env)
    keystore(env, name="saved-key", addr=ADDR)
    endpoint = keeper_auth["endpoint"]
    assert login(env, keeper_auth, "saved-key").returncode == 0
    env.clipboard()
    env.clip.write_text(make_token(OTHER))
    assert env.run("--endpoint", endpoint, "auth", "import").returncode == 0

    keeper_auth["token"] = make_token(hours=1440)
    renewed = env.run("--json", "--endpoint", endpoint, "auth", "renew", ADDR)
    assert renewed.returncode == 0, renewed.stderr
    assert json.loads(renewed.stdout)["addr"] == ADDR
    assert keeper_auth["token"] not in renewed.stdout + renewed.stderr
    assert env.log.read_text().count(" wallet sign --account saved-key ") == 2
    status = env.run("--json", "--endpoint", endpoint, "auth", "status")
    assert json.loads(status.stdout)["addr"] == OTHER

    refused = env.run("--endpoint", endpoint, "auth", "renew")
    assert refused.returncode == 1
    assert 'sai --endpoint "${SAI_RECONCILE_ENDPOINT:?' in refused.stderr
    assert "auth import" in refused.stderr
    assert endpoint not in refused.stderr
    assert len(keeper_auth["signatures"]) == 2


def test_renew_refuses_a_key_that_changed_address(env: Env, keeper_auth):
    fake_cast(env)
    keystore(env, addr=ADDR)
    assert login(env, keeper_auth, "dev").returncode == 0
    keystore(env, addr=OTHER)
    result = env.run("--endpoint", keeper_auth["endpoint"], "auth", "renew")
    assert result.returncode == 1
    assert "does not match the wallet addr" in result.stderr
    assert len(keeper_auth["signatures"]) == 1


def test_cli_status_and_list_report_expiry_warnings(env: Env):
    env.clipboard()
    env.clip.write_text(make_token(hours=48))
    assert env.run("auth", "import").returncode == 0
    status = env.run("--json", "auth", "status")
    result = env.run("--json", "auth", "list")
    row = json.loads(result.stdout)["accounts"][0]
    assert row["expiryWarning"] == json.loads(status.stdout)["expiryWarning"]
    renewal = row["renewalCommand"]
    assert renewal == "sai auth import"
    assert status.stderr == result.stderr == ""


@pytest.mark.parametrize("configured", [False, True])
def test_paste_renewal_command_with_the_configured_keeper(
    env: Env, keeper_auth, configured: bool
):
    if configured:
        endpoint = f"{keeper_auth['endpoint']}/private-path"
        assert env.run("config", "set", "keeper.endpoint", endpoint).returncode == 0
    env.clipboard()
    env.clip.write_text(make_token(hours=48))
    assert env.run("auth", "import").returncode == 0
    report = json.loads(env.run("--json", "auth", "status").stdout)
    assert report["renewalCommand"] == "sai auth import"
    assert "renewalGuidance" not in report

    env.clip.write_text(make_token())
    renewed = env.run_shell(report["renewalCommand"])
    assert renewed.returncode == 0, renewed.stderr
    assert json.loads(renewed.stdout)["expiresAt"] > report["expiresAt"]
    assert env.run("--json", "auth", "status").returncode == 0


@pytest.mark.parametrize("json_output", [False, True])
def test_expiry_notice_uses_stderr_on_a_terminal(env: Env, json_output: bool):
    env.clipboard()
    env.clip.write_text(make_token(hours=48))
    assert env.run("auth", "import").returncode == 0
    primary, secondary = pty.openpty()
    flags = ["--json"] if json_output else []
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "sai_cli",
                *flags,
                "config",
                "get",
                "keeper.endpoint",
            ],
            env={**env.vars(), "CI": "1"},
            stdin=secondary,
            stderr=secondary,
            stdout=subprocess.PIPE,
            text=True,
            timeout=15,
            check=True,
        )
        os.set_blocking(primary, False)
        try:
            error = os.read(primary, 4096).decode()
        except BlockingIOError:
            error = ""
    finally:
        os.close(primary)
        os.close(secondary)
    assert "expires in" not in result.stdout
    if json_output:
        assert error == ""
    else:
        assert len(error.splitlines()) == 1
        assert "expires in" in error
        assert error.splitlines()[0].endswith("Run sai auth import")


@pytest.mark.parametrize(
    "suffix",
    ["/query?api_key=synthetic-test-credential", "/synthetic-test-credential/query"],
)
def test_session_hint_preserves_endpoint_context_without_exposing_credentials(
    env: Env, suffix: str
):
    env.clipboard()
    env.clip.write_text(make_token(hours=48))
    origin = "http://127.0.0.1:9876"
    endpoint = origin + suffix
    args = ("--json", "--endpoint", endpoint, "auth")
    assert env.run(*args, "import").returncode == 0
    status = env.run(*args, "status")
    row = json.loads(status.stdout)
    assert row["endpoint"] == origin
    assert shlex.split(row["renewalCommand"]) == [
        "sai",
        "--endpoint",
        "${SAI_RECONCILE_ENDPOINT:?Set SAI_RECONCILE_ENDPOINT to the original Keeper URL}",
        "auth",
        "import",
    ]
    assert f"Reuse the Keeper endpoint at {origin}" in row["renewalGuidance"]
    refused = env.run(*args, "renew")
    assert refused.returncode == 1
    assert f"Reuse the Keeper endpoint at {origin}" in login_error(refused)
    assert (
        "synthetic-test-credential"
        not in status.stdout + refused.stdout + refused.stderr
    )


def test_renew_json_shows_only_the_keeper_origin(env: Env, keeper_auth):
    fake_cast(env)
    keystore(env, addr=ADDR)
    origin = keeper_auth["endpoint"].removesuffix("/query")
    endpoint = f"{origin}/synthetic-test-credential/query"
    args = ("--json", "--endpoint", endpoint, "auth")
    assert env.run(*args, "login", "dev").returncode == 0
    result = env.run(*args, "renew")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["endpoint"] == origin
    assert "synthetic-test-credential" not in result.stdout


def test_wallet_login_reads_addr_from_ud(env: Env, keeper_auth) -> None:
    fake_ud(env)
    result = login(env, keeper_auth, "dev")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["addr"] == ADDR
    assert UD_SECRET not in result.stdout + result.stderr
    assert keeper_auth["token"] not in result.stdout + result.stderr
    calls = env.log.read_text().splitlines()
    assert calls[0].endswith("ud evm wallet show dev")
    assert " evm wallet run dev -- wallet sign --data --from-file " in calls[1]
    assert keeper_auth["signatures"] == [{"signature": SIGNATURE, "challenge": NONCE}]
    status = env.run("--json", "--endpoint", keeper_auth["endpoint"], "auth", "status")
    assert status.returncode == 0, status.stderr


def test_wallet_login_cross_checks_addr(env: Env, keeper_auth) -> None:
    fake_ud(env)
    same = login(env, keeper_auth, "dev", "--addr", "0x" + ADDR[2:].upper())
    assert same.returncode == 0, same.stderr
    env.log.write_text("")
    other = login(env, keeper_auth, "dev", "--addr", OTHER)
    assert other.returncode != 0
    assert "does not match the wallet addr" in login_error(other)
    assert "wallet run" not in env.log.read_text()
    assert len(keeper_auth["signatures"]) == 1


def test_login_uses_the_keystore_when_ud_does_not_know_the_name(
    env: Env, keeper_auth
) -> None:
    fake_ud(env)
    fake_cast(env)
    keystore(env, "other")
    result = login(env, keeper_auth, "other")
    assert result.returncode == 0, result.stderr
    calls = env.log.read_text().splitlines()
    assert calls[0].endswith("ud evm wallet show other")
    assert calls[1].endswith("cast wallet address --account other")
    assert " wallet sign --account other --data --from-file " in calls[2]
    assert len(keeper_auth["signatures"]) == 1


def test_ud_wallet_signs_before_the_keystore_of_the_same_name(
    env: Env, keeper_auth
) -> None:
    # ud keeps the password for keystore `dev`, so it signs without a prompt.
    fake_ud(env)
    fake_cast(env)
    keystore(env)
    result = login(env, keeper_auth, "dev")
    assert result.returncode == 0, result.stderr
    assert "/cast " not in env.log.read_text()
    assert " evm wallet run dev -- wallet sign " in env.log.read_text()


def test_login_without_ud_signs_with_the_keystore(env: Env, keeper_auth) -> None:
    fake_cast(env)
    keystore(env)
    result = login(env, keeper_auth, "dev")
    assert result.returncode == 0, result.stderr
    assert "/ud " not in env.log.read_text()
    assert len(keeper_auth["signatures"]) == 1


@pytest.mark.parametrize(
    "state, hint",
    [
        ("missing", "run `ud evm wallet sync dev` to create it"),
        ("name-conflict", "another keystore already uses that name"),
    ],
)
def test_wallet_login_names_an_unsigned_cast_state(
    env: Env, keeper_auth, state: str, hint: str
) -> None:
    fake_ud(env, state=state)
    fake_cast(env)
    result = login(env, keeper_auth, "dev")
    assert result.returncode != 0
    assert f"Cast keystore state {state}" in login_error(result)
    assert hint in login_error(result)
    calls = env.log.read_text()
    assert "wallet run" not in calls
    assert "/cast " not in calls
    assert keeper_auth["signatures"] == []


def test_ud_login_refuses_foreign_typed_data(env: Env, keeper_auth) -> None:
    fake_ud(env)
    keeper_auth["typed"] = typed_data(primaryType="Permit")
    result = login(env, keeper_auth, "dev")
    assert result.returncode != 0
    assert "not a Sai trading session request" in login_error(result)
    assert "wallet run" not in env.log.read_text()
    assert keeper_auth["signatures"] == []


def ud_show(addr: object, state: object = "matching") -> str:
    return json.dumps({"name": "a", "address": addr, "cast": {"state": state}})


@pytest.mark.parametrize(
    "output, expected",
    [
        (ud_show(ADDR), keeper.UdWallet(ADDR, "matching")),
        (ud_show(ADDR, "name-conflict"), keeper.UdWallet(ADDR, "name-conflict")),
        (json.dumps({"address": ADDR}), keeper.UdWallet(ADDR, None)),
        (ud_show(ADDR, 3), keeper.UdWallet(ADDR, None)),
        (
            "note {not json}\n" + json.dumps({"x": {"address": 1}, "address": OTHER}),
            keeper.UdWallet(OTHER, None),
        ),
        (
            'log line\n{"name": "a"}\n' + ud_show(ADDR),
            keeper.UdWallet(ADDR, "matching"),
        ),
        (ud_show("0x123"), None),
        (ud_show("0x" + "g" * 40), None),
        ("", None),
    ],
)
def test_parse_wallet(output: str, expected: keeper.UdWallet | None) -> None:
    assert keeper.parse_wallet(output) == expected


def test_login_rejects_malformed_addr_before_signing(env: Env, keeper_auth) -> None:
    fake_cast(env)
    keystore(env)
    result = login(env, keeper_auth, "dev", "--addr", "0xTYPO")
    assert result.returncode != 0
    assert "Not an EVM addr: 0xTYPO" in login_error(result)
    assert "cast" not in env.log.read_text()
    assert keeper_auth["signatures"] == []


def test_login_stops_when_ud_and_the_keystore_hold_different_accounts(
    env: Env, keeper_auth
) -> None:
    # ud's `dev` is OTHER; the Foundry keystore `dev` holds ADDR.
    fake_ud(env, state="name-conflict", addr=OTHER)
    fake_cast(env)
    keystore(env, addr=ADDR)
    result = login(env, keeper_auth, "dev")
    assert result.returncode != 0
    assert "dev names two accounts" in login_error(result)
    assert OTHER[2:].upper() in login_error(result) and ADDR in login_error(result)
    assert f"sai auth login dev --addr {ADDR}" in login_error(result)
    assert "ud evm wallet sync dev" in login_error(result)
    assert "/cast " not in env.log.read_text()
    assert keeper_auth["signatures"] == []
    listed = env.run("--json", "auth", "list")
    assert ADDR not in listed.stdout


def test_addr_picks_the_keystore_account_in_a_name_conflict(
    env: Env, keeper_auth
) -> None:
    fake_ud(env, state="name-conflict", addr=OTHER)
    fake_cast(env)
    keystore(env, addr=ADDR)
    # ud cannot sign with its own account while the names collide.
    as_ud = login(env, keeper_auth, "dev", "--addr", OTHER)
    assert as_ud.returncode != 0
    assert "Cast keystore state name-conflict" in login_error(as_ud)
    neither = login(env, keeper_auth, "dev", "--addr", "0x" + "d" * 40)
    assert neither.returncode != 0
    assert "matches neither" in login_error(neither)
    assert "wallet sign" not in env.log.read_text()
    assert keeper_auth["signatures"] == []

    picked = login(env, keeper_auth, "dev", "--addr", ADDR)
    assert picked.returncode == 0, picked.stderr
    assert json.loads(picked.stdout)["addr"] == ADDR
    calls = env.log.read_text()
    assert " wallet sign --account dev --data --from-file " in calls
    assert "wallet run" not in calls


def test_cast_signs_in_a_terminal_when_ud_cannot_unlock_the_keystore(
    env: Env, keeper_auth
) -> None:
    fake_ud(env, locked=True)
    fake_cast(env)
    keystore(env, addr=ADDR)
    args = ("--json", "--endpoint", keeper_auth["endpoint"], "auth", "login", "dev")
    code, out, err = env.run_tty(*args, typed=[])
    assert code == 0, err
    assert json.loads(out)["addr"] == ADDR
    assert "ud could not sign with dev" in err
    calls = env.log.read_text().splitlines()
    assert " evm wallet run dev -- wallet sign " in calls[1]
    assert " wallet sign --account dev --data --from-file " in calls[2]
    assert keeper_auth["signatures"] == [{"signature": SIGNATURE, "challenge": NONCE}]


@pytest.mark.parametrize(
    "private_path", [False, True], ids=["plain-url", "private-path"]
)
def test_ud_signing_failure_without_a_terminal_says_to_use_one(
    env: Env, keeper_auth, private_path: bool
) -> None:
    fake_ud(env, locked=True)
    fake_cast(env)
    keystore(env, addr=ADDR)
    endpoint = keeper_auth["endpoint"]
    secret = "test-keeper-path-key"
    if private_path:
        endpoint = f"{endpoint.removesuffix('/query')}/{secret}/query"
    result = env.run(
        "--json", "--endpoint", endpoint, "auth", "login", "dev", "--addr", ADDR
    )
    assert result.returncode == 3
    # The whole command, so a retry keeps the endpoint and --addr.
    assert secret not in result.stdout + result.stderr
    assert endpoint not in result.stdout + result.stderr
    endpoint_arg = (
        '"${SAI_RECONCILE_ENDPOINT:?'
        'Set SAI_RECONCILE_ENDPOINT to the original Keeper URL}"'
    )
    command = f"sai --endpoint {endpoint_arg} --json auth login dev --addr {ADDR}"
    assert f"Run `{command}` in a terminal." in login_error(result)
    assert json.loads(result.stdout)["error"]["hint"] == command
    assert "/cast " not in env.log.read_text()
    assert keeper_auth["signatures"] == []


def test_ud_signing_failure_without_a_keystore_file_fails(
    env: Env, keeper_auth
) -> None:
    fake_ud(env, locked=True)
    fake_cast(env)
    result = login(env, keeper_auth, "dev")
    assert result.returncode != 0
    assert "Wallet signing failed" in login_error(result)
    assert "/cast " not in env.log.read_text()


def test_cast_does_not_sign_when_the_keystore_changed_under_the_name(
    env: Env, keeper_auth
) -> None:
    # ud looks up `dev` as ADDR, then the file is replaced by OTHER's key
    # before ud's signing fails. cast finds keystores by name.
    fake_ud(env, locked=True, rotate=True)
    fake_cast(env)
    keystore(env, addr=ADDR)
    args = ("--json", "--endpoint", keeper_auth["endpoint"], "auth", "login", "dev")
    code, out, _ = env.run_tty(*args, "--addr", ADDR, typed=[])
    assert code != 0
    message = json.loads(out)["error"]["message"]
    assert f"now holds {OTHER}, not " in message
    assert "nothing was signed" in message
    assert "/cast " not in env.log.read_text()
    assert keeper_auth["signatures"] == []
    assert ADDR not in env.run("--json", "auth", "list").stdout


def test_a_session_for_another_account_than_the_challenge_is_not_stored(
    env: Env, keeper_auth
) -> None:
    # Keeper's fixture issues its session for ADDR whoever signed, as it
    # would if cast signed with a key other than the keystore file records.
    fake_cast(env)
    keystore(env, addr=OTHER)
    challenge = typed_data()
    challenge["message"]["account"] = OTHER
    keeper_auth["typed"] = challenge
    result = login(env, keeper_auth, "dev")
    assert result.returncode != 0
    assert len(keeper_auth["signatures"]) == 1
    assert "Session addr does not match the requested account" in login_error(result)
    listed = env.run("--json", "auth", "list").stdout
    assert ADDR not in listed and OTHER not in listed


def test_login_stops_when_ud_cannot_say_whether_it_knows_the_name(
    env: Env, keeper_auth
) -> None:
    # ud may hold another account as `dev`; its lookup fails, so sai cannot
    # rule out a conflict with the keystore.
    fake_ud(env, addr=OTHER, down=True)
    fake_cast(env)
    keystore(env, addr=ADDR)
    result = login(env, keeper_auth, "dev")
    assert result.returncode != 0
    assert "`ud evm wallet show dev` failed" in login_error(result)
    assert f"`--addr {ADDR}`" in login_error(result)
    assert "gpg" not in login_error(result)
    wrong = login(env, keeper_auth, "dev", "--addr", OTHER)
    assert wrong.returncode != 0
    assert "/cast " not in env.log.read_text()
    assert keeper_auth["signatures"] == []

    picked = login(env, keeper_auth, "dev", "--addr", ADDR)
    assert picked.returncode == 0, picked.stderr
    assert json.loads(picked.stdout)["addr"] == ADDR


def test_login_stops_when_ud_fails_and_there_is_no_keystore(
    env: Env, keeper_auth
) -> None:
    fake_ud(env, down=True)
    result = login(env, keeper_auth, "dev")
    assert result.returncode != 0
    assert "check that ud works" in login_error(result)
    assert keeper_auth["signatures"] == []


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Error: wallet dev is not in pass entry evm/wallets\n", True),
        ("wallet dev is not in pass entry evm/wallets", True),
        ("warning: slow\nError: wallet dev is not in pass entry evm/wallets\n", True),
        # Another wallet's name, or other text that mentions an unknown wallet,
        # is a failed lookup and must not look like a confirmed absence.
        ("Error: wallet other is not in pass entry evm/wallets\n", False),
        ("Error: invalid table `unknown wallet` in evm/wallets\n", False),
        ("Error: no such wallet store\n", False),
        (
            "Error: wallet dev is not in pass entry evm/wallets\nError: gpg failed\n",
            False,
        ),
        ("", False),
    ],
)
def test_ud_says_unknown_matches_only_the_exact_message(stderr, expected) -> None:
    assert keeper.ud_says_unknown("dev", stderr) is expected
