"""`sai config` and how CLI commands pick up settings."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import tomllib
from typing import Any

import pytest
from click.testing import CliRunner, Result
from sai_cli.core import margin, settings
from sai_cli.sai_cli import forwarded_flags, main

SECRET_URL = "https://user:pw@k.example/query?key=s"
DEAD = "http://127.0.0.1:9/query"


@pytest.fixture(autouse=True)
def scrubbed_env(tmp_path, monkeypatch):
    for spec in settings.SETTINGS:
        monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.setenv("SAI_HOME", str(tmp_path))


def sai(*args: str) -> Result:
    return CliRunner().invoke(main, list(args))


def listed(*flags: str) -> dict[str, Any]:
    result = sai(*flags, "--json", "config", "list")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_list_json_shape_with_defaults(tmp_path):
    data = listed()
    assert data["path"] == str(tmp_path / "config.toml")
    assert list(data["settings"]) == list(settings.KEYS)
    assert data["settings"]["keeper.endpoint"] == {
        "value": "https://sai-keeper.nibiru.fi",
        "source": "default",
        "env": "SAI_ENDPOINT",
        "default": "https://sai-keeper.nibiru.fi",
    }
    assert data["settings"]["keeper.ws_endpoint"] == {
        "value": "wss://sai-keeper.nibiru.fi",
        "source": "derived",
        "env": "SAI_WS_ENDPOINT",
        "default": None,
    }
    assert data["settings"]["lcd.endpoint"]["value"] == margin.MAINNET_LCD
    assert data["settings"]["update.auto"]["value"] is True
    # Reading never creates the file.
    assert not (tmp_path / "config.toml").exists()


def test_set_get_unset(tmp_path):
    path = tmp_path / "config.toml"
    result = sai("--json", "config", "set", "keeper.endpoint", SECRET_URL)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "key": "keeper.endpoint",
        "value": SECRET_URL,
        "path": str(path),
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert tomllib.loads(path.read_text())["keeper"] == {"endpoint": SECRET_URL}

    entry = listed()["settings"]["keeper.endpoint"]
    assert entry["value"] == "https://k.example"
    assert entry["source"] == "file"
    assert listed()["settings"]["keeper.ws_endpoint"] == {
        "value": "wss://k.example",
        "source": "derived",
        "env": "SAI_WS_ENDPOINT",
        "default": None,
    }
    # `get` prints the exact value; `list` hides the credentials.
    assert sai("config", "get", "keeper.endpoint").stdout == SECRET_URL + "\n"
    got = json.loads(sai("--json", "config", "get", "keeper.endpoint").stdout)
    assert got == {"key": "keeper.endpoint", "value": SECRET_URL, "source": "file"}
    assert "pw" not in sai("config", "list").stdout

    result = sai("--json", "config", "unset", "keeper.endpoint")
    assert json.loads(result.stdout) == {
        "key": "keeper.endpoint",
        "removed": True,
        "path": str(path),
    }
    assert sai("config", "unset", "keeper.endpoint").stdout.startswith(
        "Already unset: keeper.endpoint"
    )
    assert path.read_text() == settings.template()


def test_bool_setting_text_output():
    assert sai("config", "set", "update.auto", "false").exit_code == 0
    assert sai("config", "get", "update.auto").stdout == "false\n"
    text = sai("config", "list").stdout
    assert "update.auto = false  # file, SAI_UPDATE_AUTO" in text


def test_list_shows_only_keeper_origins():
    path_key, query_key = "fixture-path-key", "fixture-query-key"
    endpoint = f"https://[::1]:9443/{path_key}/query?key={query_key}"
    assert sai("config", "set", "keeper.endpoint", endpoint).exit_code == 0

    text = sai("config", "list")
    structured = sai("--json", "config", "list")
    for result in (text, structured):
        assert result.exit_code == 0
        assert path_key not in result.output and query_key not in result.output

    assert 'keeper.endpoint = "https://[::1]:9443"' in text.stdout
    assert 'keeper.ws_endpoint = "wss://[::1]:9443"' in text.stdout
    shown = json.loads(structured.stdout)["settings"]
    assert shown["keeper.endpoint"]["value"] == "https://[::1]:9443"
    assert shown["keeper.ws_endpoint"]["value"] == "wss://[::1]:9443"
    assert shown["keeper.ws_endpoint"]["source"] == "derived"


@pytest.mark.parametrize(
    "args",
    [
        ("config", "get", "keeper.url"),
        ("config", "set", "keeper.url", "https://k.example"),
        ("config", "unset", "keeper.url"),
        ("config", "set", "update.auto", "maybe"),
        ("config", "set", "lcd.endpoint", "lcd.example"),
    ],
)
def test_unknown_key_or_bad_value_exits_2_listing_keys(args, tmp_path):
    result = sai(*args)
    assert result.exit_code == 2
    assert "Valid keys: keeper.endpoint, keeper.ws_endpoint" in result.stderr
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize("authority", ["[::1]suffix", "prefix[::1]", "[::1"])
def test_config_set_rejects_malformed_ipv6_authority(tmp_path, authority):
    result = sai(
        "--json", "config", "set", "keeper.endpoint", f"https://{authority}/path"
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": (
                "Invalid value for keeper.endpoint: expected a URL starting with "
                f"https:// or http://. Valid keys: {', '.join(settings.KEYS)}"
            ),
            "retryable": False,
        },
    }
    assert result.stderr == ""
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / ".config.lock").exists()


def test_config_set_accepts_ipv6_with_port():
    endpoint = "https://[::1]:8443/path"
    result = sai("--json", "config", "set", "keeper.endpoint", endpoint)
    assert result.exit_code == 0
    assert json.loads(result.stdout)["value"] == endpoint


@pytest.mark.parametrize(
    ("key", "scheme"), [("keeper.endpoint", "https"), ("keeper.ws_endpoint", "wss")]
)
@pytest.mark.parametrize("fragment", ["#client", "#"])
def test_config_set_rejects_keeper_endpoint_fragments(tmp_path, key, scheme, fragment):
    result = sai(
        "--json", "config", "set", key, f"{scheme}://keeper.example/graphql{fragment}"
    )

    assert result.exit_code == 2
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert key in error["message"] and "fragment" in error["message"]
    assert result.stderr == ""
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / ".config.lock").exists()


def test_watch_rejects_a_keeper_fragment_in_config(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[keeper]\nendpoint = "http://127.0.0.1:9/graphql#client"\n'
    )
    result = subprocess.run(
        [sys.executable, "-m", "sai_cli", "--json", "watch", "price", "1"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 2
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert "keeper.endpoint" in error["message"] and "fragment" in error["message"]
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("key", "endpoint"),
    [
        ("keeper.ws_endpoint", "wss://keeper.example/graphql stream"),
        ("keeper.ws_endpoint", "wss://keeper.example/graphql?name=one two"),
        ("keeper.endpoint", " https://keeper.example/graphql"),
        ("keeper.endpoint", "https://keeper.example/graphql "),
        ("keeper.endpoint", "\xa0https://[::1]:8443/path\xa0"),
        ("lcd.endpoint", "https://lcd.example/path\x80"),
    ],
)
def test_config_set_rejects_raw_whitespace_and_controls_in_urls(
    tmp_path, key, endpoint
):
    result = sai("--json", "config", "set", key, endpoint)

    assert result.exit_code == 2
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert key in error["message"]
    assert result.stderr == ""
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / ".config.lock").exists()


def test_config_set_accepts_percent_encoded_websocket_request_target():
    endpoint = "wss://keeper.example/graphql%20stream?name=one%20two"
    result = sai("--json", "config", "set", "keeper.ws_endpoint", endpoint)
    assert result.exit_code == 0
    assert json.loads(result.stdout)["value"] == endpoint


@pytest.mark.parametrize(
    ("key", "endpoint"),
    [
        ("keeper.endpoint", "https://127.0.0.999/query"),
        ("keeper.endpoint", "https://keeper.example/que\try"),
        ("keeper.endpoint", "https://keeper.example/query\t"),
        ("lcd.endpoint", "https://127.0.0.999/query"),
        ("keeper.ws_endpoint", "wss://keeper.example/query#fragment"),
    ],
)
def test_config_set_rejects_endpoints_the_transport_cannot_parse(
    tmp_path, key, endpoint
):
    result = sai("--json", "config", "set", key, endpoint)

    assert result.exit_code == 2
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert key in error["message"]
    assert endpoint not in error["message"]
    assert result.stderr == ""
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / ".config.lock").exists()


@pytest.mark.parametrize(
    "endpoint", ["https://127.0.0.999/query", "https://127.0.0.1:9/que\try"]
)
def test_market_list_rejects_an_unparseable_endpoint_from_config(tmp_path, endpoint):
    path = tmp_path / "config.toml"
    path.write_text(f"[keeper]\nendpoint = {json.dumps(endpoint)}\n")

    result = sai("--json", "market", "list")

    assert result.exit_code == 2
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert "keeper.endpoint" in error["message"]
    assert result.stderr == ""


@pytest.mark.parametrize("source", ["set", "flag", "env", "file"])
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://bad host/path",
        "https://bad\thost/path",
        "https://user:password@bad host/path",
        "https://bad\\host/path",
        "https:\t//bad host/path",
        "https:/\t/host/path",
    ],
)
def test_rejects_malformed_endpoint_authority(tmp_path, monkeypatch, source, endpoint):
    path = tmp_path / "config.toml"
    if source == "set":
        args = ["config", "set", "keeper.endpoint", endpoint]
    else:
        args = ["config", "get", "keeper.endpoint"]
        if source == "flag":
            args[:0] = ["--endpoint", endpoint]
        elif source == "env":
            monkeypatch.setenv("SAI_ENDPOINT", endpoint)
        else:
            path.write_text(f"[keeper]\nendpoint = {json.dumps(endpoint)}\n")

    result = sai("--json", *args)

    assert result.exit_code == 2
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert "keeper.endpoint" in error["message"]
    assert endpoint not in error["message"]
    assert "password" not in error["message"]
    assert result.stderr == ""
    assert not (tmp_path / ".config.lock").exists()
    assert path.exists() == (source == "file")


def test_flag_and_env_override_the_file(monkeypatch):
    sai("config", "set", "keeper.endpoint", "https://file.example/q")
    sai("config", "set", "keeper.ws_endpoint", "wss://ws.file.example/q")
    data = listed("--endpoint", "https://flag.example/q")["settings"]
    assert data["keeper.endpoint"]["source"] == "flag"
    # The file's WebSocket belongs to the file's Keeper, so it is derived.
    assert data["keeper.ws_endpoint"]["value"] == "wss://flag.example"
    assert data["keeper.ws_endpoint"]["source"] == "derived"
    assert listed()["settings"]["keeper.ws_endpoint"]["source"] == "file"

    monkeypatch.setenv("SAI_ENDPOINT", "https://env.example/q")
    entry = listed()["settings"]["keeper.endpoint"]
    assert (entry["value"], entry["source"]) == ("https://env.example", "env")
    result = sai("config", "set", "keeper.endpoint", "https://file2.example/q")
    assert "SAI_ENDPOINT is set and overrides keeper.endpoint" in result.stderr


@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("source", ["flag", "env", "file"])
@pytest.mark.parametrize(
    "endpoint", ["https://[::1]:oops/path", "https://[::1/path", "keeper.example"]
)
def test_malformed_endpoint_during_resolution_is_a_usage_error(
    tmp_path, monkeypatch, json_output, source, endpoint
):
    args = ["--json"] if json_output else []
    if source == "flag":
        args.extend(("--endpoint", endpoint))
        origin = "--endpoint"
    elif source == "env":
        monkeypatch.setenv("SAI_ENDPOINT", endpoint)
        origin = "SAI_ENDPOINT"
    else:
        path = tmp_path / "config.toml"
        path.write_text(f"[keeper]\nendpoint = {json.dumps(endpoint)}\n")
        origin = str(path)

    result = sai(*args, "config", "get", "keeper.endpoint")

    assert result.exit_code == 2
    message = (
        f"{origin}: Invalid value for keeper.endpoint:"
        " expected a URL starting with https:// or http://"
    )
    if json_output:
        assert json.loads(result.stdout) == {
            "error": {"code": "INVALID_REQUEST", "message": message, "retryable": False}
        }
        assert result.stderr == ""
    else:
        assert result.stdout == ""
        assert message in result.stderr
        assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("args", "key"),
    [
        (("mcp", "serve", "--endpoint", "https://[::1]:oops/path"), "keeper.endpoint"),
        (
            ("watch", "price", "1", "--ws-endpoint", "wss://[::1]:oops/path"),
            "keeper.ws_endpoint",
        ),
        (
            (
                "--endpoint",
                "https://[::1]:oops/path",
                "logs",
                "intent",
                "key",
                "--offline",
            ),
            "keeper.endpoint",
        ),
    ],
)
def test_malformed_command_endpoint_is_a_usage_error(args, key):
    result = sai("--json", *args)
    assert result.exit_code == 2
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert key in error["message"]
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("args", "key"),
    [
        (("watch", "price", "1", "--ws-endpoint", ""), "keeper.ws_endpoint"),
        (
            (
                "trade",
                "partial-close",
                "7",
                "--percent",
                "10",
                "--dry-run",
                "--lcd-endpoint",
                "",
            ),
            "lcd.endpoint",
        ),
        (
            ("doctor", "--offline", "--no-fix", "--ws-endpoint", ""),
            "keeper.ws_endpoint",
        ),
        (("doctor", "--offline", "--no-fix", "--lcd-endpoint", ""), "lcd.endpoint"),
    ],
)
def test_explicitly_empty_subcommand_endpoint_is_a_usage_error(monkeypatch, args, key):
    # A fallback to the configured WebSocket is refused before it connects.
    monkeypatch.setenv("SAI_WS_ENDPOINT", "ws://keeper.example/query")
    result = sai("--json", *args)

    assert result.exit_code == 2
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert key in error["message"]
    assert result.stderr == ""


def test_commands_use_the_configured_endpoint():
    """A Keeper endpoint set in the file is the one queries go to."""
    sai("config", "set", "keeper.endpoint", DEAD)
    result = sai("market", "prices")
    assert result.exit_code == 4
    assert "Could not connect to Keeper" in result.stderr


def test_malformed_file_fails_commands_but_can_be_fixed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[update]\nauto = "sometimes"  # oops\n')
    result = sai("market", "prices")
    assert result.exit_code == 1
    assert f"{path}: Invalid value for update.auto" in result.stderr
    assert sai("config", "unset", "update.auto").exit_code == 0
    assert listed()["settings"]["update.auto"]["source"] == "default"

    path.write_text("[keeper\n")
    for args in (("config", "list"), ("config", "set", "update.auto", "true")):
        result = sai(*args)
        assert result.exit_code == 1
        assert f"Cannot parse {path}" in result.stderr


@pytest.mark.parametrize(
    "original",
    [
        'keeper = { endpoint = "https://old.example/q" }  # staging\n',
        'keeper.endpoint = "https://old.example/q"  # staging\n',
        "[keeper.sub]  # mine\nx = 1\n",
    ],
    ids=["inline", "dotted", "subtable-only"],
)
def test_set_and_unset_refuse_non_standard_sections(tmp_path, original):
    path = tmp_path / "config.toml"
    path.write_bytes(original.encode())
    for args in (
        ("config", "set", "keeper.endpoint", "https://new.example/q"),
        ("config", "unset", "keeper.endpoint"),
        ("config", "unset", "keeper.ws_endpoint"),
    ):
        result = sai(*args)
        assert result.exit_code == 2, result.output
        assert f"keeper in {path} is an inline table" in result.stderr
        assert "rewrite it as a [keeper] table" in result.stderr
        assert path.read_bytes() == original.encode()


def test_unset_of_an_unset_key_never_writes(tmp_path):
    path = tmp_path / "config.toml"
    assert sai("config", "unset", "keeper.endpoint").exit_code == 0
    assert not path.exists()
    original = "[keeper]  # hand-written\n# my note\n"
    path.write_text(original)
    path.chmod(0o644)
    before = path.stat().st_mtime_ns
    result = sai("config", "unset", "keeper.endpoint")
    assert result.stdout.startswith("Already unset: keeper.endpoint")
    assert path.read_text() == original
    assert path.stat().st_mtime_ns == before


def test_unknown_file_keys_warn(tmp_path):
    (tmp_path / "config.toml").write_text("[keeper]\ntimeout = 3\n")
    result = sai("config", "list")
    assert result.exit_code == 0
    assert "ignoring unknown setting 'keeper.timeout'" in result.stderr


def test_per_command_flags_win(tmp_path):
    sai("config", "set", "lcd.endpoint", "http://127.0.0.1:9")
    sai("config", "set", "keeper.endpoint", DEAD)
    result = sai(
        "--json",
        "account",
        "balances",
        "0x" + "b" * 40,
        "--ws-endpoint",
        "ws://keeper.example/q",
    )
    # Plain ws:// off loopback is refused before any connection, which shows
    # the command used its own flag rather than the derived ws://127.0.0.1.
    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "STREAM_ERROR"
    assert "WebSocket endpoint must use WSS" in error["message"]


def test_repl_forwards_only_flags_the_person_passed():
    obj: dict[str, Any] = {
        "flags": {
            "keeper.endpoint": None,
            "keeper.ws_endpoint": "wss://w.example/q",
            "lcd.endpoint": None,
        },
        "json": True,
    }
    assert forwarded_flags(obj) == ["--ws-endpoint", "wss://w.example/q", "--json"]
    obj["flags"] = {}
    obj["json"] = False
    assert forwarded_flags(obj) == []
