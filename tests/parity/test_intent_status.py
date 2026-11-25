"""Compare intent status through both CLIs against Keeper's recorded replays."""

from __future__ import annotations

import gzip
import json
import os
import select
import shutil
import signal
import socket
import sqlite3
import ssl
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from stat import S_IMODE
from typing import Any

import pytest
from compare import Normalize, validate_log_endpoint_key
from release_mismatch import assert_release_equal
from sai_cli.core import keeper, log, storage
from sai_cli.tests.loopback import graphql_ws
from websockets.http11 import Request as UpgradeRequest
from websockets.http11 import Response as UpgradeResponse
from websockets.sync.server import ServerConnection

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "cli/sai_cli/tests/fixtures/intent_outcomes.json"
    ).read_text(encoding="utf-8")
)
ADDR = FIXTURE["address"]
KEY = FIXTURE["key"]
RESPONSES = {**FIXTURE["recorded"], **FIXTURE["replays"]}
NIBI = RESPONSES["pending_exec"]["data"]["userIntents"][0]["nibiAddress"]
OTHER = "0x" + "b" * 40
OTHER_NIBI = "nibi1hwamhwamhwamhwamhwamhwamhwamhwamc9gs8a"
CLI = tuple[str, ...]


@pytest.fixture
def c11_cli_pair(cli_pair: tuple[CLI, CLI], tmp_path: Path) -> tuple[CLI, CLI]:
    version = invoke(cli_pair[0], tmp_path / "version-home", "--version")
    assert version.returncode == 0, version.stderr
    if version.stdout.strip() != b"sai, version 0.1.2":
        pytest.skip("C11 uses the Python v0.1.2 reference in tests/parity/README.md")
    return cli_pair


@dataclass(frozen=True)
class Reply:
    body: bytes = b""
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()
    body_delay: float = 0
    header_delay: float = 0
    disconnect: bool = False


@dataclass(frozen=True)
class Request:
    path: str
    version: str
    headers: dict[str, str]
    body: bytes


ReplayReply = Reply | Callable[[Request], Reply]


@contextmanager
def replay(
    tls: ssl.SSLContext | None = None,
    *,
    get: Callable[[Request], Reply] | None = None,
) -> Iterator[tuple[str, deque[ReplayReply], list[Request]]]:
    replies: deque[ReplayReply] = deque()
    requests: list[Request] = []

    class Handler(BaseHTTPRequestHandler):
        def record(self, body: bytes) -> Request:
            request = Request(
                self.path,
                self.request_version,
                {name.lower(): value for name, value in self.headers.items()},
                body,
            )
            requests.append(request)
            return request

        def do_POST(self) -> None:
            request = self.record(self.rfile.read(int(self.headers["Content-Length"])))
            if not replies:
                self.send_error(500, "Unexpected Keeper request")
                return
            reply = replies.popleft()
            if callable(reply):
                reply = reply(request)
            self.send_reply(reply)

        def do_GET(self) -> None:
            if get is None:
                self.send_error(501, "Unexpected LCD request")
                return
            self.send_reply(get(self.record(b"")))

        def send_reply(self, reply: Reply) -> None:
            if reply.disconnect:
                self.close_connection = True
                return
            self.send_response(reply.status)
            if reply.header_delay:
                self.flush_headers()
                time.sleep(reply.header_delay)
                self.send_header("Content-Type", "application/json")
                self.flush_headers()
                time.sleep(reply.header_delay)
            for name, value in reply.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            time.sleep(reply.body_delay)
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(reply.body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        if tls is not None:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        scheme = "https" if tls is not None else "http"
        worker = threading.Thread(
            target=server.serve_forever, args=(0.01,), daemon=True
        )
        worker.start()
        try:
            yield f"{scheme}://127.0.0.1:{server.server_port}/query", replies, requests
        finally:
            server.shutdown()
            worker.join()


def invoke(
    cli: CLI,
    home: Path,
    *args: str,
    overrides: dict[str, str] | None = None,
    cwd: Path | None = None,
    umask: int = -1,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*cli, *args],
        env=cli_environment(home, overrides),
        cwd=cwd or home.parent,
        capture_output=True,
        check=False,
        timeout=30,
        umask=umask,
    )


def cli_environment(
    home: Path, overrides: dict[str, str] | None = None
) -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": str(home.parent),
        "SAI_HOME": str(home),
        "SAI_LOG": "1",
        "SAI_UPDATE_AUTO": "0",
        "PYTHONUTF8": "1",
        **(overrides or {}),
    }


@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
@pytest.mark.parametrize(
    "source",
    [
        "http-flag",
        "http-env",
        "http-config",
        "http-flag-space",
        "http-env-space",
        "http-config-space",
        "http-flag-control",
        "http-flag-leading-space",
        "http-flag-ipvfuture",
        "ws-fragment",
        "ws-global-empty",
        "ws-local-empty",
        "ws-config-empty",
        "ws-invalid-scheme",
        "ws-invalid-port",
        "ws-backslash-authority",
    ],
)
def test_c11_rejects_endpoints_before_network_io(
    c11_cli_pair: tuple[CLI, CLI], tmp_path: Path, source: str, json_output: bool
) -> None:
    upgrades: list[str] = []

    def reject_stream(connection: ServerConnection) -> None:
        # An unexpected upgrade ends the CLI immediately after connection_init.
        connection.recv(timeout=5)
        connection.send('{"type":"connection_error"}')

    def record_upgrade(_connection: ServerConnection, request: UpgradeRequest) -> None:
        upgrades.append(request.path)

    with (
        replay() as (http, _replies, requests),
        graphql_ws(reject_stream, process_request=record_upgrade) as ws,
    ):
        for index, cli in enumerate(c11_cli_pair):
            home = tmp_path / str(index)
            home.mkdir(mode=0o700)
            config = home / "config.toml"
            flags = ["--json"] if json_output else []
            env = {"SAI_ENDPOINT": http, "SAI_WS_ENDPOINT": ws}
            if source.startswith("http-"):
                command = ["market", "list"]
                detail = "Invalid value for keeper.endpoint: remove the URL fragment"
                layer, _, malformed = source.removeprefix("http-").partition("-")
                value = http + ("#" if layer == "env" else "#client")
                if malformed == "ipvfuture":
                    value = "https://[v1.foo]/query"
                    detail = (
                        "Invalid value for keeper.endpoint: expected a URL "
                        "starting with https:// or http://"
                    )
                elif malformed:
                    detail = (
                        "Invalid value for keeper.endpoint: URL contains whitespace "
                        "or control characters"
                    )
                    value = {
                        "space": http + "/has space",
                        "control": http + "/has\x80control",
                        "leading-space": " " + http,
                    }[malformed]
                if layer == "flag":
                    flags += ["--endpoint", value]
                    origin = "--endpoint"
                elif layer == "env":
                    env["SAI_ENDPOINT"] = value
                    origin = "SAI_ENDPOINT"
                else:
                    config.write_text(f'[keeper]\nendpoint = "{value}"\n')
                    origin = str(config)
            else:
                command = ["watch", "balances", ADDR]
                detail = (
                    "Invalid value for keeper.ws_endpoint: expected a URL starting "
                    "with wss:// or ws://"
                )
                if source == "ws-fragment":
                    command = [
                        "account",
                        "balances",
                        ADDR,
                        "--ws-endpoint",
                        ws + "#client",
                    ]
                    detail = (
                        "Invalid value for keeper.ws_endpoint: remove the URL fragment"
                    )
                    origin = ""
                elif source == "ws-global-empty":
                    flags += ["--ws-endpoint", ""]
                    origin = "--ws-endpoint"
                elif source == "ws-local-empty":
                    command += ["--ws-endpoint", ""]
                    origin = ""
                elif source == "ws-config-empty":
                    config.write_text('[keeper]\nws_endpoint = ""\n')
                    origin = str(config)
                else:
                    invalid_ws = {
                        "ws-invalid-scheme": http,
                        "ws-invalid-port": "ws://127.0.0.1:65536/query",
                        "ws-backslash-authority": ws.replace(
                            "/query", "\\example/query"
                        ),
                    }[source]
                    command = ["account", "balances", ADDR, "--ws-endpoint", invalid_ws]
                    origin = ""
            message = f"{origin}: {detail}" if origin else detail
            result = invoke(cli, home, *flags, *command, overrides=env)
            assert requests == []
            assert upgrades == []
            assert result.returncode == 2, (result.stdout, result.stderr)
            if json_output:
                assert result.stderr == b""
                assert json.loads(result.stdout) == {
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": message,
                        "retryable": False,
                    }
                }
            else:
                assert result.stdout == b""
                assert result.stderr == f"Error: {message}\n".encode()


@pytest.mark.parametrize(
    ("host", "accepted"),
    [
        ("☃.localhost", False),
        ("ｂücher.example", False),
        ("bu\u0308cher.example", False),
        ("b·cher.example", False),
        ("bücher._example", False),
        ("bücher..example", False),
        ("K_.example", False),
        ("K..example", False),
        ("K.example", True),
        ("Bücher.example", True),
        ("例え。テスト.", True),
        ("l·l.example", True),
        ("bücher.xn--bcher-kva", True),
        ("bücher.xn--n3h", False),
        ("999.0.0.1", False),
        ("127.0.0.01", False),
        ("192.168.001.1", False),
        ("0177.0.0.1", False),
        ("127.0.256.1", False),
        ("127.0.0.1", True),
        ("0.0.0.0", True),
        ("255.255.255.255", True),
        ("host_1.localhost", True),
        ("123.45.67.example", True),
    ],
)
def test_c11_http_hostname(
    c11_cli_pair: tuple[CLI, CLI], tmp_path: Path, host: str, accepted: bool
) -> None:
    endpoint = f"https://{host}/query"
    for index, cli in enumerate(c11_cli_pair):
        result = invoke(
            cli,
            tmp_path / str(index),
            "--json",
            "--endpoint",
            endpoint,
            "config",
            "get",
            "keeper.endpoint",
            overrides={"SAI_LOG": "0"},
        )
        assert result.stderr == b""
        assert result.returncode == (0 if accepted else 2)
        if accepted:
            assert json.loads(result.stdout) == {
                "key": "keeper.endpoint",
                "value": endpoint,
                "source": "flag",
            }
        else:
            assert json.loads(result.stdout) == {
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": (
                        "--endpoint: Invalid value for keeper.endpoint: expected "
                        "a URL starting with https:// or http://"
                    ),
                    "retryable": False,
                }
            }


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks and directory modes")
def test_c11_log_writer_leaves_symlink_target_untouched(
    c11_cli_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    # A Keeper read would create both the log and its endpoint key in a fresh home.
    data = {"perp": {"borrowings": []}}
    for index, cli in enumerate(c11_cli_pair):
        target = tmp_path / f"target-{index}"
        target.mkdir(mode=0o750)
        home = tmp_path / f"home-{index}"
        home.symlink_to(target, target_is_directory=True)
        with replay() as (endpoint, replies, requests):
            replies.append(response({"data": data}))
            result = invoke(
                cli, home, "--json", "--endpoint", endpoint, "market", "list"
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert json.loads(result.stdout) == data
            assert result.stderr == b""
            assert [request.body for request in requests] == [wire(keeper.MARKETS)]
        assert home.is_symlink()
        assert S_IMODE(target.stat().st_mode) == 0o750
        assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX umask and directory modes")
@pytest.mark.parametrize("writer", ["logs", "config", "trader", "existing-home"])
def test_c11_first_write_restores_owner_directory_mode(
    c11_cli_pair: tuple[CLI, CLI], tmp_path: Path, writer: str
) -> None:
    profiles = []
    for index, cli in enumerate(c11_cli_pair):
        parent = tmp_path / str(index) / "ancestor"
        home = parent / "home"
        inherited_home = writer == "existing-home"
        if inherited_home:
            home.mkdir(parents=True, mode=0o750)
        overrides = {} if writer == "logs" else {"SAI_LOG": "0"}
        with replay() as (endpoint, replies, requests):
            flags = ["--json", "--endpoint", endpoint]
            if writer == "logs":
                # Reject the key before reading settings while the log thread
                # creates the profile at first use.
                args = ["config", "get", "unknown"]
            elif writer == "trader":
                args = ["intent", "status", ADDR, "--key", KEY]
                replies.append(response({"data": {"userIntents": []}}))
            else:
                args = ["config", "set", "logs.keep_days", "14"]
            result = invoke(
                cli, home, *flags, *args, overrides=overrides, cwd=tmp_path, umask=0o777
            )
            assert result.returncode == (2 if writer == "logs" else 0), result.stdout
            assert result.stderr == b""
            assert not replies
            assert len(requests) == (writer == "trader")
        if not inherited_home:
            assert S_IMODE(parent.parent.stat().st_mode) == 0o700
            assert S_IMODE(parent.stat().st_mode) == 0o700
        assert S_IMODE(home.stat().st_mode) == (0o750 if inherited_home else 0o700)
        if writer in {"logs", "trader"}:
            assert S_IMODE((home / writer).stat().st_mode) == 0o700
        profiles.append(
            {
                str(child.relative_to(home)): S_IMODE(child.stat().st_mode)
                for child in home.rglob("*")
            }
        )
    assert profiles[0] == profiles[1]


def read_events(home: Path) -> list[dict[str, Any]]:
    events = [
        json.loads(line)
        for path in sorted((home / "logs").glob("*.jsonl"))
        for line in path.read_bytes().splitlines()
    ]
    groups: dict[str, list[int]] = {}
    for event in events:
        groups.setdefault(event["runId"], []).append(event["seq"])
    assert all(seq == list(range(1, len(seq) + 1)) for seq in groups.values())
    return events


def normalized_events(home: Path) -> list[dict[str, Any]]:
    normalize = Normalize(home)
    # The request and ledger assertions use this fixed fixture key.
    normalize.identity("intentKey", KEY)
    return normalize.log_records(read_events(home))


def test_log_events_detect_wrong_fixture_intent_key(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "cli-2026-11-20.jsonl"
    event = {
        "ts": "2026-11-20T15:43:31.123Z",
        "runId": "a" * 32,
        "seq": 1,
        "event": "intent.keeper",
        "intentKey": KEY,
    }
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert read_events(tmp_path)[0]["intentKey"] == KEY
    reference = normalized_events(tmp_path)

    path.write_text(
        json.dumps({**event, "intentKey": "20261120T154331123Z_" + "A" * 16}) + "\n",
        encoding="utf-8",
    )
    assert normalized_events(tmp_path) != reference


def read_state(home: Path) -> tuple[Any, ...]:
    files = {
        str(path.relative_to(home)): path.stat().st_mode for path in home.rglob("*")
    }
    if "logs/.endpoint-key" in files:
        validate_log_endpoint_key(home / "logs/.endpoint-key")
    db = home / "trader/intents.sqlite3"
    if not db.exists():
        return (files,)
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        schema = connection.execute("SELECT sql FROM sqlite_master ORDER BY name")
        version = connection.execute("PRAGMA user_version").fetchone()
        rows = connection.execute("SELECT * FROM intents ORDER BY id")
        return files, schema.fetchall(), version, rows.fetchall()


def test_read_state_rejects_short_endpoint_key(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    key = log_dir / ".endpoint-key"
    key.write_bytes(b"x" * 32)
    key.chmod(0o600)
    read_state(tmp_path)

    key.write_bytes(b"x")
    with pytest.raises(ValueError, match="Expected a 32-byte log endpoint key"):
        read_state(tmp_path)


def seed(
    home: Path, endpoint: str, monkeypatch: pytest.MonkeyPatch, initial: str
) -> None:
    monkeypatch.setenv("SAI_HOME", str(home))
    with log.silenced():
        storage.pending(KEY, endpoint, ADDR, "OPEN_TRADE", "17")
        storage.pending("foreign", endpoint, OTHER, "OPEN_TRADE", "17")
        if initial in {"accepted", "pending", "terminal"}:
            storage.set_status(KEY, endpoint, ADDR, initial)
        with closing(storage.db()) as connection, connection:
            # A fixed, expired timestamp makes the two ledgers comparable.
            connection.execute("UPDATE intents SET created_at=1")
            if initial in {"absent", "legacy_terminal"}:
                connection.execute(
                    "UPDATE intents SET status=? WHERE id=?",
                    (initial.removeprefix("legacy_"), KEY),
                )


def response(payload: str | dict[str, Any] | Reply) -> Reply:
    if isinstance(payload, Reply):
        return payload
    if isinstance(payload, str):
        payload = RESPONSES[payload]
    return Reply(json.dumps(payload).encode())


def wire(query: str, **variables: str | int | None) -> bytes:
    return json.dumps(
        {"query": query, "variables": variables},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


@dataclass(frozen=True)
class Step:
    replies: tuple[str | dict[str, Any] | Reply, ...]
    local: str | None
    key: str | None = KEY
    addr: str = ADDR
    code: int = 0
    error: str | None = None
    json_output: bool = True


def compare(
    cli_pair: tuple[CLI, CLI],
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    steps: tuple[Step, ...],
    initial: str | None = "unknown",
) -> None:
    with replay() as (endpoint, replies, requests):
        fixture = root / "fixture"
        fixture.mkdir(mode=0o700)
        if initial is not None:
            seed(fixture, endpoint, monkeypatch, initial)
        observed = []
        for index, cli in enumerate(cli_pair):
            home = root / str(index)
            shutil.copytree(fixture, home)
            runs = []
            for step in steps:
                replies.extend(response(item) for item in step.replies)
                requests.clear()
                args = ["--endpoint", endpoint]
                if step.json_output:
                    args.append("--json")
                args.extend(("intent", "status", step.addr))
                if step.key is not None:
                    args.extend(("--key", step.key))
                result = invoke(cli, home, *args)
                first_request = wire(keeper.INTENTS, user=step.addr, key=step.key)
                history_request = wire(
                    keeper.INTENT_HISTORY, trader=step.addr, limit=25, offset=0
                )
                bodies = [request.body for request in requests]
                expected = [
                    first_request,
                    *[history_request] * (len(step.replies) - 1),
                ]
                if index == 0:
                    assert result.returncode == step.code, (
                        result.stdout,
                        result.stderr,
                    )
                assert not replies
                assert bodies == expected
                if step.json_output:
                    assert not result.stderr
                    assert len(result.stdout.splitlines()) == 1
                    document = json.loads(result.stdout)
                    if step.error:
                        assert document["error"]["code"] == step.error
                        assert document["error"]["retryable"] is (step.code == 4)
                state = read_state(home)
                if step.local is not None:
                    _, _, _, rows = state
                    _, _, _, _, stored, _, _, keeper_status = next(
                        row for row in rows if row[0] == KEY
                    )
                    assert (keeper_status or stored) == step.local
                    assert (
                        next(row for row in rows if row[0] == "foreign")[4] == "unknown"
                    )
                runs.append(
                    {
                        "exit": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "requests": [asdict(request) for request in requests],
                        "unconsumed": len(replies),
                        "files": state[0],
                        "ledger": state[1:],
                    }
                )
                replies.clear()
            observed.append({"runs": runs, "logs": normalized_events(home)})
        assert_release_equal(*observed)


@pytest.mark.parametrize(
    ("name", "local"),
    [
        pytest.param("started", "unknown", id="started"),
        pytest.param("jwt_failed", "unknown", id="jwt_failed"),
        pytest.param("user_rejected", "unknown", id="user_rejected"),
        pytest.param("user_sign_failed", "unknown", id="user_sign_failed"),
        pytest.param("user_submitted", "unknown", id="user_submitted"),
        pytest.param("accepted", "accepted", id="accepted"),
        pytest.param("submitted", "accepted", id="submitted"),
        pytest.param("pending_exec", "pending", id="pending_exec"),
        pytest.param("chain_execution_failed", "terminal", id="chain_execution_failed"),
        pytest.param("market_order_expired", "terminal", id="market_order_expired"),
        pytest.param(
            "market_order_fill_failed", "terminal", id="market_order_fill_failed"
        ),
        pytest.param(
            "market_order_trading_paused", "terminal", id="market_order_trading_paused"
        ),
        pytest.param(
            "market_order_market_closed", "terminal", id="market_order_market_closed"
        ),
        pytest.param(
            "market_order_trade_not_open", "terminal", id="market_order_trade_not_open"
        ),
        pytest.param("simulation_failed", "terminal", id="simulation_failed"),
        pytest.param("admission_expired", "terminal", id="admission_expired"),
        pytest.param("broadcast_rejected", "terminal", id="broadcast_rejected"),
        pytest.param("absent", "absent", id="absent"),
    ],
)
def test_lifecycle(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    local: str,
) -> None:
    compare(cli_pair, tmp_path, monkeypatch, (Step((name,), local),))


@pytest.mark.parametrize(
    ("history", "local"),
    [
        pytest.param(
            ("server_finality", "open_history"), "terminal", id="server-finality"
        ),
        pytest.param(
            ("user_finality", "not_yet_in_history", "not_yet_in_history"),
            "pending",
            id="user-finality-empty-history",
        ),
        pytest.param(
            ("other_trade_finality", "other_trade_history"),
            "terminal",
            id="other-trade-finality",
        ),
        pytest.param(
            ("other_trade_executed", "other_trade_history"),
            "terminal",
            id="other-trade-executed",
        ),
        pytest.param(
            ("limit_placement_exec_pass", "limit_placement_history"),
            "terminal",
            id="limit-placement",
        ),
        pytest.param(
            ("filled", "not_yet_in_history", "not_yet_in_history"),
            "terminal",
            id="filled-without-history",
        ),
        pytest.param(
            ("outcome_not_indexed", "not_yet_in_history", "filled_history"),
            "terminal",
            id="late-fill",
        ),
        pytest.param(
            ("outcome_not_indexed", "not_yet_in_history", "not_yet_in_history"),
            "pending",
            id="unresolved",
        ),
        pytest.param(
            ("outcome_not_indexed", Reply(status=503)),
            "pending",
            id="service-unavailable",
        ),
        pytest.param(
            (
                "outcome_not_indexed",
                Reply(response("not_yet_in_history").body, body_delay=2.25),
            ),
            "pending",
            id="body-timeout",
        ),
    ],
)
def test_history(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history: tuple[str | Reply, ...],
    local: str,
) -> None:
    compare(cli_pair, tmp_path, monkeypatch, (Step(history, local),))


def test_split_history_headers_keep_the_inactivity_timer_alive(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fill = Reply(response("filled_history").body, header_delay=1.2)
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (Step(("outcome_not_indexed", fill), "terminal"),),
    )


def test_malformed_history_page_stays_pending_without_retry(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = {"data": {"perp": {"tradeHistory": {}}}}
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (Step(("outcome_not_indexed", empty), "pending"),),
    )


@pytest.mark.parametrize(
    "key", [pytest.param(KEY, id="keyed"), pytest.param(None, id="unkeyed")]
)
def test_late_admission_and_stale_results(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str | None,
) -> None:
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (
            Step(("accepted",), "unknown", None),
            Step(("absent",), "absent"),
            Step(("accepted",), "pending", key),
            Step(("outcome_not_indexed",), "pending", None),
            Step(("submitted",), "pending"),
            Step(("absent",), "pending"),
            Step(("market_order_expired",), "pending", None),
            Step(("market_order_expired",), "terminal"),
            Step(("pending_exec",), "terminal"),
        ),
    )


def test_legacy_terminal_repairs_only_for_market_pending(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (
            Step(
                ("server_finality", "not_yet_in_history", "not_yet_in_history"),
                "terminal",
            ),
            Step(("pending_exec",), "pending"),
            Step(("market_order_expired",), "terminal"),
            Step(("pending_exec",), "terminal"),
        ),
        "legacy_terminal",
    )


def test_filters_foreign_rows_and_correlates_canonical_addresses(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = RESPONSES["pending_exec"]["data"]["userIntents"][0]
    invalid_rows = [
        {**row, "evmAddress": OTHER},
        {**row, "nibiAddress": OTHER_NIBI},
        {**row, "nibiAddress": NIBI[:-1] + "q"},
        {**row, "intentKey": "foreign"},
    ]
    foreign = {"data": {"userIntents": invalid_rows}}
    fill = RESPONSES["filled_history"]["data"]["perp"]["tradeHistory"][0]
    foreign_history = {
        "data": {
            "perp": {
                "tradeHistory": [
                    {**fill, "intentKey": "foreign"},
                    {**fill, "trade": {**fill["trade"], "trader": OTHER}},
                    {**fill, "txHash": "C" * 64, "evmTxHash": None},
                ]
            }
        }
    }
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (
            Step((foreign,), "unknown", addr=NIBI.upper()),
            Step(("pending_exec",), "pending", addr=NIBI.upper()),
            Step(
                ("outcome_not_indexed", foreign_history, "filled_history"),
                "terminal",
                addr=NIBI,
            ),
        ),
    )


def test_absent_response_only_creates_ledger_for_valid_keyed_account(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (
            Step(("absent",), None, None),
            Step(("absent",), None, addr="invalid"),
            Step(({"data": {"userIntents": None}},), None),
            Step(("pending_exec",), None, None, json_output=False),
            Step(("absent",), None),
        ),
        None,
    )


@pytest.mark.parametrize(
    ("replies", "local", "error"),
    [
        pytest.param(
            (Reply(status=302, headers=(("Location", "/elsewhere"),)),),
            "unknown",
            "KEEPER_ERROR",
            id="redirect",
        ),
        pytest.param(
            (Reply(status=401),), "unknown", "KEEPER_ERROR", id="unauthorized"
        ),
        pytest.param(
            (Reply(status=500),), "unknown", "KEEPER_ERROR", id="server-error"
        ),
        pytest.param(
            (Reply(disconnect=True),), "unknown", "KEEPER_ERROR", id="disconnect"
        ),
        pytest.param(
            (Reply(b"malformed"),), "unknown", "KEEPER_ERROR", id="invalid-json"
        ),
        pytest.param(
            (Reply(b"[]"),), "unknown", "KEEPER_ERROR", id="non-object-response"
        ),
        pytest.param(
            (Reply(b'{"data":null}'),), "unknown", "KEEPER_ERROR", id="null-data"
        ),
        pytest.param(
            ({"data": {"userIntents": 123}},), "unknown", None, id="non-list-intents"
        ),
        pytest.param(
            ({"data": {"userIntents": [None]}},), "unknown", None, id="null-intent-row"
        ),
        pytest.param(
            ("outcome_not_indexed", {"data": {}}), "pending", None, id="missing-history"
        ),
        pytest.param(
            ("outcome_not_indexed", {"data": {"perp": {"tradeHistory": None}}}),
            "pending",
            None,
            id="null-history",
        ),
    ],
)
def test_request_and_data_failures(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replies: tuple[str | dict[str, Any] | Reply, ...],
    local: str,
    error: str | None,
) -> None:
    step = Step(replies, local, code=1 if error else 0, error=error)
    compare(cli_pair, tmp_path, monkeypatch, (step,))


@pytest.mark.parametrize("with_record", [False, True], ids=["null-only", "mixed"])
def test_c11_intent_status_skips_null_records(
    c11_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_record: bool,
) -> None:
    record = RESPONSES["started"]["data"]["userIntents"][0]
    documents = []
    for index, cli in enumerate(c11_cli_pair):
        home = tmp_path / str(index)
        with replay() as (endpoint, replies, requests):
            seed(home, endpoint, monkeypatch, "unknown")
            ledger = read_state(home)[1:]
            rows = [None, record, None] if with_record else [None]
            replies.append(response({"data": {"userIntents": rows}}))
            result = invoke(
                cli,
                home,
                "--json",
                "--endpoint",
                endpoint,
                "intent",
                "status",
                ADDR,
                "--key",
                KEY,
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert result.stderr == b""
            assert [request.body for request in requests] == [
                wire(keeper.INTENTS, user=ADDR, key=KEY)
            ]
        document = json.loads(result.stdout)
        if with_record:
            [intent] = document["userIntents"]
            assert intent["intentKey"] == KEY
            assert intent["lifecycleStep"] == "intent_start"
        else:
            assert document == {"userIntents": []}
            assert all(event["event"] != "intent.keeper" for event in read_events(home))
        # Even an expired local reservation stays unknown when Keeper sent
        # a nonempty list with no usable record.
        assert read_state(home)[1:] == ledger
        documents.append(document)
    assert documents[0] == documents[1]


@pytest.mark.parametrize("operation", ["intents", "history"])
def test_intent_status_logs_only_public_graphql_codes(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, operation: str
) -> None:
    diagnostic = "synthetic Keeper diagnostic"
    failure = {
        "data": {"userIntents": [], "perp": {"tradeHistory": []}},
        "errors": [
            {"message": diagnostic, "extensions": {"code": "FORBIDDEN"}},
            {"message": diagnostic, "extensions": {"code": "PRIVATE_CODE"}},
            {"message": diagnostic},
            {"message": diagnostic, "extensions": {"code": "BAD_USER_INPUT"}},
        ],
    }
    with replay() as (endpoint, replies, requests):
        for index, cli in enumerate(cli_pair):
            home = tmp_path / str(index)
            requests.clear()
            expected_requests = [wire(keeper.INTENTS, user=ADDR, key=KEY)]
            if operation == "history":
                replies.append(response("outcome_not_indexed"))
                history_request = wire(
                    keeper.INTENT_HISTORY, trader=ADDR, limit=25, offset=0
                )
                expected_requests.append(history_request)
            replies.append(response(failure))
            result = invoke(
                cli,
                home,
                "--endpoint",
                endpoint,
                "--json",
                "intent",
                "status",
                ADDR,
                "--key",
                KEY,
            )
            assert not replies
            assert [request.body for request in requests] == expected_requests
            assert result.stderr == b""
            document = json.loads(result.stdout)
            if operation == "intents":
                assert result.returncode == 1
                assert document["error"]["code"] == "KEEPER_ERROR"
            else:
                assert result.returncode == 0
                assert document["outcome"] == "pending"
            [event] = [
                event
                for event in normalized_events(home)
                if event["event"] == "keeper.request"
                and event["operation"] == operation
            ]
            assert event["code"] == "KEEPER_ERROR"
            assert event["keeperErrors"] == [
                {"code": "FORBIDDEN"},
                {"code": "KEEPER_ERROR"},
                {"code": "KEEPER_ERROR"},
            ]
            assert event["keeperErrorCount"] == 4


def test_compressed_keeper_response(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compressed = Reply(
        gzip.compress(response("pending_exec").body),
        headers=(("Content-Encoding", "gzip"),),
    )
    compare(cli_pair, tmp_path, monkeypatch, (Step((compressed,), "pending"),))


def test_connection_refused(cli_pair: tuple[CLI, CLI], tmp_path: Path) -> None:
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{unavailable.getsockname()[1]}/query"
        outputs = []
        for index, cli in enumerate(cli_pair):
            home = tmp_path / str(index)
            result = invoke(
                cli, home, "--json", "--endpoint", endpoint, "intent", "status", ADDR
            )
            assert result.returncode == 4
            assert result.stderr == b""
            assert json.loads(result.stdout)["error"]["code"] == "NOT_SENT"
            assert not (home / "trader").exists()
            outputs.append((result.stdout, normalized_events(home)))
        assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    ("args", "exit_code", "code"),
    [
        pytest.param(("--json",), 2, "INVALID_REQUEST", id="root-usage"),
        pytest.param(
            ("--json", "intent"), 2, "INVALID_REQUEST", id="intent-group-usage"
        ),
        pytest.param(("--json", "config"), 2, "INVALID_REQUEST", id="config-usage"),
        pytest.param(
            ("--bad-option", "--json", "intent", "status", ADDR),
            2,
            "INVALID_REQUEST",
            id="unknown-option",
        ),
        pytest.param(
            ("--json", "intent", "status"), 2, "INVALID_REQUEST", id="status-usage"
        ),
        pytest.param(
            ("--json", "intent", "status", ADDR, "--key"),
            2,
            "INVALID_REQUEST",
            id="missing-key",
        ),
        pytest.param(
            ("--json", "config", "get", "invalid"),
            2,
            "INVALID_REQUEST",
            id="invalid-config",
        ),
        pytest.param(
            (
                "--json",
                "--endpoint",
                "http://keeper.invalid/query",
                "intent",
                "status",
                ADDR,
            ),
            1,
            "KEEPER_ERROR",
            id="http-endpoint",
        ),
        pytest.param(
            (
                "--json",
                "--endpoint",
                "https://user:pass@keeper.invalid/query",
                "intent",
                "status",
                ADDR,
            ),
            1,
            "KEEPER_ERROR",
            id="userinfo-endpoint",
        ),
        pytest.param(("--json", "auth", "status"), 6, "SIGNED_OUT", id="signed-out"),
    ],
)
def test_cli_errors_share_one_envelope(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    args: tuple[str, ...],
    exit_code: int,
    code: str,
) -> None:
    outputs = []
    for index, cli in enumerate(cli_pair):
        result = invoke(cli, tmp_path / str(index), *args)
        assert result.returncode == exit_code
        assert not result.stderr
        assert len(result.stdout.splitlines()) == 1
        payload = json.loads(result.stdout)
        assert payload["error"]["code"] == code
        if code == "SIGNED_OUT":
            origin = None if index == 0 else keeper.MAINNET_HTTP.removesuffix("/query")
            expected_hint = origin_login_hint(origin)
            assert payload["error"].pop("hint") == expected_hint
            outputs.append(payload)
        else:
            outputs.append(result.stdout)
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(("intent",), id="group"),
        pytest.param(("intent", "status"), id="status"),
    ],
)
def test_intent_help(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, command: tuple[str, ...]
) -> None:
    outputs = []
    for index, cli in enumerate(cli_pair):
        home = tmp_path / str(index)
        result = invoke(cli, home, *command, "--help")
        assert result.returncode == 0
        assert not result.stderr
        assert not home.exists()
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]


def origin_login_hint(origin: str | None) -> str:
    suffix = f" at {origin}" if origin else ""
    return (
        'sai --endpoint "${SAI_RECONCILE_ENDPOINT:?'
        f'Set SAI_RECONCILE_ENDPOINT to the original Keeper URL{suffix}}}"'
        " auth login NAME"
    )


@pytest.mark.parametrize("path", ["/query", "/private/query"])
def test_expired_auth_status(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    origin = "http://127.0.0.1:9"
    endpoint = origin + path
    fixture = tmp_path / "fixture"
    monkeypatch.setenv("SAI_HOME", str(fixture))
    with log.silenced():
        storage.save_session(
            {
                "evmAddress": ADDR,
                "endpoint": endpoint,
                "token": "fixture-token",
                "expiresAt": "2000-01-01T00:00:00Z",
            }
        )
    observed = []
    for index, cli in enumerate(cli_pair):
        home = tmp_path / str(index)
        shutil.copytree(fixture, home)
        result = invoke(cli, home, "--json", "--endpoint", endpoint, "auth", "status")
        assert result.returncode == 6
        assert result.stderr == b""
        payload = json.loads(result.stdout)
        assert payload["error"]["code"] == "SESSION_EXPIRED"
        login = origin_login_hint(origin if index == 1 else None)
        assert payload["error"].pop("hint") == f"{login} --addr {ADDR}"
        observed.append({"stdout": payload, "logs": normalized_events(home)})
    assert_release_equal(*observed)


@pytest.mark.parametrize("json_output", [True, False])
@pytest.mark.parametrize("known_code", [True, False])
@pytest.mark.parametrize("valid_hash", [True, False])
def test_public_intent_status_scrubs_keeper_payload(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
    known_code: bool,
    valid_hash: bool,
) -> None:
    token = "a1b2c3d4" * 8 if valid_hash else "synthetic-stored-session"
    private = f"Keeper message: {token} " + "x" * 500
    with replay() as (endpoint, replies, requests):
        expected = "market_order_expired" if known_code else "KEEPER_ERROR"
        for index, cli in enumerate(cli_pair):
            home = tmp_path / str(index)
            monkeypatch.setenv("SAI_HOME", str(home))
            with log.silenced():
                storage.save_session(
                    {
                        "evmAddress": ADDR,
                        "endpoint": endpoint,
                        "token": token,
                        "expiresAt": "2099-01-01T00:00:00Z",
                    }
                )
            row = RESPONSES["market_order_expired"]["data"]["userIntents"][0]
            row = {
                **row,
                "errorMsg": private,
                "errorCode": expected if known_code else token,
                "txHash": token,
                "serverDiagnostic": token,
            }
            requests.clear()
            replies.append(
                response(
                    {"data": {"userIntents": [row], "hint": private, "extra": token}}
                )
            )
            args = ["--endpoint", endpoint]
            if json_output:
                args.append("--json")
            result = invoke(cli, home, *args, "intent", "status", ADDR, "--key", KEY)
            assert result.returncode == 0
            assert not result.stderr
            assert [request.body for request in requests] == [
                wire(keeper.INTENTS, user=ADDR, key=KEY)
            ]
            output = result.stdout
            assert token.encode() not in output
            assert private.encode() not in output
            envelope = json.loads(output)
            assert set(envelope) == {"userIntents"}
            public = envelope["userIntents"][0]
            assert "serverDiagnostic" not in public
            if valid_hash:
                assert public["txHash"] == "[redacted session token]"
            else:
                assert "txHash" not in public
                assert public["txHashInvalid"] is True
            assert public["errorCode"] == expected
            assert public["errorMsg"] == "Keeper reported an intent error."


def test_public_history_projects_after_raw_hash_correlation(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opaque = "synthetic-keeper-text"
    c4_time = "2026-11-20 15:43:31.1234567890123456Z"
    intent = RESPONSES["outcome_not_indexed"]["data"]["userIntents"][0]
    history = {
        "intentKey": KEY,
        "id": opaque,
        "tradeChangeType": opaque,
        "txHash": opaque,
        "evmTxHash": "0X" + "CD" * 32,
        "trade": {"id": opaque, "trader": ADDR, "note": opaque},
        "block": {"block": "2142-2", "block_ts": c4_time},
        "debug": opaque,
    }
    compare(
        cli_pair,
        tmp_path,
        monkeypatch,
        (
            Step(
                (
                    {
                        "data": {
                            "userIntents": [
                                {**intent, "txHash": "cd" * 32, "execBlockTs": c4_time}
                            ],
                            "extra": opaque,
                        }
                    },
                    {"data": {"perp": {"tradeHistory": [history]}}},
                ),
                "terminal",
            ),
        ),
    )


@pytest.mark.parametrize(
    ("change", "projected"),
    [("synthetic-keeper-text", None), ("position_liquidated", "position_liquidated")],
)
def test_public_history_preserves_liquidation_label(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    projected: str | None,
) -> None:
    opaque = "synthetic-keeper-text"
    intent = RESPONSES["outcome_not_indexed"]["data"]["userIntents"][0]
    history = {
        "intentKey": KEY,
        "id": opaque,
        "tradeChangeType": change,
        "txHash": opaque,
        "evmTxHash": "0X" + "CD" * 32,
        "trade": {"id": opaque, "trader": ADDR, "note": opaque},
        "block": {"block": "2142-2", "block_ts": opaque},
        "debug": opaque,
    }

    with replay() as (endpoint, replies, _requests):
        for index, cli in enumerate(cli_pair):
            home = tmp_path / str(index)
            home.mkdir(mode=0o700)
            seed(home, endpoint, monkeypatch, "unknown")
            replies.clear()
            replies.extend(
                [
                    response(
                        {"data": {"userIntents": [{**intent, "txHash": "cd" * 32}]}}
                    ),
                    response({"data": {"perp": {"tradeHistory": [history]}}}),
                ]
            )
            result = invoke(
                cli,
                home,
                "--endpoint",
                endpoint,
                "--json",
                "intent",
                "status",
                ADDR,
                "--key",
                KEY,
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
            [row] = json.loads(result.stdout)["matchedTradeHistory"]
            assert row.get("tradeChangeType") == projected


@pytest.fixture(scope="module")
def keeper_certificate(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[ssl.SSLContext, Path]:
    certs = tmp_path_factory.mktemp("keeper-ca")

    def openssl(*args: str) -> str:
        return subprocess.run(
            ["openssl", *args],
            cwd=certs,
            check=True,
            capture_output=True,
            timeout=10,
            text=True,
        ).stdout

    key_args = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes")
    for name in ("keeper-ca", "other-ca"):
        openssl(
            "req",
            "-x509",
            *key_args,
            "-days",
            "2",
            "-subj",
            f"/CN={name}   Root/O=Fixture CA",
            "-keyout",
            f"{name}.key",
            "-out",
            f"{name}.pem",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
    (certs / "server.ext").write_text(
        "subjectAltName=IP:127.0.0.1\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth\n"
        "authorityKeyIdentifier=keyid,issuer\n",
        encoding="ascii",
    )
    for name, issuer in (("server", "keeper-ca"), ("proxy", "other-ca")):
        openssl(
            "req",
            "-new",
            *key_args,
            "-subj",
            "/CN=127.0.0.1",
            "-keyout",
            f"{name}.key",
            "-out",
            f"{name}.csr",
        )
        openssl(
            "x509",
            "-req",
            "-in",
            f"{name}.csr",
            "-CA",
            f"{issuer}.pem",
            "-CAkey",
            f"{issuer}.key",
            "-CAcreateserial",
            "-out",
            f"{name}.pem",
            "-days",
            "2",
            "-extfile",
            "server.ext",
        )
    proxy_roots = certs / "proxy-roots"
    proxy_roots.mkdir()
    shutil.copy(certs / "other-ca.pem", proxy_roots / "other-ca.pem")
    openssl("rehash", "proxy-roots")
    hashed = certs / "hashed"
    hashed.mkdir()
    shutil.copy(certs / "keeper-ca.pem", hashed / "keeper-ca.pem")
    openssl("rehash", "hashed")
    raw = certs / "unhashed"
    raw.mkdir()
    shutil.copy(certs / "keeper-ca.pem", raw / "keeper-ca.pem")
    hash_name = openssl(
        "x509", "-in", "keeper-ca.pem", "-noout", "-subject_hash"
    ).strip()
    for name, filename in (("wrong_hash", "00000000.0"), ("gap", f"{hash_name}.1")):
        invalid = certs / name
        invalid.mkdir()
        shutil.copy(certs / "keeper-ca.pem", invalid / filename)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs / "server.pem", certs / "server.key")
    return context, certs


@pytest.mark.skipif(os.name != "posix", reason="requires a process-group SIGINT")
def test_watch_reports_tls_certificate_failure(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    keeper_certificate: tuple[ssl.SSLContext, Path],
) -> None:
    context, certs = keeper_certificate
    with replay(context) as (endpoint, _replies, requests):
        ws = endpoint.replace("https://", "wss://", 1)
        for index, cli in enumerate(cli_pair):
            assert_watch_disconnect(
                cli,
                tmp_path / str(index),
                ws,
                "SSLCertVerificationError",
                overrides={"SSL_CERT_FILE": str(certs / "other-ca.pem")},
            )
        assert not requests


def assert_watch_disconnect(
    cli: CLI,
    home: Path,
    ws: str,
    error_type: str,
    *,
    overrides: dict[str, str] | None = None,
) -> None:
    with subprocess.Popen(
        [*cli, "--json", "--ws-endpoint", ws, "watch", "balances", ADDR],
        env={
            "PATH": os.defpath,
            "HOME": str(home.parent),
            "SAI_HOME": str(home),
            "SAI_LOG": "1",
            **(overrides or {}),
        },
        cwd=home.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as proc:
        assert proc.stderr is not None
        try:
            assert select.select([proc.stderr], [], [], 8)[0]
            warning = proc.stderr.readline()
            os.killpg(proc.pid, signal.SIGINT)
            stdout, _ = proc.communicate(timeout=5)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate(timeout=5)
    assert proc.returncode == 1
    assert json.loads(stdout)["error"]["message"] == "Aborted"
    assert warning == (
        f"Disconnected ({error_type}); data may be stale; reconnecting in 2s\n".encode()
    )
    assert [
        (event["level"], event["code"], event["errorType"], event["endpoint"])
        for event in read_events(home)
        if event["event"] == "watch.disconnected"
    ] == [("warn", "STREAM_ERROR", error_type, ws.removesuffix("/query"))]


@pytest.mark.parametrize(
    ("header", "values", "error_type"),
    [
        ("Connection", (), "InvalidUpgrade"),
        ("Connection", ("keep-alive",), "InvalidUpgrade"),
        ("Upgrade", (), "InvalidUpgrade"),
        ("Upgrade", ("websocket", "other/2"), "InvalidUpgrade"),
        ("Upgrade", ("websocket trailing",), "InvalidHeaderFormat"),
        ("Sec-WebSocket-Accept", (), "InvalidHeader"),
        ("Sec-WebSocket-Accept", ("first", "second"), "InvalidHeader"),
        ("Sec-WebSocket-Accept", ("wrong",), "InvalidHeaderValue"),
        (
            "Sec-WebSocket-Protocol",
            ("graphql-transport-ws", "graphql-transport-ws"),
            "InvalidHeader",
        ),
        (
            "Sec-WebSocket-Protocol",
            ("graphql-transport-ws, other",),
            "InvalidHeader",
        ),
    ],
)
def test_watch_reports_rejected_upgrade(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    header: str,
    values: tuple[str, ...],
    error_type: str,
) -> None:
    def replace_header(
        _connection: ServerConnection,
        _request: UpgradeRequest,
        response: UpgradeResponse,
    ) -> None:
        del response.headers[header]
        for value in values:
            response.headers[header] = value

    def wait_for_client_disconnect(connection: ServerConnection) -> None:
        # A server close frame can race with Python's failed handshake cleanup.
        connection.wait_closed()

    with graphql_ws(wait_for_client_disconnect, process_response=replace_header) as ws:
        for index, cli in enumerate(cli_pair):
            assert_watch_disconnect(cli, tmp_path / str(index), ws, error_type)


@pytest.mark.parametrize(
    "mode",
    [
        "file",
        "dir",
        "file_precedes_dir",
        "unhashed_dir",
        "wrong_hash",
        "gap",
        "default",
    ],
)
def test_keeper_tls_trust_matches_httpx(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    keeper_certificate: tuple[ssl.SSLContext, Path],
    mode: str,
) -> None:
    context, certs = keeper_certificate
    trust = {
        "file": {"SSL_CERT_FILE": str(certs / "keeper-ca.pem")},
        "dir": {"SSL_CERT_DIR": str(certs / "hashed")},
        "file_precedes_dir": {
            "SSL_CERT_FILE": str(certs / "other-ca.pem"),
            "SSL_CERT_DIR": str(certs / "hashed"),
        },
        "unhashed_dir": {"SSL_CERT_DIR": str(certs / "unhashed")},
        "wrong_hash": {"SSL_CERT_DIR": str(certs / "wrong_hash")},
        "gap": {"SSL_CERT_DIR": str(certs / "gap")},
        "default": {},
    }[mode]
    trusted = mode in {"file", "dir"}
    with replay(context) as (endpoint, replies, requests):
        observed = []
        for index, cli in enumerate(cli_pair):
            requests.clear()
            if trusted:
                replies.append(response("pending_exec"))
            result = invoke(
                cli,
                tmp_path / str(index),
                "--endpoint",
                endpoint,
                "--json",
                "intent",
                "status",
                ADDR,
                overrides=trust,
            )
            assert result.returncode == (0 if trusted else 4)
            assert not result.stderr
            assert not replies
            assert [request.body for request in requests] == (
                [wire(keeper.INTENTS, user=ADDR, key=None)] if trusted else []
            )
            if not trusted:
                assert json.loads(result.stdout)["error"]["code"] == "NOT_SENT"
            observed.append((result.stdout, list(requests)))
        assert observed[0] == observed[1]


@pytest.mark.parametrize(
    ("host", "exclusion", "bypass"),
    [
        ("127.0.0.1", "{host}:{port}", True),
        ("127.0.0.1", "{host}:{wrong_port}", False),
        ("localhost", "{host}:{port}", True),
        ("localhost", "{host}:{wrong_port}", False),
        ("localhost", "LOCALHOST", True),
        ("localhost", ".localhost", False),
        ("localhost", "https://{host}:{port}", False),
        ("localhost", "http://", True),
        ("localhost", "http://*:{port}", True),
        ("localhost", "http://*:{wrong_port}", False),
        ("127.0.0.1", "not-this-host, *", True),
        ("127.0.0.1", "", False),
    ],
)
def test_no_proxy_matches_keeper_destination(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    host: str,
    exclusion: str,
    bypass: bool,
) -> None:
    with (
        replay() as (keeper_url, keeper_replies, keeper_requests),
        replay() as (proxy, proxy_replies, proxy_requests),
    ):
        endpoint = keeper_url.replace("127.0.0.1", host)
        port = int(endpoint.rsplit(":", 1)[1].split("/", 1)[0])
        pattern = exclusion.format(host=host, port=port, wrong_port=port - 1)
        for index, cli in enumerate(cli_pair):
            recorded, unexpected = (
                (keeper_requests, proxy_requests)
                if bypass
                else (proxy_requests, keeper_requests)
            )
            recorded.clear()
            (keeper_replies if bypass else proxy_replies).append(response("absent"))
            result = invoke(
                cli,
                tmp_path / str(index),
                "--endpoint",
                endpoint,
                "--json",
                "intent",
                "status",
                ADDR,
                overrides={
                    "HTTP_PROXY": proxy.removesuffix("/query"),
                    "NO_PROXY": pattern,
                },
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {"userIntents": []}
            assert not unexpected
            assert not (keeper_replies or proxy_replies)
            assert [(request.path, request.body) for request in recorded] == [
                (
                    "/query" if bypass else endpoint,
                    wire(keeper.INTENTS, user=ADDR, key=None),
                )
            ]


@contextmanager
def tls_proxy(
    context: ssl.SSLContext, keeper_url: str
) -> Iterator[tuple[str, list[str]]]:
    authority = keeper_url.removeprefix("https://").split("/", 1)[0]
    host, port = authority.split(":", 1)
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_CONNECT(self) -> None:
            requests.append(self.path)
            if self.path != authority:
                self.send_error(400)
                return
            with socket.create_connection((host, int(port)), timeout=3) as upstream:
                self.send_response(200, "Connection established")
                self.end_headers()
                self.connection.settimeout(3)
                # The proxy terminates its own TLS, then forwards opaque bytes
                # for the separate TLS handshake and request with Keeper.
                with suppress(OSError):
                    while True:
                        pending = self.connection.pending()
                        readable = (
                            [self.connection]
                            if pending
                            else select.select([self.connection, upstream], [], [], 3)[
                                0
                            ]
                        )
                        if not readable:
                            return
                        for source in readable:
                            data = source.recv(16384)
                            if not data:
                                return
                            peer = (
                                upstream
                                if source is self.connection
                                else self.connection
                            )
                            peer.sendall(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(
            target=server.serve_forever, args=(0.01,), daemon=True
        )
        worker.start()
        try:
            yield f"https://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            worker.join()


@pytest.mark.parametrize(
    "mode",
    ["split_cas", "file_trusts_proxy", "dir_trusts_proxy", "keeper_ignores_proxy_dir"],
)
def test_https_proxy_and_keeper_use_httpx_trust_sources(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    keeper_certificate: tuple[ssl.SSLContext, Path],
    mode: str,
) -> None:
    keeper_context, certs = keeper_certificate
    proxy_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    same_ca = mode in {"file_trusts_proxy", "dir_trusts_proxy"}
    leaf = "server" if same_ca else "proxy"
    proxy_context.load_cert_chain(certs / f"{leaf}.pem", certs / f"{leaf}.key")
    trust = {
        "split_cas": {
            "SSL_CERT_FILE": str(certs / "keeper-ca.pem"),
            "SSL_CERT_DIR": str(certs / "proxy-roots"),
        },
        "file_trusts_proxy": {"SSL_CERT_FILE": str(certs / "keeper-ca.pem")},
        "dir_trusts_proxy": {"SSL_CERT_DIR": str(certs / "hashed")},
        "keeper_ignores_proxy_dir": {
            "SSL_CERT_FILE": str(certs / "other-ca.pem"),
            "SSL_CERT_DIR": str(certs / "hashed"),
        },
    }[mode]
    accepted = mode != "keeper_ignores_proxy_dir"
    with (
        replay(keeper_context) as (endpoint, replies, requests),
        tls_proxy(proxy_context, endpoint) as (proxy_url, connects),
    ):
        for index, cli in enumerate(cli_pair):
            connects.clear()
            requests.clear()
            if accepted:
                replies.append(response("absent"))
            result = invoke(
                cli,
                tmp_path / str(index),
                "--endpoint",
                endpoint,
                "--json",
                "intent",
                "status",
                ADDR,
                overrides={**trust, "HTTPS_PROXY": proxy_url},
            )
            assert result.returncode == (0 if accepted else 4), result.stdout
            assert not result.stderr
            assert connects == [endpoint.removeprefix("https://").split("/", 1)[0]]
            assert not replies
            assert [request.body for request in requests] == (
                [wire(keeper.INTENTS, user=ADDR, key=None)] if accepted else []
            )
            output = json.loads(result.stdout)
            if accepted:
                assert output == {"userIntents": []}
            else:
                assert output["error"]["code"] == "NOT_SENT"
