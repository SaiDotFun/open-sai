"""JSONL logs under $SAI_HOME/logs: format, concurrency, redaction,
retention, and the commands that read them.

Keeper is a real HTTP server on loopback, and the CLI and MCP server run
against a temp SAI_HOME.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent
from sai_cli import doctor
from sai_cli.core import keeper, log, log_queries, reconcile, settings, storage
from sai_cli.core import session as sessions
from sai_cli.core.log_events import SCHEMA_VERSION, VOLATILE_FIELDS, valid_event
from sai_cli.mcp_server import server
from sai_cli.sai_cli import command_path, main

ADDR = "0x" + "b" * 40
OTHER = "0x" + "c" * 40
COMMON = set(log.COMMON)
GOLDENS = Path(__file__).parent / "fixtures" / "logs"
# Secrets planted in URLs; none may reach a log line.
URL_PASSWORD = "hunter2-url-password"
URL_KEY = "url-api-key-0123456789"
PATH_KEY = "path-api-key-9876543210"
SIGNATURE = "0x" + "ab" * 65
OPAQUE_TOKEN = "opaque-session-token-0123456789abcdef"
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    for name in ("SAI_ENDPOINT", "SAI_WS_ENDPOINT", "SAI_LCD_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    log.reset()
    return tmp_path


def jwt(address: str = ADDR, hours: float = 1) -> str:
    """A token shaped like Keeper's: three base64url segments."""

    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    expiry = int((datetime.now(UTC) + timedelta(hours=hours)).timestamp())
    payload = {"sub": address, "exp": expiry, "purpose": "user_intent"}
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(payload)}.{'s' * 43}"


def read_logs(root: Path, surface: str | None = None) -> list[dict[str, Any]]:
    assert log.flush(60)
    lines: list[dict[str, Any]] = []
    for _day, found, path in log.log_files(root / "logs"):
        if surface in (None, found):
            lines += [json.loads(raw) for raw in path.read_text().splitlines()]
    return lines


def raw_logs(root: Path) -> str:
    assert log.flush(60)
    return "".join(
        path.read_text() for _day, _surface, path in log.log_files(root / "logs")
    )


def events(root: Path, name: str) -> list[dict[str, Any]]:
    return [line for line in read_logs(root) if line["event"] == name]


def run_cli(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(main, list(args), catch_exceptions=False)
    return result.exit_code, result.output


def sai_command() -> list[str]:
    script = Path(sys.executable).with_name("sai.exe" if os.name == "nt" else "sai")
    return [str(script)] if script.exists() else [sys.executable, "-m", "sai_cli"]


def assert_log_golden(
    name: str, lines: list[dict[str, Any]], replacements: dict[str, str]
) -> None:
    """Keep the shape, sequence, and ID relationships of captured lines."""
    runs: dict[str, str] = {}
    calls: dict[str, str] = {}
    commands: dict[str, str] = {}
    endpoints: dict[str, str] = {}
    pids: dict[int, int] = {}
    rows = []
    for index, line in enumerate(lines, 1):
        assert line["schema"] == SCHEMA_VERSION
        assert valid_event(line), line
        values = dict(line)
        for field in VOLATILE_FIELDS:
            if field not in line:
                continue
            value = line[field]
            match field:
                case "ts":
                    values[field] = f"2026-01-01T00:00:00.{index:03d}Z"
                case "pid":
                    values[field] = pids.setdefault(value, 1000 + len(pids))
                case "runId":
                    values[field] = runs.setdefault(value, f"{len(runs) + 1:032x}")
                case "callId":
                    values[field] = calls.setdefault(value, f"{len(calls) + 1:012x}")
                case "commandId":
                    values[field] = commands.setdefault(
                        value, f"{len(commands) + 1:032x}"
                    )
                case "endpointId":
                    assert log_queries.ENDPOINT_ID.fullmatch(value)
                    values[field] = endpoints.setdefault(
                        value, f"h1_{len(endpoints) + 1:064x}"
                    )
                case "durationMs":
                    values[field] = 0
                case "version":
                    values[field] = "0.0.0"
                case _:
                    raise AssertionError(f"No golden normalization for {field}")
        rows.append(json.dumps(values, separators=(",", ":"), sort_keys=True))
    captured = "\n".join(rows) + "\n"
    for actual, stable in replacements.items():
        captured = captured.replace(actual, stable)
    path = GOLDENS / f"{name}.jsonl"
    if os.environ.get("SAI_REGENERATE_LOG_GOLDENS") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(captured, encoding="utf-8", newline="\n")
    assert [json.loads(row) for row in captured.splitlines()] == [
        json.loads(row) for row in path.read_text(encoding="utf-8").splitlines()
    ]


class Keeper:
    """What the stub Keeper answers, and the requests it received."""

    def __init__(self) -> None:
        self.intents: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] | None = None
        self.status = 200
        self.ack_tx_hash: Any = None
        self.intents_block: threading.Event | None = None
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.authorizations: list[str | None] = []


@pytest.fixture
def keeper_stub() -> Iterator[tuple[str, Keeper]]:
    """A Keeper on loopback answering prices, markets, and intent records."""
    state = Keeper()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(body)
            state.paths.append(self.path)
            state.authorizations.append(self.headers.get("Authorization"))
            query = body["query"]
            if state.errors is not None:
                value: dict[str, Any] = {"errors": state.errors}
            elif "recordUserIntent" in query and state.ack_tx_hash is not None:
                envelope = body["variables"]["input"]
                value = {
                    "data": {
                        "recordUserIntent": {
                            "intentKey": envelope["intentKey"],
                            "kind": envelope["intent"]["kind"],
                            "evmAddress": ADDR,
                            "lifecycleStep": "jwt_pass",
                            "execActor": "server",
                            "txHash": state.ack_tx_hash,
                        }
                    }
                }
            elif "userIntents" in query:
                if state.intents_block is not None:
                    assert state.intents_block.wait(15)
                value = {"data": {"userIntents": state.intents}}
            elif "tokenPricesUsd" in query:
                value = {"data": {"oracle": {"tokenPricesUsd": []}}}
            elif "tradeHistory" in query:
                value = {"data": {"perp": {"tradeHistory": state.history}}}
            else:
                value = {"data": {"perp": {"borrowings": [], "trades": []}}}
            data = json.dumps(value).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/query", state
    finally:
        httpd.shutdown()
        httpd.server_close()


def save_session(endpoint: str, address: str = ADDR, token: str | None = None) -> str:
    token = token or jwt(address)
    storage.save_session(
        {
            "token": token,
            "evmAddress": address,
            "endpoint": endpoint,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
    )
    return token


def call(mcp: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call a tool through an in-memory MCP client and decode its JSON."""

    async def run() -> Any:
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            return await client.call_tool(name, arguments or {})

    block = asyncio.run(run()).content[0]
    assert isinstance(block, TextContent)
    return json.loads(block.text)


# Line format


def sample_request(
    operation: str, level: str = "info", *, duration: int = 12, status: str = "ok"
) -> None:
    log.event(
        "keeper.request",
        level,
        operation=operation,
        durationMs=duration,
        endpoint="https://example.test",
        authenticated=False,
        status=status,
    )


def test_event_writes_one_json_line_with_the_common_fields(home):
    sample_request("prices")
    assert log.flush(60)
    path = log.file_path("cli", datetime.now(UTC).date(), home / "logs")
    raw = path.read_text()
    assert raw.endswith("\n") and raw.count("\n") == 1
    line = json.loads(raw)
    assert set(line) == COMMON | {
        "operation",
        "durationMs",
        "endpoint",
        "authenticated",
        "status",
    }
    assert line["event"] == "keeper.request" and line["level"] == "info"
    assert line["schema"] == SCHEMA_VERSION
    assert line["surface"] == "cli" and line["pid"] == os.getpid()
    assert line["runId"] == log.RUN_ID and len(log.RUN_ID) == 32
    assert line["version"] == log.VERSION
    stamp = datetime.fromisoformat(line["ts"])
    assert line["ts"].endswith("Z") and stamp.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - stamp) < timedelta(minutes=1)


@posix_only
def test_directory_and_files_are_private(home):
    with log.bind(surface="mcp"):
        log.event("mcp.tool_call", tool="market_prices", callId="test", arguments={})
    assert log.flush(60)
    root = home / "logs"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    names = sorted(path.name for path in root.iterdir())
    assert names == [".pruned", f"mcp-{datetime.now(UTC).date().isoformat()}.jsonl"]
    for path in root.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_bound_fields_reach_every_event_and_pick_the_surface(home):
    with log.bind(surface="mcp", tool="trade_open", callId="abc"):
        sample_request("submit")
        with log.bind(surface="update"):
            log.event("update.check", ok=True)
    log.event("command.start", argv=[])
    assert [(line["surface"], line["event"]) for line in read_logs(home)] == [
        ("cli", "command.start"),
        ("mcp", "keeper.request"),
        ("update", "update.check"),
    ]
    request = events(home, "keeper.request")[0]
    assert (request["tool"], request["callId"]) == ("trade_open", "abc")
    assert "tool" not in events(home, "command.start")[0]


def test_each_event_is_copied_within_a_fixed_budget(home):
    """What `event` copies is bounded, whatever a caller or an MCP client
    hands it: long text, long lists, deep nesting, and the event overall."""
    deep: Any = "leaf"
    for _ in range(20):
        deep = [deep]
    log.event("big.text", words="word " * 2000, unbroken="x" * 5000)
    log.event(
        "big.list", items=list(range(100_000)), keys={str(i): i for i in range(500)}
    )
    log.event("big.depth", deep=deep)
    log.event("big.total", items=["y " * 900 for _ in range(60)])
    text, listed, nested, total = read_logs(home)
    assert len(text["words"]) < 2100 and text["words"].startswith("word word")
    assert f"{log.CUT} " in text["words"]
    assert text["unbroken"] == f"{log.CUT} 5000 characters"
    assert listed["items"][: log.MAX_ITEMS] == list(range(log.MAX_ITEMS))
    assert listed["items"][-1] == f"{log.CUT} {100_000 - log.MAX_ITEMS} more items"
    assert len(listed["keys"]) == log.MAX_ITEMS + 1
    assert listed["keys"][log.CUT] == 500 - log.MAX_ITEMS
    assert json.dumps(nested["deep"]).count("[") == log.MAX_DEPTH
    assert total["items"][-1].startswith(f"{log.CUT} ") and len(total["items"]) < 60
    kept = sum(len(item) for item in total["items"])
    assert kept <= log.EVENT_CHARS + log.MAX_TEXT
    assert all(len(raw) <= log.MAX_LINE for raw in raw_logs(home).splitlines())


def test_a_copy_of_a_huge_value_takes_bounded_time():
    huge = ["z" * 1_000_000 for _ in range(2000)]
    start = time.monotonic()
    for _ in range(200):
        log._snapshot({"addr": huge}, [log.EVENT_CHARS, log.EVENT_NODES], ())
    assert time.monotonic() - start < 5


def test_an_oversized_line_keeps_the_common_fields():
    line = {key: "v" for key in log.COMMON} | {"a": "x" * log.MAX_LINE, "b": 1}
    kept = json.loads(log.encode(line))
    assert set(kept) == COMMON | {"truncated"} and kept["truncated"] == ["a", "b"]


WRITER = """
import sys
from sai_cli.core import log
worker, count = int(sys.argv[1]), int(sys.argv[2])
for index in range(count):
    log.event("stress", worker=worker, index=index, pad="p" * (index * 37 % 1900))
assert log.flush(60)
"""


def test_concurrent_processes_write_whole_lines(home):
    workers, count = 12, 150
    env = {**os.environ, "SAI_HOME": str(home)}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER, str(worker), str(count)], env=env
        )
        for worker in range(workers)
    ]
    assert [process.wait(timeout=120) for process in processes] == [0] * workers
    raw = raw_logs(home)
    lines = [json.loads(line) for line in raw.splitlines()]
    assert len(lines) == workers * count
    seen = {(line["worker"], line["index"]) for line in lines}
    assert seen == {(w, i) for w in range(workers) for i in range(count)}
    assert all(len(line["pad"]) == line["index"] * 37 % 1900 for line in lines)
    assert len({line["pid"] for line in lines}) == workers
    assert len({line["runId"] for line in lines}) == workers


# Redaction


def test_strings_lose_stored_tokens_and_anything_shaped_like_a_jwt(home):
    stored = save_session(keeper.MAINNET_HTTP)
    save_session(keeper.MAINNET_HTTP, OTHER, OPAQUE_TOKEN)
    unsaved = jwt(OTHER)
    log.event(
        "probe",
        error=f"failed with {stored} and {OPAQUE_TOKEN}",
        nested={"values": [unsaved, {OPAQUE_TOKEN: stored}]},
    )
    raw = raw_logs(home)
    assert raw.count(log.REDACTED) >= 5
    for secret in (stored, OPAQUE_TOKEN, unsaved):
        assert secret not in raw


@pytest.mark.parametrize(
    "tail",
    [
        f"/v1/it's/{PATH_KEY}/query",
        f"/query?name=o'brien&api_key={URL_KEY}",
        f"/a(b)/{PATH_KEY}",
        f"/a;params/{PATH_KEY};v=1",
        f"/a,b,{PATH_KEY}",
        f"/%27%20%22/{PATH_KEY}?q=%3C{URL_KEY}%3E",
        f'/"quoted"/{PATH_KEY}',
        f"/<angle>/{PATH_KEY}",
        f"/back\\slash/{PATH_KEY}",
        f"#frag'{URL_KEY}",
    ],
)
def test_a_url_is_scrubbed_through_to_its_end(tail):
    """Quotes, brackets, commas, semicolons, and percent escapes are all
    legal inside a URL, so none of them ends it."""
    url = f"https://user:{URL_PASSWORD}@keeper.example:8443{tail}"
    site = "https://keeper.example:8443"
    secrets = (PATH_KEY, URL_KEY, URL_PASSWORD)
    for text in (
        log.scrub(log.Endpoint(url), ()),
        log.scrub(log.Arg(url), ()),
        log.scrub(log.Arg(f"--endpoint={url}"), ()),
        log.scrub(f"failed for {url} then retry", ()),
        log.scrub(log.Untrusted(f"error '{url}' (see {url}); {url}, {PATH_KEY}"), ()),
        log.scrub(f'{{"url":"{url}","next":1}}', ()),
    ):
        assert not any(secret in text for secret in secrets), text
        assert site in text or log.REDACTED_URL in text
    assert log.scrub(log.Arg(url), ()) == site
    assert log.scrub(log.Arg(f"--endpoint={url}"), ()) == f"--endpoint={site}"
    assert (
        log.scrub(f"failed for {url} then retry", ()) == f"failed for {site} then retry"
    )
    # One whole value: a space inside it does not end the URL either.
    assert log.scrub(log.Arg(f"{url} and {PATH_KEY}"), ()) == site


def test_text_that_ran_into_a_url_is_not_taken_for_its_host():
    text = log.scrub(f'{{"url":"https://keeper.example","token":"{URL_KEY}"}}', ())
    assert URL_KEY not in text and log.REDACTED_URL in text
    assert log.origin(f"https://host,{URL_KEY}/x") == log.REDACTED_URL
    assert log.origin(f"https://ho'st{URL_KEY}") == log.REDACTED_URL


def test_urls_with_awkward_characters_do_not_reach_the_log(home, keeper_stub):
    base, _state = keeper_stub
    site = base.removesuffix("/query")
    tricky = f"{site}/it's/{PATH_KEY}/query?a=(b);c,d&api_key=%27{URL_KEY}"
    assert run_cli("--endpoint", tricky, "--json", "market", "prices")[0] == 0
    assert (
        run_cli(f"--endpoint={tricky}", "auth", "use", f"{tricky} {PATH_KEY}")[0] == 1
    )
    mcp = server.create_server(settings.load({"keeper.endpoint": tricky}))
    assert "perp" in call(mcp, "account_positions", {"addr": f"{tricky} {PATH_KEY}"})
    assert "oracle" in call(mcp, "market_prices")
    raw = raw_logs(home)
    for secret in (PATH_KEY, URL_KEY, "it's", "api_key"):
        assert secret not in raw, secret
    start = events(home, "command.start")[0]
    assert start["argv"] == ["--endpoint", site, "--json", "market", "prices"]
    assert events(home, "mcp.tool_call")[0]["arguments"] == {"addr": site}


def test_urls_are_reduced_to_their_origin():
    url = (
        f"https://user:{URL_PASSWORD}@keeper.example:8443/{PATH_KEY}/q?key={URL_KEY}#f"
    )
    assert log.origin(url) == "https://keeper.example:8443"
    assert log.origin("wss://[::1]:9/query") == "wss://[::1]:9"
    for bad in ("http://[::1", "https://host:port/x", "not a url", "", "//host/x"):
        assert log.origin(bad) == log.REDACTED_URL
    assert log.scrub(log.Endpoint(url)) == "https://keeper.example:8443"
    assert log.scrub(log.Endpoint(f"no scheme {URL_KEY}")) == log.REDACTED_URL
    assert (
        log.scrub(f"--endpoint={url}", ()) == "--endpoint=https://keeper.example:8443"
    )
    assert log.scrub(f"failed for {url} then retry", ()) == (
        "failed for https://keeper.example:8443 then retry"
    )
    assert log.scrub(ADDR, ()) == ADDR and log.scrub("--json", ()) == "--json"


def test_signatures_go_everywhere_and_shorter_hex_only_from_untrusted_text():
    digest = "0x" + "c" * 64
    assert log.scrub(f"sig {SIGNATURE} tx {digest}", ()) == (
        f"sig {log.REDACTED_HEX} tx {digest}"
    )
    assert log.scrub(log.Untrusted(f"sig {SIGNATURE} tx {digest}"), ()) == (
        f"sig {log.REDACTED_HEX} tx {log.REDACTED_HEX}"
    )
    assert log.scrub(log.Untrusted(ADDR), ()) == ADDR
    cut = log.scrub(log.Untrusted("m" * 1000), ())
    assert cut.startswith("m" * 300 + "... [700 more")


def test_no_token_or_url_credential_reaches_a_log_line(home, keeper_stub):
    """Run CLI commands and MCP tools with a signed-in session, a Keeper URL
    that carries API keys in its path and query, and credentials pasted
    where they do not belong; then scan every log line."""
    base, state = keeper_stub
    site = base.removesuffix("/query")
    endpoint = f"{site}/{PATH_KEY}/query?api_key={URL_KEY}"
    with_password = endpoint.replace("http://", f"http://user:{URL_PASSWORD}@")
    token = save_session(endpoint)
    save_session(endpoint, OTHER, OPAQUE_TOKEN)
    storage.use_session(ADDR)
    storage.pending("KEY1", endpoint, ADDR, "OPEN_TRADE", "")

    assert run_cli("--endpoint", endpoint, "market", "prices")[0] == 0
    assert run_cli(f"--endpoint={endpoint}", "auth", "status")[0] == 0
    assert run_cli("--endpoint", endpoint, "auth", "list")[0] == 0
    status = run_cli("--endpoint", endpoint, "intent", "status", ADDR, "--key", "KEY1")
    assert status[0] == 0
    # Mistakes: credentials in the URL, and secrets as arguments.
    assert run_cli("--endpoint", with_password, "market", "list")[0] == 1
    assert run_cli("--endpoint", endpoint, "account", "positions", token)[0] == 0
    assert run_cli("--endpoint", endpoint, "account", "history", SIGNATURE)[0] == 0
    assert run_cli("--endpoint", endpoint, "auth", "use", OPAQUE_TOKEN)[0] == 1
    assert run_cli("--endpoint", endpoint, "auth", "use", with_password)[0] == 1
    assert run_cli("--endpoint", endpoint, "doctor", "--offline")[0] in (0, 1)

    mcp = server.create_server(settings.load({"keeper.endpoint": endpoint}))
    assert call(mcp, "mcp_info")["trading"]["available"] is True
    assert call(mcp, "auth_status")["addr"] == ADDR
    assert "accounts" in call(mcp, "auth_list")
    assert "oracle" in call(mcp, "market_prices")
    assert "userIntents" in call(mcp, "intent_status", {"key": token})
    assert "code" in call(mcp, "auth_use", {"addr": OPAQUE_TOKEN})
    # A known parameter holding a credential URL, then unknown parameters.
    assert "perp" in call(mcp, "account_positions", {"addr": with_password})
    stray = {"endpoint": with_password, "token": OPAQUE_TOKEN, "sig": SIGNATURE}
    with pytest.raises(ToolError, match="market_prices"):
        asyncio.run(mcp.call_tool("market_prices", stray))
    assert "code" in call(mcp, "trade_close", {"trade_id": 1, "preview_digest": token})
    with pytest.raises(ToolError, match="preview_digest"):
        asyncio.run(mcp.call_tool("trade_close", {"trade_id": with_password}))
    with pytest.raises(ToolError):
        asyncio.run(mcp.call_tool(f"no_such_tool_{URL_KEY}", {"a": with_password}))
    assert call(mcp, "auth_use", {"addr": OTHER})["accounts"]
    storage.set_status("KEY1", endpoint, ADDR, "accepted")
    storage.delete_session(OTHER)

    assert all(PATH_KEY in path and URL_KEY in path for path in state.paths)
    raw = raw_logs(home)
    lines = read_logs(home)
    assert {line["surface"] for line in lines} == {"cli", "mcp"}
    seen = {line["event"] for line in lines}
    assert seen >= {
        "command.start",
        "command.end",
        "mcp.tool_call",
        "mcp.tool_result",
        "keeper.request",
        "intent.reserved",
        "intent.status",
        "intent.keeper",
        "session.saved",
        "session.active",
        "session.removed",
        "doctor.result",
    }
    secrets = (token, OPAQUE_TOKEN, URL_KEY, PATH_KEY, URL_PASSWORD, SIGNATURE)
    for secret in (*secrets, "api_key", "user:", SIGNATURE[2:]):
        assert secret not in raw, secret
    assert log.REDACTED in raw and log.REDACTED_HEX in raw
    endpoints = {line["endpoint"] for line in lines if "endpoint" in line}
    assert endpoints == {site}
    stray_call = next(line for line in lines if line.get("unknownArguments") == 3)
    assert stray_call["tool"] == "market_prices" and stray_call["arguments"] == {}
    unknown_tool = [line for line in lines if line.get("tool") == "unknown"]
    assert [line["event"] for line in unknown_tool] == [
        "mcp.tool_call",
        "mcp.tool_result",
    ]
    assert (
        unknown_tool[0]["arguments"] == {} and unknown_tool[0]["unknownArguments"] == 1
    )


# What each event records


def test_command_start_and_end_record_argv_exit_code_and_duration(home, keeper_stub):
    base, _state = keeper_stub
    assert run_cli("--endpoint", base, "--json", "market", "prices")[0] == 0
    start, request, end = read_logs(home)
    assert start["event"] == "command.start" and start["command"] == "market prices"
    site = base.removesuffix("/query")
    assert start["argv"] == ["--endpoint", site, "--json", "market", "prices"]
    assert request["event"] == "keeper.request"
    assert request["command"] == "market prices"
    assert request["operation"] == "prices" and request["status"] == "ok"
    assert request["httpStatus"] == 200 and request["endpoint"] == site
    assert request["authenticated"] is False and request["durationMs"] >= 0
    assert end["event"] == "command.end" and end["exitCode"] == 0
    assert end["level"] == "info" and end["durationMs"] >= request["durationMs"]
    assert start["runId"] == end["runId"]


def test_a_failed_command_logs_its_exit_code_and_error(home):
    code, _output = run_cli("--endpoint", "http://127.0.0.1:1/query", "market", "list")
    assert code == 4
    request = events(home, "keeper.request")[0]
    assert request["status"] == "not_sent" and request["level"] == "warn"
    assert request["code"] == "NOT_SENT"
    assert "httpStatus" not in request
    end = events(home, "command.end")[0]
    assert end["exitCode"] == 4 and end["level"] == "warn"
    assert end["code"] == request["code"]
    assert end["error"] == "Could not connect to Keeper; the request was not sent"
    # A usage error exits 2 before any command runs.
    assert run_cli("market", "nope")[0] == 2
    usage = events(home, "command.end")[1]
    assert usage["exitCode"] == 2 and usage["command"] == "market"
    assert usage["code"] == "INVALID_REQUEST"
    assert "No such command" in usage["error"]


def test_admission_rejection_keeps_its_code_and_key_in_cli_and_logs(home, keeper_stub):
    base, state = keeper_stub
    save_session(base)
    state.errors = [{"extensions": {"code": "INTENT_NOT_ACCEPTED"}}]
    result = CliRunner().invoke(
        main, ["--json", "--endpoint", base, "trade", "close", "7"]
    )
    assert result.exit_code == 4 and result.stderr == ""
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "ADMISSION_REJECTED" and error["retryable"] is True
    key = error["intentKey"]
    assert storage.list_pending(base, ADDR)[0]["status"] == "rejected"
    assert events(home, "intent.status")[0]["intentKey"] == key
    for event in ("keeper.request", "intent.status", "command.end"):
        assert events(home, event)[0]["code"] == error["code"]


def test_command_path_skips_options_and_their_values():
    assert command_path(main, ["--endpoint", "market", "market", "prices"]) == [
        "market",
        "prices",
    ]
    assert command_path(main, ["--json", "--endpoint=x", "auth", "use", "0xabc"]) == [
        "auth",
        "use",
    ]
    assert command_path(main, ["mcp", "serve", "--endpoint", "x"]) == ["mcp", "serve"]
    assert command_path(main, []) == []
    assert command_path(main, ["nope", "market"]) == []


def test_help_version_and_skill_write_nothing(home):
    for args in (["--help"], ["--version"], ["--skill"], ["market", "--help"]):
        assert run_cli(*args)[0] == 0
    assert not (home / "logs").exists()


def test_keeper_errors_record_what_keeper_answered(home, keeper_stub):
    base, state = keeper_stub
    state.errors = [
        {"message": "market is paused", "extensions": {"code": "MARKET_PAUSED"}}
    ]
    with pytest.raises(keeper.KeeperError):
        keeper.request(base, keeper.POSITIONS, {"trader": ADDR}, token=jwt())
    state.errors, state.status = None, 503
    with pytest.raises(keeper.KeeperError):
        keeper.request(base, "query { custom }")
    rejected, unavailable = events(home, "keeper.request")
    assert rejected["operation"] == "positions" and rejected["status"] == "error"
    assert rejected["addr"] == ADDR and rejected["authenticated"] is True
    assert rejected["keeperErrors"] == [
        {"code": "MARKET_PAUSED", "message": "market is paused"}
    ]
    assert rejected["error"] == "Keeper rejected the request; check inputs and session"
    assert unavailable["operation"] == "custom" and unavailable["httpStatus"] == 503


def test_a_keeper_error_that_echoes_the_signature_is_scrubbed(home, keeper_stub):
    """Keeper's message for a bad session mutation can quote its input."""
    base, state = keeper_stub
    token = jwt(OTHER)
    echo = f"invalid signature {SIGNATURE} for challenge {'c' * 64}, token {token}"
    state.errors = [
        {"message": echo, "extensions": {"code": "BAD_SIGNATURE"}},
        {"message": "x" * 5000, "extensions": {"code": SIGNATURE}},
    ]
    variables = {"signature": SIGNATURE, "challenge": "c" * 64}
    with pytest.raises(keeper.KeeperError):
        keeper.request(base, keeper.SESSION, variables)
    request = events(home, "keeper.request")[0]
    assert request["operation"] == "session"
    first, second = request["keeperErrors"]
    assert first == {
        "code": "BAD_SIGNATURE",
        "message": f"invalid signature {log.REDACTED_HEX} for challenge"
        f" {log.REDACTED_HEX}, token {log.REDACTED}",
    }
    assert second["code"] == log.REDACTED_HEX and len(second["message"]) < 350
    raw = raw_logs(home)
    for secret in (SIGNATURE, SIGNATURE[2:], "c" * 64, token):
        assert secret not in raw


def test_a_url_that_does_not_parse_neither_fails_nor_hides_the_error(home):
    """Logging the endpoint must not replace the request's own error, and a
    malformed URL as an ordinary argument must not fail the command."""
    with pytest.raises(keeper.KeeperError, match="Invalid IPv6 URL"):
        keeper.request("http://[::1/query", keeper.PRICES)
    with pytest.raises(httpx.InvalidURL):
        keeper.request("https://host:port/query", keeper.PRICES)
    code, output = run_cli("--json", "intent", "local", "http://[::1")
    assert (code, output) == (0, "[]\n")
    requests = events(home, "keeper.request")
    assert [line["endpoint"] for line in requests] == [log.REDACTED_URL] * 2
    start = events(home, "command.start")[0]
    assert start["argv"] == ["--json", "intent", "local", log.REDACTED_URL]


def test_intent_lifecycle_is_logged_from_reservation_to_terminal(home, keeper_stub):
    base, state = keeper_stub
    storage.pending("KEY1", base, ADDR, "CLOSE_POSITION", "7")
    record = {
        "intentKey": "KEY1",
        "kind": "CLOSE_POSITION",
        "lifecycleStep": "exec_req_pass",
        "execActor": "server",
        "evmAddress": ADDR,
        "txHash": None,
        "errorCode": None,
        "errorMsg": None,
    }
    state.intents = [record]
    reconcile.reconcile(base, ADDR, "KEY1")
    reconcile.reconcile(base, ADDR, "KEY1")  # No change: no second status line.
    state.intents = [
        {
            **record,
            "lifecycleStep": "exec_fail",
            "errorCode": "SLIPPAGE",
            "errorMsg": "price moved",
        }
    ]
    reconcile.reconcile(base, ADDR, "KEY1")
    trail = [line for line in read_logs(home) if line["event"].startswith("intent.")]
    assert [
        (line["event"], line.get("status") or line.get("lifecycleStep"))
        for line in trail
    ] == [
        ("intent.reserved", "unknown"),
        ("intent.keeper", "exec_req_pass"),
        ("intent.status", "accepted"),
        ("intent.keeper", "exec_req_pass"),
        ("intent.keeper", "exec_fail"),
        ("intent.status", "terminal"),
    ]
    assert all(line["intentKey"] == "KEY1" for line in trail)
    site = base.removesuffix("/query")
    assert all(line["addr"] == ADDR and line["endpoint"] == site for line in trail)
    reserved, _, accepted, _, failed, terminal = trail
    assert reserved["kind"] == "CLOSE_POSITION" and reserved["tradeId"] == "7"
    assert (accepted["previous"], accepted["kind"]) == ("unknown", "CLOSE_POSITION")
    assert failed["errorCode"] == "SLIPPAGE" and "errorMsg" not in failed
    assert failed["code"] == "KEEPER_ERROR"
    assert terminal["previous"] == "accepted"


def test_an_intent_keeper_never_recorded_is_logged_absent(home, keeper_stub):
    base, _state = keeper_stub
    storage.pending("KEY2", base, ADDR, "OPEN_TRADE", "")
    with storage.db() as connection:
        connection.execute("UPDATE intents SET created_at = 0 WHERE id = 'KEY2'")
    assert reconcile.reconcile(base, ADDR, "KEY2")["localStatus"] == "absent"
    missing = events(home, "intent.keeper")[0]
    assert missing["found"] is False and "lifecycleStep" not in missing
    status = events(home, "intent.status")[0]
    assert (status["previous"], status["status"]) == ("unknown", "absent")


def test_session_events_name_the_addr_and_never_the_token(home):
    token = save_session(keeper.MAINNET_HTTP)
    save_session(keeper.MAINNET_HTTP, OTHER)
    storage.use_session(ADDR)
    storage.delete_session(OTHER)
    expired = {**storage.load_session(), "expiresAt": "2020-01-01T00:00:00+00:00"}
    with pytest.raises(ValueError, match="expired"):
        sessions.check_active(expired, keeper.MAINNET_HTTP)
    lines = [line for line in read_logs(home) if line["event"].startswith("session.")]
    assert [(line["event"], line["addr"]) for line in lines] == [
        ("session.saved", ADDR),
        ("session.saved", OTHER),
        ("session.active", ADDR),
        ("session.removed", OTHER),
        ("session.expired", ADDR),
    ]
    assert lines[1]["connectedAccounts"] == 2 and lines[3]["connectedAccounts"] == 1
    assert lines[0]["endpoint"] == "https://sai-keeper.nibiru.fi"
    assert lines[4]["level"] == "warn"
    assert token not in raw_logs(home)


def test_mcp_tool_calls_log_arguments_result_code_and_account(home, keeper_stub):
    base, _state = keeper_stub
    save_session(base)
    mcp = server.create_server(settings.load({"keeper.endpoint": base}))
    assert "oracle" in call(mcp, "market_prices")
    assert call(mcp, "account_positions", {"addr": OTHER})["addr"] == OTHER
    refused = call(mcp, "trade_close", {"trade_id": 3, "addr": OTHER})
    assert refused["code"] == "SESSION_UNAVAILABLE"
    lines = read_logs(home, "mcp")
    names = [line["event"] for line in lines]
    assert names == [
        "mcp.tool_call",
        "keeper.request",
        "mcp.tool_result",
        "mcp.tool_call",
        "keeper.request",
        "mcp.tool_result",
        "mcp.tool_call",
        "mcp.tool_result",
    ]
    prices_call, prices_request, prices_result = lines[:3]
    assert prices_call["tool"] == "market_prices" and prices_call["arguments"] == {}
    assert len({line["callId"] for line in lines[:3]}) == 1
    assert prices_request["tool"] == "market_prices"
    assert prices_result["ok"] is True and prices_result["durationMs"] >= 0
    assert lines[3]["arguments"] == {"addr": OTHER}
    assert lines[5]["account"] == OTHER and lines[4]["addr"] == OTHER
    assert lines[3]["callId"] != prices_call["callId"]
    result = lines[7]
    assert result["tool"] == "trade_close" and result["level"] == "warn"
    assert result["ok"] is False and result["code"] == "SESSION_UNAVAILABLE"
    assert "is not connected" in result["error"]


def test_arguments_fastmcp_rejects_are_logged_without_the_input(home):
    save_session(keeper.MAINNET_HTTP)
    mcp = server.create_server(settings.load({}))
    with pytest.raises(ToolError, match="seven-secret"):
        asyncio.run(mcp.call_tool("trade_close", {"trade_id": "seven-secret"}))
    started, result = read_logs(home, "mcp")
    assert started["arguments"] == {"trade_id": "seven-secret"}
    assert result["event"] == "mcp.tool_result" and result["level"] == "error"
    assert result["ok"] is False and result["code"] == "INVALID_REQUEST"
    assert result["errorType"] == "ToolError" and result["callId"] == started["callId"]
    assert result["error"].startswith("Error executing tool trade_close")
    assert "seven-secret" not in result["error"] and "\n" not in result["error"]


def test_only_a_tools_own_parameters_are_logged(home, keeper_stub):
    base, _state = keeper_stub
    mcp = server.create_server(settings.load({"keeper.endpoint": base}))
    arguments = {"addr": ADDR, "note": "private", "token": OPAQUE_TOKEN}
    # The server rejects unknown arguments; the log must not keep them.
    with pytest.raises(ToolError, match="account_positions"):
        asyncio.run(mcp.call_tool("account_positions", arguments))
    started, result = read_logs(home, "mcp")
    assert started["arguments"] == {"addr": ADDR}
    assert started["unknownArguments"] == 2
    assert result["code"] == "INVALID_REQUEST" and "unknownArguments" not in result
    assert "private" not in raw_logs(home) and OPAQUE_TOKEN not in raw_logs(home)
    assert call(mcp, "account_positions", {"addr": ADDR})["addr"] == ADDR
    assert "unknownArguments" not in events(home, "mcp.tool_call")[1]


def test_mcp_info_reports_the_log_directory(home, monkeypatch):
    mcp = server.create_server(settings.load({}))
    assert call(mcp, "mcp_info")["logs"] == {"dir": str(home / "logs"), "enabled": True}
    monkeypatch.setenv("SAI_LOG", "0")
    assert call(mcp, "mcp_info")["logs"]["enabled"] is False


def test_update_check_logs_to_the_update_surface(home):
    code, _output = run_cli(
        "update",
        "--check",
        "--api-base",
        "http://127.0.0.1:1",
        "--web-base",
        "http://127.0.0.1:1",
    )
    assert code == 1
    lines = read_logs(home, "update")
    assert [line["event"] for line in lines] == [
        "command.start",
        "update.check",
        "command.end",
    ]
    assert lines[1]["ok"] is False and lines[2]["exitCode"] == 1
    assert read_logs(home, "cli") == []


def test_the_background_update_logs_to_the_update_surface_and_prints_nothing(home):
    """The detached worker has no terminal. From a checkout it changes
    nothing, and says so only in the log."""
    result = CliRunner().invoke(main, ["update", "--background"])
    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")
    lines = read_logs(home, "update")
    assert [line["event"] for line in lines] == [
        "command.start",
        "update.result",
        "command.end",
    ]
    assert lines[1]["background"] is True and lines[1]["status"] == "unsupported"
    assert lines[1]["command"] == "update" and lines[2]["exitCode"] == 0
    assert read_logs(home, "cli") == []


def test_logs_commands_never_start_an_update():
    from sai_cli.sai_cli import NO_AUTO_UPDATE, auto_update_applies

    assert "logs" in NO_AUTO_UPDATE
    assert auto_update_applies("logs", False, {}, True) is False
    assert auto_update_applies("market", False, {}, True) is True


def test_doctor_logs_its_outcome_and_no_fix_logs_nothing(home, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert run_cli("doctor", "--offline", "--no-fix")[0] == 0
    assert list(home.iterdir()) == []
    assert run_cli("doctor", "--offline")[0] == 0
    result = events(home, "doctor.result")[0]
    assert result["overall"] == "warn" and result["offline"] is True
    by_id = {check["id"]: check for check in result["checks"]}
    assert list(by_id) == [
        "config",
        "install",
        "version",
        "home",
        "logs",
        "session",
        "intents",
        "keeper",
        "websocket",
        "lcd",
    ]
    assert by_id["config"]["status"] == "pass"
    assert by_id["home"]["status"] == "fixed" and by_id["home"]["changes"]
    assert set(by_id["logs"]) == {"id", "status", "durationMs"}
    assert by_id["logs"]["status"] == "pass"
    assert "Signed out" in by_id["session"]["summary"]


def test_doctor_logs_the_invalid_request_code_for_a_failed_config_check(
    home, monkeypatch
):
    monkeypatch.setenv("PATH", "")
    code, _ = run_cli("--endpoint", "invalid-value", "doctor", "--offline")
    assert code == 1
    result = events(home, "doctor.result")[0]
    assert result["code"] == "INVALID_REQUEST"
    assert any(
        check["id"] == "config" and check["status"] == "fail"
        for check in result["checks"]
    )


# Retention


def test_expired_files_keeps_today_and_the_days_before_it():
    today = date(2026, 11, 18)
    files = [
        (today - timedelta(days=age), "cli", Path(f"cli-{age}.jsonl"))
        for age in (0, 1, 13, 14, 40)
    ]
    assert [p.name for p in log.expired_files(files, today, 14)] == [
        "cli-14.jsonl",
        "cli-40.jsonl",
    ]
    assert [p.name for p in log.expired_files(files, today, 1)] == [
        "cli-1.jsonl",
        "cli-13.jsonl",
        "cli-14.jsonl",
        "cli-40.jsonl",
    ]


def old_file(root: Path, surface: str, age: int) -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = log.file_path(surface, datetime.now(UTC).date() - timedelta(days=age), root)
    path.write_text('{"event":"old"}\n')
    return path


def test_first_event_deletes_old_files_once_a_day(home):
    root = home / "logs"
    kept = [old_file(root, "cli", 13), old_file(root, "mcp", 1)]
    gone = [old_file(root, "cli", 14), old_file(root, "update", 90)]
    log.event("first")
    assert log.flush(60)
    assert all(path.exists() for path in kept)
    assert not any(path.exists() for path in gone)
    # The marker says a prune ran today, so a new process skips it.
    late = old_file(root, "cli", 30)
    log.reset()
    log.event("second")
    assert log.flush(60) and late.exists()
    yesterday = datetime.now(UTC).timestamp() - 24 * 60 * 60
    os.utime(root / log.PRUNE_MARKER, (yesterday, yesterday))
    log.reset()
    log.event("third")
    assert log.flush(60) and not late.exists()


def test_retention_only_deletes_sais_own_log_files(home):
    root = home / "logs"
    old = datetime.now(UTC).date() - timedelta(days=400)
    root.mkdir(mode=0o700)
    others = [
        root / f"audit-{old.isoformat()}.jsonl",
        root / f"cli-{old.isoformat()}.jsonl.bak",
        root / f"xcli-{old.isoformat()}.jsonl",
        root / f"cli-{old.isoformat()}.json",
        root / "cli-2020-13-45.jsonl",
        root / "notes.txt",
    ]
    for path in others:
        path.write_text("keep me")
    folder = root / f"mcp-{old.isoformat()}.jsonl"
    folder.mkdir()
    expired = old_file(root, "update", 400)
    log.event("first")
    assert log.flush(60)
    assert not expired.exists()
    assert all(path.read_text() == "keep me" for path in others)
    assert folder.is_dir()
    assert log.parse_name("cli-2026-11-18.jsonl") == (date(2026, 11, 18), "cli")
    assert log.parse_name("audit-2026-11-18.jsonl") is None
    names = [path.name for _day, _surface, path in log.log_files(root)]
    assert names == [f"cli-{datetime.now(UTC).date().isoformat()}.jsonl"]


def test_a_running_process_prunes_again_when_the_day_changes(home, monkeypatch):
    """A long-running MCP server checks for old files on the first event of
    each UTC day, without restarting."""
    root = home / "logs"
    log.event("day.one")
    assert log.flush(60)
    today = datetime.now(UTC)
    aging = old_file(root, "mcp", 13)
    log.event("still.day.one")
    assert log.flush(60) and aging.exists()
    monkeypatch.setattr(log, "now", lambda: today + timedelta(days=1))
    log.event("day.two")
    assert log.flush(60)
    assert not aging.exists()
    tomorrow = (today + timedelta(days=1)).date()
    assert log.file_path("cli", tomorrow, root).exists()
    assert log.file_path("cli", today.date(), root).exists()


def test_keep_days_comes_from_the_environment_or_the_config_file(home, monkeypatch):
    root = home / "logs"
    week_old = old_file(root, "cli", 7)
    monkeypatch.setenv("SAI_LOG_KEEP_DAYS", "30")
    log.event("first")
    assert log.flush(60) and week_old.exists()
    monkeypatch.delenv("SAI_LOG_KEEP_DAYS")
    assert run_cli("config", "set", "logs.keep_days", "3")[0] == 0
    assert log.config() == (True, 3)
    assert log.flush(60)
    (root / log.PRUNE_MARKER).unlink()
    log.reset()
    log.event("second")
    assert log.flush(60) and not week_old.exists()


# Settings


def test_logs_settings_resolve_and_validate(home):
    resolved = settings.resolve({}, {}, {})
    assert (resolved.logs_enabled, resolved.logs_keep_days) == (True, 14)
    env = {"SAI_LOG": "off", "SAI_LOG_KEEP_DAYS": " 30 "}
    resolved = settings.resolve({}, env, {"logs.keep_days": 5})
    assert (resolved.logs_enabled, resolved.logs_keep_days) == (False, 30)
    assert resolved.sources["logs.keep_days"] == "env"
    assert settings.resolve({}, {}, {"logs.keep_days": 5}).value("logs.keep_days") == 5
    for bad in ("0", "-1", "1.5", "many", ""):
        with pytest.raises(settings.SettingsError, match="whole number"):
            settings.parse_value("logs.keep_days", bad)
    for bad in (True, 0, "7", 2.5):
        with pytest.raises(settings.SettingsError, match="whole number"):
            settings.check_value("logs.keep_days", bad)
    text, value = settings.set_value(None, "logs.keep_days", "21")
    assert value == 21 and "\nkeep_days = 21\n" in text
    assert settings.parse_file(text, "config.toml")[0] == {"logs.keep_days": 21}
    assert "# keep_days = 14" in settings.unset_value(text, "logs.keep_days")[0]


def test_disabled_by_environment_writes_nothing(home, monkeypatch, keeper_stub):
    base, _state = keeper_stub
    monkeypatch.setenv("SAI_LOG", "0")
    log.event("nothing")
    assert run_cli("--endpoint", base, "market", "prices")[0] == 0
    save_session(base)
    mcp = server.create_server(settings.load({"keeper.endpoint": base}))
    assert "oracle" in call(mcp, "market_prices")
    assert not (home / "logs").exists()
    monkeypatch.setenv("SAI_LOG", "1")
    log.event("something")
    assert [line["event"] for line in read_logs(home)] == ["something"]


def test_disabled_in_the_config_file_writes_nothing(home, keeper_stub):
    base, _state = keeper_stub
    (home / "config.toml").write_text("[logs]\nenabled = false\n")
    assert run_cli("--endpoint", base, "market", "prices")[0] == 0
    log.event("nothing")
    assert log.flush(60) and not (home / "logs").exists()
    code, output = run_cli("--json", "logs", "path")
    assert code == 0 and json.loads(output)["enabled"] is False
    # Turning it back on takes effect in the same process.
    (home / "config.toml").write_text("[logs]\nenabled = true\n")
    log.reset()
    log.event("something")
    assert [line["event"] for line in read_logs(home)] == ["something"]


def test_settings_that_cannot_be_read_turn_logging_off(home, monkeypatch):
    """An opt-out must survive a typo elsewhere, and a value sai cannot
    read is not consent to log."""
    monkeypatch.setenv("SAI_LOG", "0")
    monkeypatch.setenv("SAI_LOG_KEEP_DAYS", "fourteen")
    assert log.config() == (False, 14)
    code, _output = run_cli("auth", "logout")
    log.event("nothing")
    assert code == 0 and log.flush(60) and not (home / "logs").exists()
    for on, keep in (("1", "fourteen"), ("maybe", "14"), ("1", "0")):
        monkeypatch.setenv("SAI_LOG", on)
        monkeypatch.setenv("SAI_LOG_KEEP_DAYS", keep)
        assert log.config()[0] is False
        log.event("nothing")
    monkeypatch.delenv("SAI_LOG")
    monkeypatch.delenv("SAI_LOG_KEEP_DAYS")
    for text in ("not = [toml", "[logs]\nkeep_days = 0\n", '[logs]\nenabled = "no"\n'):
        (home / "config.toml").write_text(text)
        assert log.config()[0] is False
        log.event("nothing")
        assert log.flush(60)
    assert not (home / "logs").exists()
    (home / "config.toml").write_text("[logs]\nkeep_days = 5\n")
    assert log.config() == (True, 5)
    monkeypatch.setenv("SAI_LOG", "0")
    assert log.config()[0] is False


def test_invalid_logs_settings_never_stop_a_command_or_the_server(
    home, monkeypatch, keeper_stub
):
    """A typo in a logging setting turns logging off. Keeper commands and
    the MCP server still start, with or without logging turned off."""
    base, _state = keeper_stub
    for on in ("0", "1", None):
        monkeypatch.setenv("SAI_LOG_KEEP_DAYS", "fourteen")
        if on is None:
            monkeypatch.delenv("SAI_LOG", raising=False)
        else:
            monkeypatch.setenv("SAI_LOG", on)
        resolved = settings.load({"keeper.endpoint": base})
        assert resolved.logs_enabled is False and resolved.endpoint == base
        assert resolved.sources["logs.keep_days"] == ("env" if on == "0" else "invalid")
        result = CliRunner().invoke(
            main, ["--endpoint", base, "--json", "market", "prices"]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"oracle": {"tokenPricesUsd": []}}
        # Off by choice: the other value is not looked at, so no warning.
        assert ("logging is off" in result.stderr) == (on != "0")
        mcp = server.create_server(resolved)
        assert "oracle" in call(mcp, "market_prices")
    monkeypatch.delenv("SAI_LOG_KEEP_DAYS")
    monkeypatch.setenv("SAI_LOG", "sometimes")
    assert settings.load({}).logs_enabled is False
    assert run_cli("--endpoint", base, "market", "prices")[0] == 0
    monkeypatch.delenv("SAI_LOG")
    (home / "config.toml").write_text("[logs]\nenabled = true\nkeep_days = -3\n")
    warnings: list[str] = []
    resolved = settings.load({}, warn=warnings.append)
    assert resolved.logs_enabled is False
    assert resolved.sources["logs.keep_days"] == "invalid"
    assert len(warnings) == 1 and "logging is off" in warnings[0]
    result = CliRunner().invoke(main, ["--endpoint", base, "market", "prices"])
    assert result.exit_code == 0 and "logging is off" in result.stderr
    # The config commands still say what is wrong.
    assert run_cli("config", "set", "logs.keep_days", "many")[0] == 2
    shown = CliRunner().invoke(main, ["--json", "config", "list"])
    listed = json.loads(shown.stdout)["settings"]
    assert listed["logs.keep_days"]["source"] == "invalid"
    assert log.flush(60) and not (home / "logs").exists()
    # A bad Keeper endpoint is still an error.
    monkeypatch.setenv("SAI_ENDPOINT", "not a url")
    with pytest.raises(settings.SettingsError):
        settings.load({})


# Failures to log


@posix_only
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes"
)
def test_an_unwritable_logs_directory_does_not_break_commands(home, keeper_stub):
    base, _state = keeper_stub
    root = home / "logs"
    root.mkdir(mode=0o500)
    try:
        code, output = run_cli("--endpoint", base, "--json", "market", "prices")
        assert code == 0
        assert json.loads(output) == {"oracle": {"tokenPricesUsd": []}}
        save_session(base)
        mcp = server.create_server(settings.load({"keeper.endpoint": base}))
        assert "oracle" in call(mcp, "market_prices")
        assert list(root.iterdir()) == []
    finally:
        root.chmod(0o700)


def test_a_file_in_place_of_the_logs_directory_does_not_break_commands(
    home, keeper_stub
):
    base, _state = keeper_stub
    (home / "logs").write_text("in the way")
    code, output = run_cli("--endpoint", base, "--json", "market", "prices")
    assert code == 0 and "tokenPricesUsd" in output
    assert (home / "logs").read_text() == "in the way"
    assert run_cli("logs", "tail") == (0, "")


@posix_only
def test_a_symlinked_logs_directory_is_neither_written_nor_pruned(home):
    """An archive behind a `logs` symlink gets no new lines and loses none
    of its old files."""
    archive = home / "archive"
    archive.mkdir(mode=0o755)
    old = old_file(archive, "cli", 400)
    (home / "logs").symlink_to(archive)
    log.event("probe")
    assert log.flush(60)
    assert sorted(path.name for path in archive.iterdir()) == [old.name]
    assert stat.S_IMODE(archive.stat().st_mode) == 0o755
    assert log.log_files(home / "logs") == [] and log.tail(5) == []
    assert run_cli("logs", "tail") == (0, "")
    # Once the link is gone, logging resumes without a restart.
    (home / "logs").unlink()
    log.reset()
    log.event("after")
    assert [line["event"] for line in read_logs(home)] == ["after"]
    assert old.exists()


@posix_only
def test_an_existing_loose_file_and_directory_are_made_private(home):
    root = home / "logs"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    path = log.file_path("cli", datetime.now(UTC).date(), root)
    path.write_text('{"event":"earlier"}\n')
    path.chmod(0o644)
    log.event("probe")
    assert log.flush(60)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().count("\n") == 2


@posix_only
def test_a_fifo_or_hard_link_at_the_log_file_is_refused_without_waiting(home):
    root = home / "logs"
    root.mkdir(mode=0o700)
    today = datetime.now(UTC).date()
    os.mkfifo(log.file_path("cli", today, root))
    outside = home / "outside.jsonl"
    outside.write_text("")
    os.link(outside, log.file_path("mcp", today, root))
    start = time.monotonic()
    log.event("to.the.fifo")
    with log.bind(surface="mcp"):
        log.event("to.the.hard.link")
    assert log.flush(60)
    assert time.monotonic() - start < 30
    assert outside.read_text() == ""
    assert log.tail(5) == []
    with log.bind(surface="update"):
        log.event("to.a.file")
    assert [line["event"] for line in read_logs(home)] == ["to.a.file"]


@posix_only
def test_a_symlinked_log_file_is_not_written_through(home):
    target = home / "elsewhere.txt"
    target.write_text("")
    root = home / "logs"
    root.mkdir(mode=0o700)
    log.file_path("cli", datetime.now(UTC).date(), root).symlink_to(target)
    log.event("probe")
    assert log.flush(60) and target.read_text() == ""


# Never blocking


def test_a_stuck_writer_blocks_nothing_and_dropped_events_are_counted(
    home, keeper_stub
):
    """With the writer stuck, as on a stalled disk, events return at once,
    the intent ledger and Keeper keep working, and the overflow is counted
    instead of waited for."""
    base, _state = keeper_stub
    log.event("before")
    assert log.flush(60)
    total = log.QUEUE_SIZE + 700
    with log._io_lock:
        start = time.monotonic()
        for index in range(total):
            log.event("burst", index=index)
        storage.pending("KEY9", base, ADDR, "OPEN_TRADE", "")
        assert keeper.request(base, keeper.PRICES) == {"oracle": {"tokenPricesUsd": []}}
        storage.set_status("KEY9", base, ADDR, "accepted")
        mcp = server.create_server(settings.load({"keeper.endpoint": base}))
        assert "oracle" in call(mcp, "market_prices")
        elapsed = time.monotonic() - start
        assert not log.flush(0.2)
    assert elapsed < 30
    assert storage.list_pending(base, ADDR)[0]["status"] == "accepted"
    assert log.flush(30)
    log.event("after")
    lines = read_logs(home)
    written = sum(line["event"] == "burst" for line in lines)
    dropped = [line for line in lines if line["event"] == "log.dropped"]
    assert log.QUEUE_SIZE <= written <= log.QUEUE_SIZE + 1
    assert len(dropped) == 1 and dropped[0]["level"] == "warn"
    # The burst's overflow, plus the ledger, Keeper, and MCP events behind it.
    assert total - written <= dropped[0]["count"] <= total - written + 8
    assert set(dropped[0]) == set(log.COMMON) | {"count"}
    assert lines[-1]["event"] == "after"


def test_event_snapshots_its_fields(home):
    arguments = {"addr": ADDR, "nested": {"list": [1, 2]}}
    with log._io_lock:
        log.event("probe", arguments=arguments)
        arguments["addr"] = "changed"
        arguments["nested"]["list"].append(3)
    assert read_logs(home)[0]["arguments"] == {
        "addr": ADDR,
        "nested": {"list": [1, 2]},
    }


@posix_only
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes"
)
def test_tokens_are_reread_after_a_failed_session_read(home):
    """A session file that cannot be read for a moment must not leave the
    redactor with an empty token list once it can be read again."""
    save_session(keeper.MAINNET_HTTP, OTHER, OPAQUE_TOKEN)
    log.reset()  # As a new process: nothing remembered from the sign-in.
    path = home / "trader" / "sessions.json"
    path.chmod(0o000)
    try:
        assert log.redact(f"failed {OPAQUE_TOKEN}") == f"failed {OPAQUE_TOKEN}"
    finally:
        path.chmod(0o600)
    assert log.redact(f"failed {OPAQUE_TOKEN}") == f"failed {log.REDACTED}"
    log.event("probe", error=f"failed {OPAQUE_TOKEN}")
    assert OPAQUE_TOKEN not in raw_logs(home)


def test_a_token_queued_before_sign_out_is_still_redacted(home):
    """Sign-out removes the token from the store, but Keeper honors it until
    it expires. An event queued before the sign-out, and events written
    after it, must not keep it."""
    save_session(keeper.MAINNET_HTTP, ADDR, OPAQUE_TOKEN)
    renewed = OPAQUE_TOKEN + "-renewed"
    with log._io_lock:
        log.event("queued", error=f"failed with {OPAQUE_TOKEN}")
        save_session(keeper.MAINNET_HTTP, ADDR, renewed)
        storage.delete_session(ADDR)
        assert not storage.session_exists()
    log.event("later", error=f"old {OPAQUE_TOKEN} new {renewed}")
    raw = raw_logs(home)
    assert OPAQUE_TOKEN not in raw and raw.count(log.REDACTED) == 3
    assert log.redact(f"x {OPAQUE_TOKEN}") == f"x {log.REDACTED}"


STORE = (
    "import sys;from sai_cli.core import storage;"
    "storage.save_session({'token':sys.argv[1],'evmAddress':sys.argv[2],"
    "'endpoint':'https://k.example/query','expiresAt':'2999-01-01T00:00:00+00:00'})"
)


def test_the_writer_redacts_a_token_another_process_stored(home):
    """This process never signed in. The writer thread reads the session
    store before it writes, so the token still goes."""
    env = {**os.environ, "SAI_HOME": str(home), "SAI_LOG": "0"}
    subprocess.run(
        [sys.executable, "-c", STORE, OPAQUE_TOKEN, ADDR], env=env, check=True
    )
    assert log.known_tokens() == ()
    log.event("queued", error=f"failed with {OPAQUE_TOKEN}")
    raw = raw_logs(home)
    assert OPAQUE_TOKEN not in raw and raw.count(log.REDACTED) == 1
    assert log.known_tokens() == (OPAQUE_TOKEN,)


def test_making_an_event_never_reads_the_session_store(home, monkeypatch, keeper_stub):
    """Event creation runs on the MCP event loop and between an intent's
    reservation and its submission, so it must not wait on the session
    file. Only the writer thread and explicit redaction read it."""
    base, _state = keeper_stub
    env = {**os.environ, "SAI_HOME": str(home), "SAI_LOG": "0"}
    subprocess.run([sys.executable, "-c", STORE, jwt(), ADDR], env=env, check=True)
    readers: list[str] = []
    original = storage.load_accounts

    def recording() -> dict[str, Any]:
        readers.append(threading.current_thread().name)
        return original()

    monkeypatch.setattr(storage, "load_accounts", recording)
    with log._io_lock:
        for index in range(50):
            log.event("probe", index=index, error="text with nothing in it")
        with log.bind(surface="mcp", tool="market_prices", callId="test"):
            log.event("mcp.tool_call", arguments={"addr": ADDR})
        assert keeper.request(base, keeper.PRICES) == {"oracle": {"tokenPricesUsd": []}}
        assert readers == []
    assert log.flush(60)
    assert readers and set(readers) == {"sai-log"}
    # The session file is not even looked at: an unreadable one changes nothing.
    monkeypatch.setattr(storage, "home", lambda: 1 / 0)
    log.event("still.fine")
    assert log.flush(60)
    monkeypatch.undo()
    assert "still.fine" in raw_logs(home)


def test_a_logs_section_that_is_not_a_table_only_turns_logging_off(
    home, monkeypatch, keeper_stub
):
    base, _state = keeper_stub
    for text in ('logs = "off"\n', "logs = 3\n", "logs = []\n"):
        (home / "config.toml").write_text(text)
        warnings: list[str] = []
        resolved = settings.load({"keeper.endpoint": base}, warn=warnings.append)
        assert resolved.logs_enabled is False and resolved.endpoint == base
        assert resolved.sources["logs.enabled"] == "invalid"
        assert len(warnings) == 1 and "must be a table" in warnings[0]
        assert "logging is off" in warnings[0]
        assert log.config()[0] is False
        result = CliRunner().invoke(
            main, ["--endpoint", base, "--json", "market", "prices"]
        )
        assert result.exit_code == 0 and "tokenPricesUsd" in result.stdout
        assert "oracle" in call(server.create_server(resolved), "market_prices")
    assert log.flush(60) and not (home / "logs").exists()
    # Explicitly off in the environment: still no failure.
    monkeypatch.setenv("SAI_LOG", "0")
    assert run_cli("--endpoint", base, "market", "prices")[0] == 0
    monkeypatch.delenv("SAI_LOG")
    # A trading section that is not a table is still an error.
    (home / "config.toml").write_text('keeper = "https://k.example/query"\n')
    with pytest.raises(settings.SettingsError, match="must be a table"):
        settings.load({})


def test_every_value_counts_toward_an_events_budget(home):
    """Numbers, flags, and containers are charged too, so a deep or wide
    value of nothing but numbers cannot be copied whole."""
    wide = [[index] * 60 for index in range(60)]
    copy = log._snapshot(
        {"values": wide}, budget := [log.EVENT_CHARS, log.EVENT_NODES], ()
    )
    kept = json.dumps(copy)
    assert kept.count(",") < log.EVENT_NODES + 8 and log.CUT in kept
    assert budget[1] <= 0
    numbers = [log.EVENT_CHARS, log.EVENT_NODES]
    log._snapshot(list(range(60)), numbers, ())
    assert log.EVENT_CHARS - numbers[0] == 61 * log.NODE_CHARS
    assert log._snapshot(10**400, [log.EVENT_CHARS, log.EVENT_NODES], ()) == log.CUT
    assert log._snapshot(2**63, [log.EVENT_CHARS, log.EVENT_NODES], ()) == 2**63
    with log._io_lock:
        log.event("first")
        for _ in range(log.QUEUE_SIZE - 10):
            log.event("numbers", values=wide)
        assert 0 < log._queued_chars <= log.QUEUE_CHARS
        assert log._queue.qsize() < log.QUEUE_SIZE // 2
    lines = read_logs(home)
    assert sum(line["event"] == "log.dropped" for line in lines) == 1
    assert all(len(json.dumps(line)) < log.MAX_LINE for line in lines)


def test_a_container_in_place_of_a_scalar_argument_is_logged_by_type(home):
    save_session(keeper.MAINNET_HTTP)
    mcp = server.create_server(settings.load({}))
    nested = [[index] * 500 for index in range(500)]
    with pytest.raises(ToolError):
        asyncio.run(mcp.call_tool("account_positions", {"addr": nested}))
    with pytest.raises(ToolError):
        asyncio.run(mcp.call_tool("trade_close", {"trade_id": {"a": {"b": 1}}}))
    first, second = events(home, "mcp.tool_call")
    assert first["arguments"] == {"addr": "[list of 500]"}
    assert second["arguments"] == {"trade_id": "[dict of 1]"}
    assert server.loggable_value(7) == 7 and server.loggable_value(None) is None
    assert server.loggable_value("x") == "x"


def test_a_redirected_logs_directory_is_refused_without_directory_descriptors(
    home, monkeypatch
):
    """The Windows fallback works by path, so a junction or any other
    reparse point at the logs directory must be refused."""
    root = home / "logs"
    root.mkdir(mode=0o700)
    assert log.redirected(root) is False
    monkeypatch.setattr(log, "_DIR_FD", False)
    opened = log.LogDir.open(root)
    assert opened is not None and opened.fd is None
    monkeypatch.setattr(Path, "is_junction", lambda self: self == root, raising=False)
    assert log.redirected(root) is True and log.LogDir.open(root) is None
    log.event("probe")
    assert log.flush(60) and list(root.iterdir()) == []
    assert log.tail(5) == [] and log.log_files(root) == []
    monkeypatch.setattr(Path, "is_junction", lambda self: False, raising=False)

    class Info:
        st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT

    monkeypatch.setattr(log.os, "lstat", lambda path: Info())
    assert log.redirected(root) is True
    Info.st_file_attributes = 0x10  # FILE_ATTRIBUTE_DIRECTORY
    assert log.redirected(root) is False
    # No attributes to read on Windows: it cannot be checked, so refuse.
    monkeypatch.setattr(log.os, "lstat", lambda path: object())
    monkeypatch.setattr(log.os, "name", "nt")
    assert log.redirected(root) is True


def test_a_stuck_writer_delays_exit_only_briefly_and_bounds_the_queue(home):
    assert log.EXIT_FLUSH_SECONDS <= 0.3
    with log._io_lock:
        log.event("first")
        start = time.monotonic()
        assert log.flush(log.EXIT_FLUSH_SECONDS) is False
        assert time.monotonic() - start < 2
        # Big events fill the queue by size long before they fill it by count.
        for _ in range(log.QUEUE_SIZE - 10):
            log.event("big", items=["y " * 900 for _ in range(60)])
        assert log._queued_chars <= log.QUEUE_CHARS
        assert log._queue.qsize() < log.QUEUE_SIZE // 2
    lines = read_logs(home)
    dropped = [line for line in lines if line["event"] == "log.dropped"]
    written = sum(line["event"] == "big" for line in lines)
    assert len(dropped) == 1 and dropped[0]["count"] + written == log.QUEUE_SIZE - 10
    assert log._queued_chars == 0


# sai mcp serve


def test_sai_mcp_serve_logs_tool_calls_and_keeps_stdout_protocol_only(home):
    """Drive a real `sai mcp serve` over stdio: every stdout line is the
    JSON-RPC reply to a request, and the calls land in the mcp log file."""
    endpoint = f"http://127.0.0.1:9/query?api_key={URL_KEY}"

    def rpc(message_id: int | None, method: str, params: dict[str, Any]) -> str:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if message_id is not None:
            message["id"] = message_id
        return json.dumps(message) + "\n"

    with subprocess.Popen(
        [*sai_command(), "mcp", "serve", "--endpoint", endpoint],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"SAI_HOME": str(home), "PATH": os.defpath},
        text=True,
    ) as process:
        assert process.stdin is not None and process.stdout is not None

        def ask(message_id: int, method: str, params: dict[str, Any]) -> Any:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(rpc(message_id, method, params))
            process.stdin.flush()
            reply = json.loads(process.stdout.readline())
            assert reply["jsonrpc"] == "2.0" and reply["id"] == message_id
            return reply["result"]

        client = {"name": "test", "version": "0"}
        ask(
            1,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": client},
        )
        process.stdin.write(rpc(None, "notifications/initialized", {}))
        info = ask(2, "tools/call", {"name": "mcp_info", "arguments": {}})
        prices = ask(3, "tools/call", {"name": "market_prices", "arguments": {}})
        # The server writes its log in the background; wait for both calls.
        deadline = time.monotonic() + 60
        while (
            sum(line["event"] == "mcp.tool_result" for line in read_logs(home)) < 2
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        process.stdin.close()
        rest = process.stdout.read()
        stderr = process.stderr.read() if process.stderr else ""
        assert process.wait(timeout=30) == 0, stderr
    assert rest == ""
    assert json.loads(info["content"][0]["text"])["logs"]["dir"] == str(home / "logs")
    assert json.loads(prices["content"][0]["text"])["code"] == "NOT_SENT"
    lines = read_logs(home)
    assert {line["surface"] for line in lines} == {"mcp"}
    expected = [
        ("command.start", None),
        ("mcp.server_start", None),
        ("mcp.tool_call", "mcp_info"),
        ("mcp.tool_result", "mcp_info"),
        ("mcp.tool_call", "market_prices"),
        ("keeper.request", "market_prices"),
        ("mcp.tool_result", "market_prices"),
        ("command.end", None),
    ]
    found = [(line["event"], line.get("tool")) for line in lines]
    # The last line is written in the 0.3 s the server waits at exit, which
    # a loaded machine can miss.
    assert found in (expected, expected[:-1])
    assert lines[0]["command"] == "mcp serve"
    assert lines[-1].get("exitCode", 0) == 0
    assert lines[1]["keeper"] == "http://127.0.0.1:9"
    assert lines[1]["websocket"] == "ws://127.0.0.1:9"
    assert lines[5]["status"] == "not_sent" and lines[6]["code"] == "NOT_SENT"
    assert len({line["runId"] for line in lines}) == 1
    assert URL_KEY not in raw_logs(home)
    assert_log_golden(
        "mcp-prices-failure",
        [line for line in lines if line.get("tool") == "market_prices"],
        {},
    )


# sai logs


def test_logs_intent_joins_cli_and_mcp_and_reads_keeper(home, keeper_stub):
    endpoint, state = keeper_stub
    token = save_session(endpoint)
    request_hash = "ab" * 32
    state.ack_tx_hash = request_hash.upper()
    submitted = subprocess.run(
        [*sai_command(), "--json", "--endpoint", endpoint, "trade", "close", "7"],
        env={"SAI_HOME": str(home), "PATH": os.defpath},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    key = json.loads(submitted.stdout)["intentKey"]
    submitted_lines = [
        line for line in read_logs(home, "cli") if line.get("command") == "trade close"
    ]
    assert_log_golden(
        "cli-trade-close",
        submitted_lines,
        {
            key: "20260101T000000000Z_0123456789ABCDEF",
            endpoint.removesuffix("/query"): "http://127.0.0.1:43210",
        },
    )
    assert state.authorizations == [f"Bearer {token}"]
    record = {
        "intentKey": key,
        "kind": "CLOSE_POSITION",
        "lifecycleStep": "exec_req_pass",
        "execActor": "server",
        "evmAddress": ADDR,
        "txHash": request_hash,
        "execBlock": 123,
    }
    state.intents = [record]
    mcp = server.create_server(settings.load({"keeper.endpoint": endpoint}))
    assert call(mcp, "intent_status", {"addr": ADDR, "key": key})["userIntents"] == [
        record
    ]
    call(mcp, "market_prices")  # Same server process, a different call.
    calls_before = len(state.requests)
    before = raw_logs(home)
    exit_code, output = run_cli("--json", "logs", "intent", key, "--offline")
    assert exit_code == 0 and len(state.requests) == calls_before
    assert raw_logs(home) == before
    trail = json.loads(output)
    assert trail["keeper"] is None and trail["gaps"] == []
    assert trail["txHash"] == request_hash
    safe_submission = [
        {key: value for key, value in line.items() if key != "argv"}
        for line in submitted_lines
    ]
    assert trail["events"][: len(submitted_lines)] == safe_submission
    mcp_lines = trail["events"][len(submitted_lines) :]
    assert {line["tool"] for line in mcp_lines} == {"intent_status"}
    assert [line["event"] for line in mcp_lines] == [
        "mcp.tool_call",
        "keeper.request",
        "intent.keeper",
        "mcp.tool_result",
    ]
    assert len({line["runId"] for line in trail["events"]}) == 2

    for identifier, expected in (
        (submitted_lines[0]["runId"], safe_submission),
        (mcp_lines[0]["callId"], mcp_lines),
    ):
        code, output = run_cli("--json", "logs", "run", identifier)
        assert code == 0
        assert json.loads(output) == {"id": identifier, "events": expected, "gaps": []}
    assert "intent.submitted" in run_cli("logs", "run", submitted_lines[0]["runId"])[1]

    # The read sees a later record but must leave the accepted ledger row alone.
    state.intents = [
        {**record, "evmAddress": OTHER},
        {
            **record,
            "lifecycleStep": "exec_fail",
            "errorMsg": f"echo {token} {SIGNATURE} opaque-credential-from-upstream",
            "extra": token,
        },
    ]
    cli_args = ("--json", "--endpoint", endpoint, "logs", "intent", key)
    code, output = run_cli(*cli_args)
    assert code == 0 and token not in output and SIGNATURE not in output
    assert "opaque-credential-from-upstream" not in output
    current = json.loads(output)
    assert current["keeper"]["lifecycleStep"] == "exec_fail"
    assert current["keeper"]["evmAddress"] == ADDR
    assert "extra" not in current["keeper"]
    assert current["events"] == trail["events"]
    assert state.requests[-1]["variables"] == {"user": ADDR, "key": key}
    assert state.authorizations[-1] is None
    assert call(mcp, "logs_intent", {"key": key}) == current
    assert call(mcp, "logs_intent", {"key": key, "offline": True}) == trail
    assert json.loads(run_cli(*cli_args)[1]) == current
    assert storage.list_pending(endpoint, ADDR)[0]["status"] == "accepted"

    state.status = 503
    failed_lookup = json.loads(run_cli(*cli_args)[1])
    assert failed_lookup["keeperError"]["code"] == "KEEPER_ERROR"
    assert (
        failed_lookup["keeper"] is None and failed_lookup["events"] == trail["events"]
    )
    assert (
        f"Request transaction {request_hash}"
        in run_cli("logs", "intent", key, "--offline")[1]
    )


def test_logs_intent_honors_root_account_selector(keeper_stub):
    endpoint, state = keeper_stub
    save_session(endpoint, OTHER)
    save_session(endpoint, ADDR)
    key = "20260101T000000000Z_0123456789ABCDEF"
    state.intents = [
        {"intentKey": key, "evmAddress": OTHER, "lifecycleStep": "exec_req_pass"},
        {"intentKey": key, "evmAddress": ADDR, "lifecycleStep": "exec_req_pass"},
    ]
    args = ("--json", "--endpoint", endpoint, "--as", OTHER, "logs", "intent", key)
    for flags, account in (((), OTHER), (("--addr", ADDR), ADDR)):
        code, output = run_cli(*args, *flags)
        assert code == 0
        report = json.loads(output)
        assert report["events"] == []
        assert report["keeper"]["evmAddress"] == account
        assert state.requests[-1]["variables"] == {"user": account, "key": key}
        assert state.authorizations[-1] is None


def test_log_queries_report_process_gaps_and_order_legacy_events(home):
    path = log.file_path("mcp", log.now().date(), home / "logs")
    path.parent.mkdir()
    identity = {"ts": log.stamp(log.now()), "runId": "server", "surface": "mcp"}
    first = {**identity, "event": "mcp.tool_call", "callId": "first", "seq": 1}
    anchor = {
        **identity,
        "event": "intent.keeper",
        "intentKey": "KEY",
        "addr": ADDR,
        "endpointId": "h1_" + "1" * 64,
        "callId": "first",
        "seq": 2,
    }
    gap = {**identity, "event": "log.dropped", "seq": 2, "count": 3}
    sibling = {**identity, "event": "mcp.tool_call", "callId": "other", "seq": 3}
    path.write_bytes(
        log.encode(sibling)
        + log.encode(gap)
        + b"invalid\n"
        + b" " * (log.MAX_LINE + 10)
        + b"\n"
        + log.encode(anchor)
        + log.encode(first)
        + b'{"incomplete":'
    )
    queries = [
        (("intent", "KEY", "--offline"), [first, anchor]),
        (("run", "first"), [first, anchor]),
        (("run", "server"), [first, anchor, sibling]),
    ]
    for args, expected in queries:
        code, output = run_cli("--json", "logs", *args)
        assert code == 0
        result = json.loads(output)
        assert result["events"] == expected and result["gaps"] == [gap]
    assert "Gap  " in run_cli("logs", "run", "first")[1]
    assert "count=3" in run_cli("logs", "intent", "KEY", "--offline")[1]


def test_logs_intent_keeps_failed_status_calls_in_the_trail(keeper_stub):
    endpoint, state = keeper_stub
    state.status = 503
    mcp = server.create_server(settings.load({"keeper.endpoint": endpoint}))
    response = call(mcp, "intent_status", {"addr": ADDR, "key": "KEY"})
    assert response["code"] == "KEEPER_ERROR"
    trail = call(mcp, "logs_intent", {"key": "KEY", "offline": True})
    assert [line["event"] for line in trail["events"]] == [
        "mcp.tool_call",
        "keeper.request",
        "mcp.tool_result",
    ]
    assert trail["events"][1]["intentKey"] == "KEY"


def test_intent_scope_separates_accounts_paths_and_repl_invocations(home, keeper_stub):
    endpoint, state = keeper_stub
    key = "KEY"
    hash_a, hash_b, hash_path = ("0x" + char * 64 for char in ("a", "b", "c"))
    base = {
        "intentKey": key,
        "evmAddress": ADDR,
        "kind": "CLOSE_POSITION",
        "lifecycleStep": "exec_req_pass",
        "execActor": "server",
    }

    def status(url, addr, key):
        return run_cli(
            "--json", "--endpoint", url, "intent", "status", addr, "--key", key
        )

    state.intents = [
        {**base, "evmAddress": OTHER, "txHash": hash_b},
        {**base, "txHash": hash_a},
    ]
    assert status(endpoint, ADDR, key)[0] == 0
    state.intents = []
    assert status(endpoint, OTHER, key)[0] == 0
    assert status(endpoint, ADDR, "UNRELATED")[0] == 0

    trail = json.loads(run_cli("--json", "logs", "intent", key, "--offline")[1])
    assert trail["txHash"] == hash_a
    assert len({line["commandId"] for line in trail["events"]}) == 1
    assert {line.get("addr") for line in trail["events"]} == {None, ADDR}
    assert not any(line.get("intentKey") == "UNRELATED" for line in trail["events"])
    assert [
        line["txHash"] for line in events(home, "intent.keeper") if "txHash" in line
    ] == [hash_a]

    state.intents = [{**base, "evmAddress": OTHER, "txHash": hash_b}]
    other = json.loads(
        run_cli(
            "--json", "--endpoint", endpoint, "logs", "intent", key, "--addr", OTHER
        )[1]
    )
    assert other["keeper"]["evmAddress"] == OTHER and other["txHash"] == hash_b
    assert all(line.get("addr") != ADDR for line in other["events"])

    other_endpoint = endpoint + "/opaque-path-secret?secret=opaque-query-secret"
    state.intents = [{**base, "txHash": hash_path}]
    assert status(other_endpoint, ADDR, key)[0] == 0
    assert log.origin(other_endpoint) == log.origin(endpoint)
    assert log.endpoint_id(other_endpoint) != log.endpoint_id(endpoint)
    assert log.endpoint_id(other_endpoint) != log.endpoint_id(
        other_endpoint.partition("?")[0]
    )
    result = json.loads(run_cli("--json", "logs", "intent", key, "--offline")[1])
    assert (
        result["events"] == [] and result["txHash"] is None and "scopeWarning" in result
    )
    result = json.loads(
        run_cli(
            "--json", "--endpoint", other_endpoint, "logs", "intent", key, "--offline"
        )[1]
    )
    assert result["txHash"] == hash_path
    assert "opaque-path-secret" not in raw_logs(home)
    assert "opaque-query-secret" not in raw_logs(home)

    state.intents = [{**base, "evmAddress": OTHER, "txHash": hash_b}]
    assert status(endpoint, OTHER, key)[0] == 0
    request_count = len(state.requests)
    code, output = run_cli("--endpoint", endpoint, "logs", "intent", key)
    assert code == 0 and "Several accounts match this key" in output
    assert "Keeper has no record" not in output
    assert len(state.requests) == request_count


def test_query_routed_keepers_and_homes_have_distinct_endpoint_ids(home, keeper_stub):
    base, state = keeper_stub
    route_a = base + "?route=opaque-route-a"
    route_b = base + "?route=opaque-route-b"
    key = "KEY"
    record = {
        "intentKey": key,
        "evmAddress": ADDR,
        "execActor": "server",
        "kind": "CLOSE_POSITION",
        "lifecycleStep": "exec_req_pass",
    }
    state.intents = [{**record, "txHash": "A" * 64}]
    assert (
        run_cli("--endpoint", route_a, "intent", "status", ADDR, "--key", key)[0] == 0
    )
    state.intents = [{**record, "txHash": "B" * 64}]
    report = json.loads(
        run_cli("--json", "--endpoint", route_b, "logs", "intent", key, "--addr", ADDR)[
            1
        ]
    )
    assert report["keeper"]["txHash"] == report["txHash"] == "b" * 64
    assert report["events"] == []

    assert (
        run_cli("--endpoint", route_b, "intent", "status", ADDR, "--key", key)[0] == 0
    )
    identity_a, identity_b = log.endpoint_id(route_a), log.endpoint_id(route_b)
    assert identity_a and identity_b and identity_a != identity_b
    entries = events(home, "intent.keeper")
    assert [line["endpointId"] for line in entries] == [identity_a, identity_b]
    assert {line["endpoint"] for line in entries} == {log.origin(base)}
    assert "opaque-route-" not in raw_logs(home)
    keyfile = home / "logs" / log.ENDPOINT_KEY
    assert keyfile.stat().st_size == 32
    if os.name != "nt":
        assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600

    second_home = home / "second-home"
    assert log.endpoint_id(route_a, second_home / "logs") is None
    assert not second_home.exists()
    subprocess.run(
        [*sai_command(), "--endpoint", route_a, "market", "prices"],
        env={"SAI_HOME": str(second_home), "PATH": os.defpath},
        capture_output=True,
        check=True,
        timeout=30,
    )
    second_identity = log.endpoint_id(route_a, second_home / "logs")
    assert second_identity and second_identity != identity_a


def test_keeper_lifecycle_codes_and_user_actor_survive_mcp_and_audit(home, keeper_stub):
    endpoint, state = keeper_stub
    mcp = server.create_server(settings.load({"keeper.endpoint": endpoint}))
    key = "20260101T000000000Z_0123456789ABCDEF"
    nibi = "nibi1hwamhwamhwamhwamhwamhwamhwamhwamc9gs8a"
    for code, actor, step, value, normalized in (
        ("simulation_failed", "server", "exec_req_fail", "AB" * 32, "ab" * 32),
        (
            "chain_execution_failed",
            "user",
            "exec_fail",
            "0x" + "CD" * 32,
            "0x" + "cd" * 32,
        ),
    ):
        state.intents = [
            {
                "intentKey": key,
                "evmAddress": ADDR,
                "nibiAddress": nibi,
                "execActor": actor,
                "lifecycleStep": step,
                "errorCode": code,
                "errorMsg": "opaque-upstream-error",
                "txHash": value,
            }
        ]
        status = call(mcp, "intent_status", {"addr": ADDR, "key": key})["userIntents"][
            0
        ]
        assert (status["errorCode"], status["execActor"], status["txHash"]) == (
            code,
            actor,
            normalized,
        )
        assert status["errorMsg"] == "Keeper reported an intent error."
        assert status["nibiAddress"] == nibi
        assert "txHashInvalid" not in status
        report = call(mcp, "logs_intent", {"key": key, "addr": ADDR})
        assert report["keeper"]["errorCode"] == code
        assert report["keeper"]["execActor"] == actor
        assert report["txHash"] == normalized
    assert "opaque-upstream-error" not in raw_logs(home)


@pytest.mark.parametrize(
    "error_code",
    [
        "market_order_expired",
        "market_order_fill_failed",
        "market_order_trading_paused",
        "market_order_market_closed",
        "market_order_trade_not_open",
    ],
)
def test_keeper_cancellation_codes_survive_intent_status_and_audit(
    keeper_stub, error_code
):
    endpoint, state = keeper_stub
    mcp = server.create_server(settings.load({"keeper.endpoint": endpoint}))
    key = "20260101T000000000Z_0123456789ABCDEF"
    state.intents = [
        {
            "intentKey": key,
            "evmAddress": ADDR,
            "execActor": "server",
            "lifecycleStep": "exec_fail",
            "errorCode": error_code,
            "errorMsg": "opaque-keeper-diagnostic",
        }
    ]
    args = {"key": key, "addr": ADDR}
    records = [
        call(mcp, "intent_status", args)["userIntents"][0],
        call(mcp, "logs_intent", args)["keeper"],
    ]
    for record in records:
        assert record["errorCode"] == error_code
        assert record["errorMsg"] == "Keeper reported an intent error."


def test_mcp_audit_omits_invalid_arguments_from_call_log(home):
    mcp = server.create_server()
    marker = "opaque-audit-argument-secret"
    result = call(
        mcp,
        "logs_intent",
        {"key": "KEY", "offline": True, "before": marker, "addr": ADDR},
    )
    assert result["code"] == "INVALID_REQUEST"
    assert events(home, "mcp.tool_call")[0]["arguments"] == {}
    assert marker not in raw_logs(home)


@pytest.mark.parametrize("logging", [True, False], ids=["logging-on", "logging-off"])
def test_mcp_audit_validation_names_types_without_rejected_values(
    home, monkeypatch, logging
):
    if not logging:
        monkeypatch.setenv("SAI_LOG", "0")
    mcp = server.create_server()
    secret = "opaque-rejected-audit-argument"
    cases = [
        ({"offline": secret}, "Argument 'offline' must be a boolean."),
        ({"before": [secret]}, "Argument 'before' must be a string or null."),
        ({"addr": {"value": secret}}, "Argument 'addr' must be a string or null."),
        ({"key": [secret]}, "Argument 'key' must be a string."),
        ({secret: secret}, "Expected audit arguments 'key' (a string)"),
    ]

    async def run():
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            for arguments, message in cases:
                result = await client.call_tool(
                    "logs_intent", {"key": "KEY", "offline": True} | arguments
                )
                assert result.isError
                assert isinstance(result.content[0], TextContent)
                assert message in result.content[0].text
                assert secret not in result.model_dump_json()

    asyncio.run(run())
    results = events(home, "mcp.tool_result")
    assert len(results) == (len(cases) if logging else 0)
    assert all(line["code"] == "INVALID_REQUEST" for line in results)
    assert secret not in raw_logs(home)


def test_invalid_hash_and_keeper_text_are_excluded_from_logs_and_mcp(home, keeper_stub):
    endpoint, state = keeper_stub
    save_session(endpoint)
    secret = "some-opaque-credential-unrelated-to-the-active-session"
    state.ack_tx_hash = secret
    code, output = run_cli("--json", "--endpoint", endpoint, "trade", "close", "7")
    assert code == 0
    key = json.loads(output)["intentKey"]
    submitted = events(home, "intent.submitted")[0]
    assert submitted["txHashInvalid"] is True and "txHash" not in submitted
    state.intents = [
        {
            "intentKey": key,
            "evmAddress": ADDR,
            "txHash": 123,
            "kind": secret,
            "execActor": secret,
            "lifecycleStep": "exec_fail",
            "errorCode": secret,
            "errorMsg": secret,
        }
    ]
    mcp = server.create_server(settings.load({"keeper.endpoint": endpoint}))
    status = call(mcp, "intent_status", {"addr": ADDR, "key": key})
    assert status["userIntents"][0]["txHashInvalid"] is True
    assert status["userIntents"][0]["errorCode"] == "KEEPER_ERROR"
    assert secret not in json.dumps(status)
    assert events(home, "intent.keeper")[0]["txHashInvalid"] is True
    trail = call(mcp, "logs_intent", {"addr": ADDR, "key": key})
    assert secret not in json.dumps(trail) and trail["txHash"] is None
    assert trail["keeper"]["errorMsg"] == "Keeper reported an intent error."
    assert secret not in raw_logs(home)
    state.errors = [{"message": secret, "extensions": {"code": secret}}]
    missing = call(mcp, "logs_intent", {"key": secret, "addr": ADDR})
    assert missing["intentKey"] is None and secret not in json.dumps(missing)
    assert secret not in raw_logs(home)
    assert missing["keeperError"]["code"] == "KEEPER_ERROR"
    state.errors = None
    invalid = call(mcp, "logs_intent", {"key": "https://host/?token=" + secret})
    assert invalid["code"] == "INVALID_REQUEST" and secret not in json.dumps(invalid)
    state.intents = [
        {
            **state.intents[0],
            "lifecycleStep": "exec_pass",
            "execActor": "server",
            "kind": "CLOSE_POSITION",
            "txHash": "a" * 64,
        }
    ]
    state.history = [
        {
            "intentKey": key,
            "trade": {"id": 7, "trader": ADDR, "extra": secret},
            "evmTxHash": "0x" + "a" * 64,
            "txHash": secret,
            "tradeChangeType": secret,
        }
    ]
    status = call(mcp, "intent_status", {"addr": ADDR, "key": key})
    assert status["matchedTradeHistory"] == [
        {
            "intentKey": key,
            "trade": {"id": 7, "trader": ADDR},
            "txHashInvalid": True,
            "evmTxHash": "0x" + "a" * 64,
        }
    ]
    assert secret not in json.dumps(status)


def test_intent_reports_unattributed_gaps_and_skips_nonfinite_rows(home):
    root = home / "logs"
    root.mkdir()
    path = log.file_path("cli", log.now().date(), root)
    scope = {"addr": ADDR, "endpointId": "h1_" + "1" * 64}
    row = {
        **scope,
        "ts": log.stamp(log.now()),
        "runId": "process",
        "event": "intent.keeper",
        "intentKey": "KEY",
        "seq": 1,
    }
    gap = {
        "event": "log.dropped",
        "count": 5,
        "ts": row["ts"],
        "runId": "process",
        "seq": 2,
    }
    prefix = json.dumps(row).encode()[:-1]
    path.write_bytes(
        b"".join(
            prefix + b', "durationMs": ' + value + b"}\n"
            for value in (b"NaN", b"Infinity", b"-Infinity", b"1e999")
        )
        + log.encode(gap)
    )
    result = json.loads(run_cli("--json", "logs", "intent", "KEY", "--offline")[1])
    assert result["events"] == []
    assert result["unattributedGaps"] == [gap]
    assert "Unattributed gap" in run_cli("logs", "intent", "KEY", "--offline")[1]
    assert json.loads(run_cli("--json", "logs", "run", "process")[1])["events"] == []


def test_log_reports_cap_rows_and_bytes_and_offer_earlier_timestamps(home):
    root = home / "logs"
    root.mkdir()
    day = log.now()
    rows = [
        {
            "event": "command.start",
            "runId": "process",
            "seq": i,
            "ts": log.stamp(day.replace(microsecond=0) + timedelta(milliseconds=i)),
            "command": "z" * 1900,
        }
        for i in range(log_queries.MAX_REPORT_ROWS + 30)
    ]
    log.file_path("cli", day.date(), root).write_bytes(
        b"".join(log.encode(row) for row in rows)
    )
    code, output = run_cli("--json", "logs", "run", "process")
    assert code == 0 and len(output.encode()) < log_queries.MAX_REPORT_BYTES
    first = json.loads(output)
    assert first["truncated"] and len(first["events"]) < log_queries.MAX_REPORT_ROWS
    code, output = run_cli(
        "--json", "logs", "run", "process", "--before", first["nextBefore"]
    )
    assert code == 0
    earlier = json.loads(output)
    assert earlier["events"][-1]["seq"] + 1 == first["events"][0]["seq"]
    for row in rows:
        row.pop("command")
    log.file_path("cli", day.date(), root).write_bytes(
        b"".join(log.encode(row) for row in rows)
    )
    assert len(log_queries.run("process")["events"]) == log_queries.MAX_REPORT_ROWS


def test_logs_run_before_skips_newer_rows_in_long_run(home):
    root = home / "logs"
    root.mkdir()
    start = datetime(2026, 11, 24, tzinfo=UTC)
    rows = [
        {
            "event": "command.start",
            "runId": "process",
            "seq": i,
            "ts": log.stamp(start + timedelta(milliseconds=i)),
        }
        for i in range(21_000)
    ]
    log.file_path("cli", start.date(), root).write_bytes(
        b"".join(log.encode(row) for row in rows)
    )

    before = log.stamp(start + timedelta(milliseconds=10))
    code, output = run_cli("--json", "logs", "run", "process", "--before", before)
    assert code == 0
    report = json.loads(output)
    assert report["events"] == rows[:10]
    assert "truncated" not in report


def test_logs_run_before_attributes_gap_using_later_call_event(home):
    root = home / "logs"
    root.mkdir()
    gap = {
        "event": "log.dropped",
        "count": 2,
        "runId": "process",
        "ts": "2026-11-24T00:00:00.000Z",
    }
    result = {
        "event": "mcp.tool_result",
        "callId": "call",
        "runId": "process",
        "ts": "2026-11-24T00:00:01.000Z",
    }
    log.file_path("mcp", date(2026, 11, 24), root).write_bytes(
        log.encode(gap) + log.encode(result)
    )

    code, output = run_cli("--json", "logs", "run", "call", "--before", result["ts"])
    assert code == 0
    report = json.loads(output)
    assert report["events"] == []
    assert report["gaps"] == [gap]


@pytest.mark.parametrize("cli_count", [1, log_queries.MAX_REPORT_ROWS + 1])
def test_logs_run_paging_defers_rows_below_the_scan_frontier(home, cli_count):
    root = home / "logs"
    root.mkdir()
    start = datetime(2026, 11, 24, tzinfo=UTC)

    def row(i: int, run_id: str) -> dict[str, Any]:
        return {
            "event": "command.start",
            "runId": run_id,
            "seq": i,
            "ts": log.stamp(start + timedelta(milliseconds=i)),
        }

    early = [row(i, "target") for i in range(cli_count)]
    buried = row(10_000, "target")
    log.file_path("cli", start.date(), root).write_bytes(
        b"".join(log.encode(line) for line in early)
    )
    log.file_path("mcp", start.date(), root).write_bytes(
        log.encode(buried)
        + b"".join(
            log.encode(row(10_001 + i, "other")) for i in range(log.MAX_SCAN_ROWS + 1)
        )
    )

    report = log_queries.run("target")
    assert report["events"] == []
    pages = []
    while before := report.get("nextBefore"):
        report = log_queries.run("target", before=before)
        pages.append(report["events"])
        assert len(pages) <= 3
    assert [line for page in reversed(pages) for line in page] == [*early, buried]


def test_earlier_intent_page_keeps_same_day_invocation_anchor(home, keeper_stub):
    endpoint, state = keeper_stub
    state.intents = [
        {
            "intentKey": "KEY",
            "evmAddress": ADDR,
            "lifecycleStep": "exec_req_pass",
            "execActor": "server",
        }
    ]
    assert (
        run_cli("--endpoint", endpoint, "intent", "status", ADDR, "--key", "KEY")[0]
        == 0
    )
    status = events(home, "intent.keeper")[0]
    path = log.file_path("cli", log.now().date(), home / "logs")
    earlier = {
        "event": "command.start",
        "runId": status["runId"],
        "commandId": status["commandId"],
        "ts": "2000-01-01T00:00:00.000Z",
        "seq": 0,
    }
    with path.open("ab") as stream:
        stream.write(log.encode(earlier))
    report = log_queries.intent("KEY", before=status["ts"])
    assert earlier in report["events"]


def test_intent_page_finds_start_and_anchor_in_two_utc_days(home):
    root = home / "logs"
    root.mkdir()
    start = {
        "event": "command.start",
        "runId": "process",
        "commandId": "command",
        "seq": 1,
        "ts": "2026-11-23T23:59:59.900Z",
        "surface": "cli",
    }
    anchor = {
        **start,
        "event": "intent.reserved",
        "intentKey": "KEY",
        "addr": ADDR,
        "endpointId": "h1_" + "1" * 64,
        "seq": 2,
        "ts": "2026-11-24T00:00:00.100Z",
    }
    previous = log.file_path("cli", date(2026, 11, 23), root)
    following = log.file_path("cli", date(2026, 11, 24), root)
    previous.write_bytes(log.encode(start))
    following.write_bytes(log.encode(anchor))
    assert log_queries.intent("KEY", before="2026-11-23T23:59:59.999Z")["events"] == [
        start
    ]
    assert log_queries.intent("KEY")["events"] == [start, anchor]

    noise = {
        **anchor,
        "event": "command.start",
        "intentKey": "OTHER",
        "commandId": "other",
    }
    following.write_bytes(log.encode(noise) * log.MAX_SCAN_ROWS + log.encode(anchor))
    assert log_queries.intent("KEY", before=anchor["ts"])["events"] == [start]
    report = log_queries.intent("KEY")
    assert report["truncated"] and report["events"] == [start, anchor]
    assert report["nextBefore"] == anchor["ts"]


def test_stats_does_not_spend_row_budget_on_stale_other_surface(home):
    root = home / "logs"
    root.mkdir()
    now = log.now()
    fresh = {
        "event": "keeper.request",
        "operation": "prices",
        "durationMs": 7,
        "ts": log.stamp(now),
    }
    stale = {**fresh, "ts": log.stamp(now - timedelta(hours=4))}
    log.file_path("cli", now.date(), root).write_bytes(log.encode(fresh))
    log.file_path("mcp", now.date(), root).write_bytes(
        log.encode({**fresh, "operation": "markets"})
        + log.encode(stale) * (log.MAX_SCAN_ROWS + 1)
    )
    report = log_queries.stats("2h")
    assert report["keeper"] == {
        "markets": {"count": 1, "p50Ms": 7, "p95Ms": 7},
        "prices": {"count": 1, "p50Ms": 7, "p95Ms": 7},
    }
    assert "truncated" not in report


def test_stats_caps_the_number_of_scanned_rows(home):
    root = home / "logs"
    root.mkdir()
    start = datetime(2026, 11, 24, 11, tzinfo=UTC)
    row = {
        "event": "keeper.request",
        "operation": "prices",
        "durationMs": 7,
        "code": "KEEPER_ERROR",
    }
    log.file_path("mcp", start.date(), root).write_bytes(
        b"".join(
            log.encode({**row, "ts": log.stamp(start + timedelta(milliseconds=i))})
            for i in range(log.MAX_SCAN_ROWS + 1)
        )
    )
    log.file_path("cli", start.date(), root).write_bytes(
        log.encode(
            {
                **row,
                "operation": "markets",
                "ts": log.stamp(start - timedelta(hours=10)),
            }
        )
    )
    report = log_queries.stats()
    assert (
        report["truncated"]
        and report["keeper"]["prices"]["count"] == log.MAX_SCAN_ROWS - 1
    )
    assert "markets" not in report["keeper"]
    assert report["errors"] == {"KEEPER_ERROR": log.MAX_SCAN_ROWS - 1}
    assert report["nextBefore"] == log.stamp(start + timedelta(milliseconds=2))

    earlier = log_queries.stats(before=report["nextBefore"])
    assert earlier["keeper"] == {
        "markets": {"count": 1, "p50Ms": 7, "p95Ms": 7},
        "prices": {"count": 2, "p50Ms": 7, "p95Ms": 7},
    }
    assert earlier["errors"] == {"KEEPER_ERROR": 3}
    assert "truncated" not in earlier


def test_logs_stats_counts_older_rows_once_when_gap_output_is_capped(home):
    root = home / "logs"
    root.mkdir()
    start = datetime(2026, 11, 24, tzinfo=UTC)
    gap = {"event": "log.dropped", "count": 1, "runId": "process"}
    sample = {
        "event": "keeper.request",
        "operation": "prices",
        "durationMs": 7,
        "code": "KEEPER_ERROR",
    }
    log.file_path("cli", start.date(), root).write_bytes(
        log.encode({**sample, "ts": log.stamp(start)})
        + b"".join(
            log.encode({**gap, "ts": log.stamp(start + timedelta(milliseconds=i))})
            for i in range(1, log_queries.MAX_REPORT_ROWS + 2)
        )
        + log.encode({**sample, "ts": log.stamp(start + timedelta(seconds=1))})
    )

    code, output = run_cli("--json", "logs", "stats")
    assert code == 0
    report = json.loads(output)
    assert report["keeper"] == {"prices": {"count": 1, "p50Ms": 7, "p95Ms": 7}}
    assert report["errors"] == {"KEEPER_ERROR": 1}
    assert len(report["gaps"]) == log_queries.MAX_REPORT_ROWS
    assert report["nextBefore"] == log.stamp(start + timedelta(milliseconds=2))

    code, output = run_cli("--json", "logs", "stats", "--before", report["nextBefore"])
    assert code == 0
    earlier = json.loads(output)
    assert earlier["keeper"] == report["keeper"]
    assert earlier["errors"] == report["errors"]
    assert earlier["gaps"] == [
        {**gap, "ts": log.stamp(start + timedelta(milliseconds=1))}
    ]
    assert "truncated" not in earlier


def test_mcp_log_concurrency_leaves_normal_tools_available(keeper_stub):
    endpoint, state = keeper_stub
    state.intents_block = threading.Event()
    mcp = server.create_server(settings.load({"keeper.endpoint": endpoint}))

    async def run():
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            args = {"key": "KEY", "addr": ADDR}
            running = [
                asyncio.create_task(client.call_tool("logs_intent", args))
                for _ in range(server.LOG_WORKERS)
            ]
            try:
                async with asyncio.timeout(5):
                    while len(state.requests) < server.LOG_WORKERS:
                        await asyncio.sleep(0.01)
                    busy = await client.call_tool("logs_intent", args)
                    assert isinstance(busy.content[0], TextContent)
                    assert "busy" in busy.content[0].text
                    other = await client.call_tool("market_prices", {})
                    assert isinstance(other.content[0], TextContent)
                    assert json.loads(other.content[0].text) == {
                        "oracle": {"tokenPricesUsd": []}
                    }
            finally:
                state.intents_block.set()
                await asyncio.gather(*running)

    asyncio.run(run())


def test_invalid_emitted_type_is_dropped_and_writer_gap_matches_schema(home):
    log.event(
        "keeper.request",
        operation="prices",
        durationMs="12",
        endpoint="https://example.test",
        authenticated=False,
        status="ok",
    )
    sample_request("prices")
    lines = read_logs(home)
    assert [line["event"] for line in lines] == ["keeper.request", "log.dropped"]
    assert all(valid_event(line) for line in lines)
    assert lines[1]["count"] == 1


def test_logs_stats_counts_errors_and_nearest_rank_percentiles(home):
    now = log.now()
    root = home / "logs"
    root.mkdir()
    ts = log.stamp(now)
    recent = [
        {
            "ts": ts,
            "seq": i,
            "event": "keeper.request",
            "operation": "intents",
            "durationMs": i,
        }
        for i in range(1, 21)
    ]
    gap = {"ts": ts, "seq": 22, "event": "log.dropped", "count": 5}
    recent += [
        {
            "ts": ts,
            "seq": 21,
            "event": "keeper.request",
            "operation": "positions",
            "durationMs": 3,
            "code": "KEEPER_ERROR",
        },
        {"ts": ts, "seq": 23, "event": "command.end", "code": "KEEPER_ERROR"},
        gap,
    ]
    log.file_path("cli", now.date(), root).write_bytes(
        b"".join(log.encode(line) for line in recent)
    )
    old = now - timedelta(days=2)
    log.file_path("mcp", old.date(), root).write_bytes(
        log.encode(
            {
                "ts": log.stamp(old),
                "event": "mcp.tool_result",
                "code": "SIGNED_OUT",
            }
        )
    )
    code, output = run_cli("--json", "logs", "stats", "--since", "24h")
    assert code == 0
    report = json.loads(output)
    assert report["keeper"] == {
        "intents": {"count": 20, "p50Ms": 10, "p95Ms": 19},
        "positions": {"count": 1, "p50Ms": 3, "p95Ms": 3},
    }
    assert report["errors"] == {"KEEPER_ERROR": 2}
    assert report["gaps"] == [gap]
    all_retained = json.loads(run_cli("--json", "logs", "stats")[1])
    assert all_retained["errors"] == {"KEEPER_ERROR": 2, "SIGNED_OUT": 1}
    plain = run_cli("logs", "stats", "--since", "24h")[1]
    assert "p95 (ms)" in plain and "KEEPER_ERROR  2" in plain and "count=5" in plain
    code, output = run_cli("--json", "logs", "stats", "--since", "0h")
    assert code == 2 and json.loads(output)["error"]["code"] == "INVALID_REQUEST"


def test_logs_path_prints_the_directory(home):
    assert run_cli("logs", "path") == (0, f"{home / 'logs'}\n")
    code, output = run_cli("--json", "logs", "path")
    assert code == 0
    assert json.loads(output) == {
        "dir": str(home / "logs"),
        "enabled": True,
        "keepDays": 14,
        "files": [],
    }
    assert not (home / "logs").exists()
    log.event("probe")
    today = datetime.now(UTC).date().isoformat()
    files = json.loads(run_cli("--json", "logs", "path")[1])["files"]
    assert files == [f"cli-{today}.jsonl"]


def test_logs_tail_filters_by_count_surface_and_event(home):
    old_file(home / "logs", "cli", 1).write_text(
        '{"ts":"2020-01-01T00:00:00.000Z","level":"info","event":"old.event",'
        '"surface":"cli","pid":1,"version":"0","runId":"r"}\nnot json\n'
    )
    log.event("command.start", command="market prices", argv=["market", "prices"])
    with log.bind(surface="mcp", tool="market_prices", callId="test"):
        log.event("mcp.tool_call", arguments={"addr": ADDR})
        sample_request("prices", "warn", status="not_sent")
    storage.pending("KEY1", "https://example.test", ADDR, "CLOSE_POSITION", "7")
    storage.set_status("KEY1", "https://example.test", ADDR, "accepted")
    before = raw_logs(home)

    def names(*args: str) -> list[str]:
        code, output = run_cli("--json", "logs", "tail", *args)
        assert code == 0
        return [json.loads(line)["event"] for line in output.splitlines()]

    assert names() == [
        "old.event",
        "command.start",
        "mcp.tool_call",
        "keeper.request",
        "intent.reserved",
        "intent.status",
    ]
    assert names("-n", "2") == ["intent.reserved", "intent.status"]
    assert names("--surface", "mcp") == ["mcp.tool_call", "keeper.request"]
    assert names("--event", "intent") == ["intent.reserved", "intent.status"]
    assert names("--event", "intent.status") == ["intent.status"]
    assert names("--event", "int") == []
    assert names("--surface", "cli", "--event", "keeper") == []
    assert names("--surface", "update") == []
    assert run_cli("logs", "tail", "-n", "0")[0] == 2
    assert run_cli("logs", "tail", "--surface", "web")[0] == 2
    # Reading the log adds nothing to it.
    assert raw_logs(home) == before


def test_logs_tail_reads_from_the_end_of_large_files(home):
    """Tail finds the newest events by reading backwards in chunks, so
    lines split across chunks, bad lines, and a missing final newline all
    come out right."""
    root = home / "logs"
    root.mkdir(mode=0o700)
    today = datetime.now(UTC).date()

    def line(surface: str, index: int) -> str:
        value = {
            "ts": f"2026-01-01T00:00:{index // 1000:02d}.{index % 1000:03d}Z",
            "event": "even" if index % 2 == 0 else "odd",
            "surface": surface,
            "seq": index,
            "pad": "p" * (index % 97),
        }
        return json.dumps(value)

    cli = [line("cli", index) for index in range(0, 3000, 3)]
    mcp = [line("mcp", index) for index in range(1, 3000, 3)]
    cli.insert(500, "not json")
    cli.insert(900, "\xff\xfe broken")
    log.file_path("cli", today, root).write_text("\n".join(cli) + "\n", "latin-1")
    log.file_path("mcp", today, root).write_text("\n".join(mcp))
    for chunk in (7, 64, 4096, log.TAIL_CHUNK):
        found = log.tail(5, chunk=chunk)
        assert [item["seq"] for item in found] == [2992, 2994, 2995, 2997, 2998]
        odd = log.tail(3, "mcp", "odd", chunk=chunk)
        assert [item["seq"] for item in odd] == [2983, 2989, 2995]
        everything = log.tail(10_000, chunk=chunk)
        assert [item["seq"] for item in everything] == sorted(
            [*range(0, 3000, 3), *range(1, 3000, 3)]
        )
    assert log.tail(0) == []


def test_logs_tail_orders_by_time_before_it_cuts(home):
    """An older event appended after a newer one, by another process or a
    slow writer, must not push the newer one out of `tail -n 1`."""
    root = home / "logs"
    root.mkdir(mode=0o700)
    today = datetime.now(UTC).date()

    def row(stamp: str, name: str, seq: int = 1) -> str:
        return json.dumps({"ts": stamp, "event": name, "seq": seq}) + "\n"

    log.file_path("cli", today, root).write_text(
        row("2026-01-01T00:00:01.000Z", "first")
        + row("2026-01-01T00:00:09.000Z", "newest")
        + row("2026-01-01T00:00:05.000Z", "late.append")
    )
    log.file_path("mcp", today, root).write_text(
        row("2026-01-01T00:00:08.000Z", "mcp.newest")
        + row("2026-01-01T00:00:02.000Z", "mcp.late.append")
    )
    assert [line["event"] for line in log.tail(1)] == ["newest"]
    assert [line["event"] for line in log.tail(2)] == ["mcp.newest", "newest"]
    assert [line["event"] for line in log.tail(1, "mcp")] == ["mcp.newest"]
    assert [line["event"] for line in log.tail(5)] == [
        "first",
        "mcp.late.append",
        "late.append",
        "mcp.newest",
        "newest",
    ]
    assert run_cli("--json", "logs", "tail", "-n", "1")[1].count("newest") == 1


def test_a_line_goes_in_the_file_for_its_own_day(home, monkeypatch):
    """An event made just before midnight and written just after belongs to
    the day it happened."""
    root = home / "logs"
    before = datetime(2026, 3, 1, 23, 59, 59, 900_000, tzinfo=UTC)
    after = datetime(2026, 3, 2, 0, 0, 0, 100_000, tzinfo=UTC)
    monkeypatch.setattr(log, "now", lambda: before)
    with log._io_lock:
        log.event("before.midnight")
        monkeypatch.setattr(log, "now", lambda: after)
    log.event("after.midnight")
    assert log.flush(60)
    first = log.file_path("cli", before.date(), root).read_text()
    second = log.file_path("cli", after.date(), root).read_text()
    assert "before.midnight" in first and "after.midnight" not in first
    assert "after.midnight" in second and "before.midnight" not in second
    assert [line["event"] for line in log.tail(2)] == [
        "before.midnight",
        "after.midnight",
    ]


def test_logs_tail_reads_only_the_end_of_a_file(home, monkeypatch):
    root = home / "logs"
    root.mkdir(mode=0o700)
    path = log.file_path("cli", datetime.now(UTC).date(), root)
    rows = [
        json.dumps({"ts": f"{i:08d}", "event": "row", "seq": i}) for i in range(50_000)
    ]
    path.write_text("\n".join(rows) + "\n")
    read = 0
    original = os.read

    def counting(fd: int, size: int) -> bytes:
        nonlocal read
        data = original(fd, size)
        read += len(data)
        return data

    monkeypatch.setattr(log.os, "read", counting)
    assert [item["seq"] for item in log.tail(2)] == [49_998, 49_999]
    assert read <= log.TAIL_CHUNK < path.stat().st_size


def test_logs_tail_plain_output_is_one_line_per_event(home):
    with log.bind(surface="mcp", tool="market_prices", callId="test"):
        log.event("mcp.tool_call", arguments={"addr": ADDR})
    sample_request("prices", "warn", duration=7, status="not_sent")
    code, output = run_cli("logs", "tail")
    assert code == 0
    first, second = output.splitlines()
    stamp, surface, level, name, *fields = first.split(" ")
    assert datetime.fromisoformat(stamp) and (surface, level) == ("mcp", "info")
    assert name == "mcp.tool_call"
    assert fields == [
        "tool=market_prices",
        "callId=test",
        f'arguments={{"addr":"{ADDR}"}}',
    ]
    assert second.split(" ")[1:] == [
        "cli",
        "warn",
        "keeper.request",
        "operation=prices",
        "durationMs=7",
        "endpoint=https://example.test",
        "authenticated=false",
        "status=not_sent",
    ]


# sai doctor


@posix_only
def test_doctor_makes_the_logs_directory_and_files_private(home):
    log.event("probe")
    assert log.flush(60)
    root = home / "logs"
    path = log.file_path("cli", datetime.now(UTC).date(), root)
    root.chmod(0o755)
    path.chmod(0o644)
    report = doctor.check_logs(doctor.context(settings.load({}), "", fix=False))
    assert report.status == "warn" and f"chmod go-rwx {path}" in (report.fix or "")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    fixed = doctor.check_logs(doctor.context(settings.load({}), "", fix=True))
    assert fixed.status == "fixed" and len(fixed.changes) == 2
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = doctor.check_logs(doctor.context(settings.load({}), "", fix=True))
    assert again.status == "pass" and again.details["files"] == 1


@posix_only
def test_doctor_does_not_follow_a_symlinked_logs_directory(home):
    elsewhere = home / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    (home / "logs").symlink_to(elsewhere)
    check = doctor.check_logs(doctor.context(settings.load({}), "", fix=True))
    assert check.status == "fail" and "symlink" in check.summary
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755


@posix_only
def test_doctor_fails_on_an_entry_the_writer_refuses(home):
    """A stale symlink or a FIFO at a daily filename blocks that surface's
    lines, so doctor must not report a pass."""
    root = home / "logs"
    root.mkdir(mode=0o700)
    today = datetime.now(UTC).date()
    stale = log.file_path("cli", today, root)
    stale.symlink_to(home / "missing-target")
    context = doctor.context(settings.load({}), "", fix=True)
    check = doctor.check_logs(context)
    assert check.status == "fail" and "a symlink" in check.summary
    assert str(stale) in (check.fix or "") and check.details["refused"] == [str(stale)]
    stale.unlink()
    fifo = log.file_path("mcp", today, root)
    os.mkfifo(fifo)
    outside = home / "outside.jsonl"
    outside.write_text("")
    linked = log.file_path("update", today, root)
    os.link(outside, linked)
    check = doctor.check_logs(context)
    assert check.status == "fail"
    assert "not a regular file" in check.summary and "hard-linked" in check.summary
    assert check.details["refused"] == [str(fifo), str(linked)]
    fifo.unlink()
    linked.unlink()
    (root / "notes.txt").symlink_to(outside)  # Not sai's name: not sai's business.
    assert doctor.check_logs(context).status == "pass"


@posix_only
def test_a_file_name_with_a_trailing_newline_is_not_a_log_file(home):
    root = home / "logs"
    root.mkdir(mode=0o700)
    old = datetime.now(UTC).date() - timedelta(days=400)
    odd = root / f"cli-{old.isoformat()}.jsonl\n"
    odd.write_text("keep me")
    expired = old_file(root, "cli", 400)
    assert log.parse_name(odd.name) is None
    assert log.parse_name(expired.name) == (old, "cli")
    log.event("first")
    assert log.flush(60)
    assert odd.read_text() == "keep me" and not expired.exists()


def test_doctor_fails_when_a_file_is_in_the_way_of_the_logs_directory(home):
    (home / "logs").write_text("in the way")
    check = doctor.check_logs(doctor.context(settings.load({}), "", fix=True))
    assert check.status == "fail"
    assert "is not a directory" in check.summary and "Move or remove" in (
        check.fix or ""
    )
    assert (home / "logs").read_text() == "in the way"


def test_doctor_reports_no_logs_yet_and_logging_off(home, monkeypatch):
    context = doctor.context(settings.load({}), "", fix=True)
    check = doctor.check_logs(context)
    assert check.status == "pass" and check.summary == f"No logs yet at {home / 'logs'}"
    assert not (home / "logs").exists()
    monkeypatch.setenv("SAI_LOG", "0")
    assert "logging is off" in doctor.check_logs(context).summary
