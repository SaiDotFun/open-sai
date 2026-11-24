"""Exercise the installed CLI against an isolated GraphQL HTTP service."""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sai_cli.core import keeper, log, storage
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve
from websockets.typing import Subprotocol

FIXTURES = Path(__file__).parent / "fixtures"
TRADE_ADDR = "0x" + "a" * 40


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def save_trade_session(endpoint: str) -> None:
    storage.save_session(
        {
            "token": "fake-token",
            "evmAddress": TRADE_ADDR,
            "endpoint": endpoint,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
    )


@contextmanager
def submission_keeper(reply: Callable[[str], dict | None]) -> Generator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["query"] == keeper.SUBMIT
            response = reply(body["variables"]["input"]["intentKey"])
            if response is not None:
                payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/query"
        finally:
            server.shutdown()
            thread.join()


def close_trade_args(endpoint: str) -> list[str]:
    return [*_resolve_cli(), "--endpoint", endpoint, "--json", "trade", "close", "7"]


@pytest.fixture
def fail_ledger_updates() -> None:
    with storage.db() as connection:
        connection.execute(
            "CREATE TRIGGER reject_status_write BEFORE UPDATE OF status ON intents "
            "BEGIN SELECT RAISE(ABORT, 'ledger write failed'); END"
        )


def _resolve_cli() -> list[str]:
    path = shutil.which("sai")
    if path:
        return [path]
    if os.environ.get("SAI_FORCE_INSTALLED") == "1":
        raise RuntimeError("Installed sai command is required")
    import sys

    return [sys.executable, "-m", "sai_cli"]


@pytest.fixture
def keeper_server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert "borrowings" in body["query"]
            result = json.dumps(
                {"data": {"perp": {"borrowings": [{"marketId": 0, "price": 100.0}]}}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/query"
    server.shutdown()
    server.server_close()
    thread.join()


def test_installed_sai_command_help():
    result = subprocess.run(
        [*_resolve_cli(), "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "Usage: sai " in result.stdout
    assert "market" in result.stdout
    assert "trade" in result.stdout


def test_installed_cli_queries_local_keeper(keeper_server):
    result = subprocess.run(
        [*_resolve_cli(), "--endpoint", keeper_server, "--json", "market", "list"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout)["perp"]["borrowings"] == [
        {"marketId": 0, "price": 100.0}
    ]


def test_installed_cli_receives_balance_snapshot():
    golden = fixture("synthetic_protocol.json")["account_balance_ws_payload"]

    def handler(connection):
        assert json.loads(connection.recv())["type"] == "connection_init"
        connection.send(json.dumps({"type": "connection_ack"}))
        assert json.loads(connection.recv())["type"] == "subscribe"
        connection.send(
            json.dumps(
                {
                    "id": "1",
                    "type": "next",
                    "payload": golden,
                }
            )
        )

    server = serve(
        handler, "127.0.0.1", 0, subprotocols=[Subprotocol("graphql-transport-ws")]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.socket.getsockname()[1]
        result = subprocess.run(
            [
                *_resolve_cli(),
                "--json",
                "account",
                "balances",
                "0xabc",
                "--ws-endpoint",
                f"ws://127.0.0.1:{port}/query",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert json.loads(result.stdout) == golden
        assert [
            row["token_info"]["name"] for row in golden["data"]["userBalances"]
        ] == ["USDC on Sai", "USDC Credits on Sai"]
    finally:
        server.shutdown()
        thread.join()


def test_installed_watch_replays_observed_btc_websocket_snapshot():
    golden = fixture("mainnet_price_ws_payload.json")

    def handler(connection):
        assert json.loads(connection.recv())["type"] == "connection_init"
        connection.send(json.dumps({"type": "connection_ack"}))
        request = json.loads(connection.recv())
        assert request["type"] == "subscribe"
        assert request["payload"]["variables"] == {"id": 3}
        connection.send(json.dumps({"id": "1", "type": "next", "payload": golden}))
        try:
            connection.recv()
        except ConnectionClosed:
            pass

    server = serve(
        handler, "127.0.0.1", 0, subprotocols=[Subprotocol("graphql-transport-ws")]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.socket.getsockname()[1]
    process = subprocess.Popen(
        [
            *_resolve_cli(),
            "--json",
            "watch",
            "price",
            "3",
            "--ws-endpoint",
            f"ws://127.0.0.1:{port}/query",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()

    def read_first_line():
        assert process.stdout is not None
        lines.put(process.stdout.readline())

    reader = threading.Thread(target=read_first_line, daemon=True)
    reader.start()
    try:
        assert json.loads(lines.get(timeout=10)) == golden
    finally:
        process.terminate()
        process.wait(timeout=10)
        reader.join(timeout=10)
        server.shutdown()
        thread.join()


def test_lost_admission_survives_restart_and_reconciles(tmp_path, monkeypatch):
    """Keeper drops the admission reply and later reports a pending fill."""
    addr = "0x" + "a" * 40
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    recorded = []
    history_visible = False

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            query = request["query"]
            if "recordUserIntent" in query:
                assert self.headers["Authorization"] == "Bearer fake-token"
                recorded.append(request["variables"]["input"])
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if "userIntents" in query:
                key = recorded[0]["intentKey"]
                data = {
                    "userIntents": [
                        {
                            "intentKey": key,
                            "evmAddress": addr,
                            "execActor": "server",
                            "kind": "CLOSE_POSITION",
                            "lifecycleStep": "exec_pass"
                            if history_visible
                            else "pending_exec",
                            "txHash": "A" * 64,
                        }
                    ]
                }
            elif "tradeHistory" in query:
                data = {
                    "perp": {
                        "tradeHistory": (
                            [
                                {
                                    "intentKey": recorded[0]["intentKey"],
                                    "txHash": "A" * 64,
                                    "trade": {"id": 7, "trader": addr},
                                }
                            ]
                            if history_visible
                            else []
                        )
                    }
                }
            else:
                raise AssertionError(query)
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
    endpoint = f"http://127.0.0.1:{server.server_port}/query"
    storage.save_session(
        {
            "token": "fake-token",
            "evmAddress": addr,
            "endpoint": endpoint,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )

    def invoke(*args):
        return subprocess.run(
            [*_resolve_cli(), "--endpoint", endpoint, "--json", *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

    try:
        first = invoke("trade", "close", "7")
        assert len(recorded) == 1
        key = recorded[0]["intentKey"]
        assert first.returncode == 5 and first.stderr == ""
        error = json.loads(first.stdout)["error"]
        assert error == {
            "code": "OUTCOME_UNKNOWN",
            "message": f"Outcome unknown for intent {key}; reconcile before another trade",
            "retryable": False,
            "intentKey": key,
            "hint": f"sai --endpoint {endpoint} intent status {addr} --key {key}",
        }
        saved = json.loads(invoke("intent", "local", addr).stdout)
        assert saved[0]["intentKey"] == key
        assert saved[0]["status"] == "unknown"

        duplicate = invoke("trade", "close", "7")
        assert duplicate.returncode == 1
        blocked = json.loads(duplicate.stdout)["error"]
        assert blocked["intentKey"] == key and blocked["retryable"] is False
        assert blocked["hint"] == error["hint"]
        assert "Reconcile" in blocked["message"]
        assert len(recorded) == 1

        status = invoke("intent", "status", addr, "--key", key)
        assert status.returncode == 0
        assert (
            json.loads(invoke("intent", "local", addr).stdout)[0]["status"] == "pending"
        )
        assert json.loads(status.stdout)["outcome"] == "pending"
        assert invoke("trade", "close", "7").returncode != 0
        assert len(recorded) == 1

        history_visible = True
        status = invoke("intent", "status", addr, "--key", key)
        assert status.returncode == 0
        assert (
            json.loads(invoke("intent", "local", addr).stdout)[0]["status"]
            == "terminal"
        )
        after_reconciliation = invoke("trade", "close", "7")
        assert after_reconciliation.returncode == 5
        assert (
            json.loads(after_reconciliation.stdout)["error"]["code"]
            == "OUTCOME_UNKNOWN"
        )
        assert len(recorded) == 2
        assert recorded[1]["intentKey"] != key
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("acknowledged", [True, False])
def test_keeper_result_survives_a_failed_ledger_write(
    fail_ledger_updates, acknowledged
):
    requests = []

    def reply(key: str) -> dict:
        requests.append(key)
        if not acknowledged:
            return {"errors": [{"extensions": {"code": "INTENT_NOT_ACCEPTED"}}]}
        return {
            "data": {
                "recordUserIntent": {
                    "intentKey": key,
                    "lifecycleStep": "jwt_pass",
                    "execActor": "server",
                    "evmAddress": TRADE_ADDR,
                }
            }
        }

    with submission_keeper(reply) as endpoint:
        save_trade_session(endpoint)
        result = subprocess.run(
            close_trade_args(endpoint),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    [key] = requests
    assert result.returncode == (5 if acknowledged else 4), (
        result.stderr or result.stdout
    )
    error = json.loads(result.stdout)["error"]
    assert error["code"] == (
        "OUTCOME_UNKNOWN" if acknowledged else "ADMISSION_REJECTED"
    )
    assert error["retryable"] is (not acknowledged)
    assert error["intentKey"] == key
    assert (
        error["hint"]
        == f"sai --endpoint {endpoint} intent status {TRADE_ADDR} --key {key}"
    )
    assert storage.list_pending(endpoint, TRADE_ADDR)[0]["status"] == "unknown"


def test_connect_refusal_survives_a_failed_ledger_write(fail_ledger_updates):
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{unavailable.getsockname()[1]}/query"
        save_trade_session(endpoint)
        result = subprocess.run(
            close_trade_args(endpoint),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 4, result.stderr
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "NOT_SENT" and error["retryable"] is True
    [entry] = storage.list_pending(endpoint, TRADE_ADDR)
    assert entry["intentKey"] == error["intentKey"] and entry["status"] == "unknown"


@pytest.mark.parametrize("credential_in_path", [False, True], ids=["query", "path"])
def test_reconciliation_hints_keep_endpoint_credentials_private(credential_in_path):
    with submission_keeper(lambda _key: None) as base:
        secret = "fixture-endpoint-secret"
        if credential_in_path:
            endpoint = f"{base.removesuffix('/query')}/{secret}/query"
        else:
            endpoint = f"{base}?api_key={secret}"
        save_trade_session(endpoint)
        attempts = [
            subprocess.run(
                close_trade_args(endpoint),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            for _ in range(2)
        ]
    received, blocked = attempts
    assert (received.returncode, blocked.returncode) == (5, 1)
    for result in attempts:
        assert secret not in result.stdout + result.stderr
        assert endpoint not in result.stdout + result.stderr
    error, block = [json.loads(result.stdout)["error"] for result in attempts]
    assert error["hint"] == block["hint"]
    assert 'sai --endpoint "${SAI_RECONCILE_ENDPOINT:?' in error["hint"]
    assert error["hint"].endswith(
        f"intent status {TRADE_ADDR} --key {error['intentKey']}"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess signals")
def test_sigint_after_keeper_receives_intent_reports_unknown():
    requests = queue.Queue()
    release = threading.Event()

    def reply(key: str) -> None:
        requests.put(key)
        release.wait(timeout=15)

    with submission_keeper(reply) as endpoint:
        save_trade_session(endpoint)
        with subprocess.Popen(
            close_trade_args(endpoint),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            try:
                key = requests.get(timeout=10)
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=10)
            finally:
                release.set()
                if process.poll() is None:
                    process.kill()
    assert process.returncode == 5 and stderr == ""
    error = json.loads(stdout)["error"]
    assert error["code"] == "OUTCOME_UNKNOWN" and error["intentKey"] == key
    assert (
        error["hint"]
        == f"sai --endpoint {endpoint} intent status {TRADE_ADDR} --key {key}"
    )
    [entry] = storage.list_pending(endpoint, TRADE_ADDR)
    assert entry["intentKey"] == key and entry["status"] == "unknown"


def test_two_processes_cannot_reserve_same_position(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    with storage.db():
        pass
    program = """
import sys
from sai_cli.core import storage
sys.stdin.read(1)
try:
    storage.pending(sys.argv[1], 'https://example.com/query', '0xabc', 'CLOSE_POSITION', '7')
    print('reserved')
except ValueError:
    print('blocked')
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", program, f"key-{i}"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(2)
    ]
    for proc in processes:
        assert proc.stdin is not None
        proc.stdin.write("x")
        proc.stdin.close()
        proc.stdin = None
    results = [proc.communicate(timeout=10) for proc in processes]
    assert all(proc.returncode == 0 for proc in processes), results
    assert sorted(stdout.strip() for stdout, _ in results) == ["blocked", "reserved"]
    assert len(storage.list_pending("https://example.com/query", "0xabc")) == 1


def test_watch_reconnects_and_emits_a_fresh_snapshot():
    connections = 0

    def handler(connection):
        nonlocal connections
        connections += 1
        assert json.loads(connection.recv())["type"] == "connection_init"
        connection.send(json.dumps({"type": "connection_ack"}))
        assert json.loads(connection.recv())["type"] == "subscribe"
        connection.send(
            json.dumps(
                {
                    "id": "1",
                    "type": "next",
                    "payload": {
                        "data": {"userBalances": [{"amount": str(connections)}]}
                    },
                }
            )
        )
        if connections == 1:
            connection.close()
        else:
            try:
                connection.recv(timeout=10)
            except (ConnectionClosed, TimeoutError):
                return

    server = serve(
        handler, "127.0.0.1", 0, subprotocols=[Subprotocol("graphql-transport-ws")]
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.socket.getsockname()[1]
    proc = subprocess.Popen(
        [
            *_resolve_cli(),
            "watch",
            "balances",
            "0xabc",
            "--ws-endpoint",
            f"ws://127.0.0.1:{port}/query",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines = queue.Queue()

    def read_lines():
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    try:
        first = json.loads(lines.get(timeout=10))
        second = json.loads(lines.get(timeout=10))
        assert first["data"]["userBalances"][0]["amount"] == "1"
        assert second["data"]["userBalances"][0]["amount"] == "2"
        assert connections == 2
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        reader.join(timeout=10)
        server.shutdown()
        server_thread.join()


def test_watch_prints_a_final_error_after_streamed_rows():
    payload = fixture("mainnet_price_ws_payload.json")

    def handler(connection):
        assert json.loads(connection.recv())["type"] == "connection_init"
        connection.send(json.dumps({"type": "connection_ack"}))
        assert json.loads(connection.recv())["type"] == "subscribe"
        connection.send(json.dumps({"id": "1", "type": "next", "payload": payload}))
        connection.send(json.dumps({"id": "1", "type": "complete"}))

    with serve(
        handler, "127.0.0.1", 0, subprotocols=[Subprotocol("graphql-transport-ws")]
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.socket.getsockname()[1]
            result = subprocess.run(
                [
                    *_resolve_cli(),
                    "--json",
                    "watch",
                    "price",
                    "1",
                    "--ws-endpoint",
                    f"ws://127.0.0.1:{port}/query",
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
        finally:
            server.shutdown()
            thread.join()
    assert result.returncode == 1
    row, final = map(json.loads, result.stdout.splitlines())
    assert row == payload
    assert final == {
        "error": {
            "code": "STREAM_ERROR",
            "message": "Keeper subscription ended",
            "retryable": False,
        }
    }


def test_balance_snapshot_fails_on_graphql_errors():
    def handler(connection):
        assert json.loads(connection.recv())["type"] == "connection_init"
        connection.send(json.dumps({"type": "connection_ack"}))
        assert json.loads(connection.recv())["type"] == "subscribe"
        connection.send(
            json.dumps(
                {
                    "id": "1",
                    "type": "next",
                    "payload": {
                        "data": {"userBalances": []},
                        "errors": [{"message": "partial failure"}],
                    },
                }
            )
        )

    server = serve(
        handler, "127.0.0.1", 0, subprotocols=[Subprotocol("graphql-transport-ws")]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.socket.getsockname()[1]
        result = subprocess.run(
            [
                *_resolve_cli(),
                "--json",
                "account",
                "balances",
                "0xabc",
                "--ws-endpoint",
                f"ws://127.0.0.1:{port}/query",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "STREAM_ERROR" and error["retryable"] is False
        assert "incomplete data" in error["message"]
    finally:
        server.shutdown()
        thread.join()


def test_unrecorded_intent_expires_and_unblocks_close(tmp_path, monkeypatch):
    """Keeper answers 401 and never records the key; the CLI recovers."""
    addr = "0x" + "a" * 40
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    recorded = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            query = request["query"]
            if "recordUserIntent" in query:
                recorded.append(request["variables"]["input"])
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            assert "userIntents" in query, query
            encoded = json.dumps({"data": {"userIntents": []}}).encode()
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
    endpoint = f"http://127.0.0.1:{server.server_port}/query"
    storage.save_session(
        {
            "token": "fake-token",
            "evmAddress": addr,
            "endpoint": endpoint,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )

    def invoke(*args):
        return subprocess.run(
            [*_resolve_cli(), "--endpoint", endpoint, "--json", *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

    try:
        first = invoke("trade", "tpsl", "7", "--take-profit", "100")
        assert first.returncode == 5
        assert json.loads(first.stdout)["error"]["code"] == "OUTCOME_UNKNOWN"
        key = recorded[0]["intentKey"]

        # Inside Keeper's admission window the key still blocks trading.
        fresh = invoke("intent", "status", addr, "--key", key)
        assert json.loads(fresh.stdout)["localStatus"] == "unknown"
        blocked = invoke("trade", "close", "7")
        assert "Reconcile" in json.loads(blocked.stdout)["error"]["message"]
        assert len(recorded) == 1

        # Age the reservation past the admission expiry plus margin.
        with storage.db() as connection:
            connection.execute(
                "UPDATE intents SET created_at=? WHERE id=?",
                (time.time() - keeper.ABSENT_AFTER_SECONDS - 1, key),
            )
        expired = invoke("intent", "status", addr, "--key", key)
        assert expired.returncode == 0
        assert json.loads(expired.stdout)["localStatus"] == "absent"

        allowed = invoke("trade", "close", "7")
        assert allowed.returncode == 5
        assert len(recorded) == 2
        assert recorded[1]["intent"]["kind"] == "CLOSE_POSITION"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_repl_survives_a_failing_command_and_shows_help(tmp_path):
    env = {**os.environ, "SAI_HOME": str(tmp_path)}
    env.pop("TELEGRAM_PASSWORD", None)
    # A passphrase-encrypted session from an older version fails without a
    # prompt, and the shell keeps reading commands.
    trader = tmp_path / "trader"
    trader.mkdir()
    (trader / "session.json").write_text(json.dumps({"version": 1}))
    result = subprocess.run(
        _resolve_cli(),
        input="auth status\nhelp\n",
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
        env=env,
        start_new_session=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Error: Stored session is passphrase-encrypted" in result.stderr
    assert "Usage: sai" in result.stdout


@pytest.mark.parametrize("in_repl", [False, True], ids=["cli", "repl"])
def test_bare_command_help_reports_usage_exit_in_process_and_log(
    tmp_path: Path, in_repl: bool
) -> None:
    command = [] if in_repl else ["trade", "open"]
    result = subprocess.run(
        [*_resolve_cli(), *command],
        input="trade open\nhelp config\nexit\n" if in_repl else "",
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
        env={**os.environ, "SAI_HOME": str(tmp_path)},
    )
    assert "Options:" in result.stderr
    assert "--slippage" in result.stderr
    if in_repl:
        assert result.returncode == 0
        assert "Usage: sai config" in result.stdout
    else:
        assert result.returncode == 2
        assert result.stdout == ""

    command_events = []
    for _, _, path in log.log_files(tmp_path / "logs"):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("command") == "trade open":
                command_events.append(event)
    assert [event["event"] for event in command_events] == [
        "command.start",
        "command.end",
    ]
    assert command_events[-1]["exitCode"] == 2
