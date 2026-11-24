"""Verify public error output using isolated Sai homes and loopback."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner, Result
from sai_cli.core import keeper, storage
from sai_cli.sai_cli import main

ADDR = "0x" + "a" * 40
OVERRIDE = "http://127.0.0.1:9/query"
PRIVATE_PATH = "fixture-endpoint-key"
PRIVATE_OVERRIDE = f"http://127.0.0.1:9/{PRIVATE_PATH}/query"


def assert_error(result: Result, exit_code: int, code: str) -> dict:
    assert result.exit_code == exit_code, result.output
    assert len(result.stdout.splitlines()) == 1
    envelope = json.loads(result.stdout)
    assert set(envelope) == {"error"}
    error = envelope["error"]
    assert {"code", "message", "retryable"} <= set(error)
    assert set(error) <= {
        "code",
        "message",
        "retryable",
        "hint",
        "intentKey",
        "details",
    }
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert error["retryable"] is (exit_code == 4)
    return error


@pytest.mark.parametrize(
    ("args", "exit_code", "code"),
    [
        (("--unknown-option",), 2, "INVALID_REQUEST"),
        (("logs", "tail", "--count", "bad"), 2, "INVALID_REQUEST"),
        (("config", "set", "update.auto", "bad"), 2, "INVALID_REQUEST"),
        (("skill", "show", "--page", "missing"), 1, "INVALID_REQUEST"),
        (("mcp", "serve", "--endpoint", "ftp://invalid"), 2, "INVALID_REQUEST"),
        (
            ("watch", "price", "1", "--ws-endpoint", "http://invalid"),
            2,
            "INVALID_REQUEST",
        ),
        (("trade", "close", "-1"), 2, "INVALID_REQUEST"),
    ],
)
def test_command_failures_use_one_json_envelope(args, exit_code, code):
    result = CliRunner().invoke(main, ["--json", *args])
    assert_error(result, exit_code, code)
    assert result.stderr == ""


def test_root_json_flag_is_detected_when_earlier_option_fails():
    result = CliRunner().invoke(main, ["--bad-option", "--json", "market", "list"])
    assert_error(result, 2, "INVALID_REQUEST")
    assert result.stderr == ""


def test_no_json_command_does_not_open_the_repl():
    result = CliRunner().invoke(main, ["--json"])
    assert_error(result, 2, "INVALID_REQUEST")


@pytest.mark.parametrize("args", [("auth", "status"), ("trade", "close", "7")])
@pytest.mark.parametrize("endpoint", [OVERRIDE, PRIVATE_OVERRIDE])
def test_explicit_account_sign_in_hint_pins_the_selected_keeper(
    monkeypatch, args, endpoint
):
    monkeypatch.setenv("SAI_ENDPOINT", "http://127.0.0.1:1/query")
    result = CliRunner().invoke(
        main, ["--json", "--endpoint", endpoint, "--as", ADDR, *args]
    )
    error = assert_error(result, 6, "SIGNED_OUT")
    assert f"{ADDR} is not connected" in error["message"]
    if endpoint == OVERRIDE:
        assert (
            error["hint"] == f"sai --endpoint {endpoint} auth login NAME --addr {ADDR}"
        )
    else:
        assert error["hint"].startswith('sai --endpoint "${SAI_RECONCILE_ENDPOINT:?')
        assert error["hint"].endswith(f"auth login NAME --addr {ADDR}")
        assert PRIVATE_PATH not in result.output
        assert endpoint not in result.output


def test_click_usage_remains_on_stderr_in_text_mode():
    result = CliRunner().invoke(main, ["market", "missing"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Usage:" in result.stderr and "No such command" in result.stderr


@pytest.mark.parametrize(
    "args",
    [("market", "list"), ("account", "history", ADDR), ("intent", "status", ADDR)],
)
def test_keeper_connection_refusal_is_safe_to_retry(args):
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{unavailable.getsockname()[1]}/query"
        result = CliRunner().invoke(main, ["--json", "--endpoint", endpoint, *args])
    assert_error(result, 4, "NOT_SENT")
    assert result.stderr == ""


def test_success_and_sign_in_exit_codes():
    runner = CliRunner()
    args = ["--json", "--endpoint", keeper.MAINNET_HTTP]
    signed_out = runner.invoke(main, [*args, "trade", "close", "7"])
    hint = f"sai --endpoint {keeper.MAINNET_HTTP} auth login NAME"
    assert assert_error(signed_out, 6, "SIGNED_OUT")["hint"] == hint
    listed = runner.invoke(main, [*args, "auth", "list"])
    assert listed.exit_code == 0 and json.loads(listed.stdout) == {"accounts": []}
    storage.save_session(
        {
            "token": "other-session-token",
            "evmAddress": "0x" + "b" * 40,
            "endpoint": keeper.MAINNET_HTTP,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
    )
    storage.save_session(
        {
            "token": "test-session-token",
            "evmAddress": ADDR,
            "endpoint": keeper.MAINNET_HTTP,
            "expiresAt": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        }
    )
    expired = runner.invoke(main, [*args, "auth", "status"])
    assert (
        assert_error(expired, 6, "SESSION_EXPIRED")["hint"] == f"{hint} --addr {ADDR}"
    )
    retry_trade = runner.invoke(main, [*args, "trade", "close", "7"])
    assert (
        assert_error(retry_trade, 6, "SESSION_EXPIRED")["hint"]
        == f"{hint} --addr {ADDR}"
    )
    text = runner.invoke(main, ["--endpoint", keeper.MAINNET_HTTP, "auth", "status"])
    assert text.exit_code == 6 and text.stdout == ""
    assert "Trading session expired" in text.stderr
