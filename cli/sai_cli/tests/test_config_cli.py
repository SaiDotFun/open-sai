"""`sai config` and how CLI commands pick up settings."""

from __future__ import annotations

import json
import stat
import tomllib
from typing import Any

import pytest
from click.testing import CliRunner, Result
from sai_cli.core import keeper, margin, settings
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
        "value": keeper.MAINNET_HTTP,
        "source": "default",
        "env": "SAI_ENDPOINT",
        "default": keeper.MAINNET_HTTP,
    }
    assert data["settings"]["keeper.ws_endpoint"] == {
        "value": keeper.MAINNET_WS,
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
    assert entry["value"] == "https://k.example/query"
    assert entry["source"] == "file"
    assert listed()["settings"]["keeper.ws_endpoint"] == {
        "value": "wss://k.example/query",
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


def test_flag_and_env_override_the_file(monkeypatch):
    sai("config", "set", "keeper.endpoint", "https://file.example/q")
    sai("config", "set", "keeper.ws_endpoint", "wss://ws.file.example/q")
    data = listed("--endpoint", "https://flag.example/q")["settings"]
    assert data["keeper.endpoint"]["source"] == "flag"
    # The file's WebSocket belongs to the file's Keeper, so it is derived.
    assert data["keeper.ws_endpoint"]["value"] == "wss://flag.example/q"
    assert data["keeper.ws_endpoint"]["source"] == "derived"
    assert listed()["settings"]["keeper.ws_endpoint"]["source"] == "file"

    monkeypatch.setenv("SAI_ENDPOINT", "https://env.example/q")
    entry = listed()["settings"]["keeper.endpoint"]
    assert (entry["value"], entry["source"]) == ("https://env.example/q", "env")
    result = sai("config", "set", "keeper.endpoint", "https://file2.example/q")
    assert "SAI_ENDPOINT is set and overrides keeper.endpoint" in result.stderr


def test_bad_flag_value_is_an_error():
    result = sai("--endpoint", "keeper.example", "config", "list")
    assert result.exit_code == 1
    assert "--endpoint: Invalid value for keeper.endpoint" in result.stderr


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
