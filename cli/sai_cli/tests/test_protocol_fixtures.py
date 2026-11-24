"""Replay public snapshots and synthetic Keeper contracts without a network."""

from __future__ import annotations

import copy
import json
import re
import socket
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner
from sai_cli.core import intents, keeper, margin, storage
from sai_cli.sai_cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_external_tcp_guard():
    with (
        socket.socket() as client,
        pytest.raises(AssertionError, match="External TCP connection forbidden"),
    ):
        client.connect(("198.51.100.1", 443))


@pytest.fixture
def synthetic() -> dict:
    return fixture("synthetic_protocol.json")


@pytest.mark.parametrize(
    ("args", "file", "field"),
    [
        (["market", "list"], "mainnet_markets_http.json", "perp"),
        (["market", "prices"], "mainnet_prices_http.json", "oracle"),
    ],
)
def test_cli_replays_observed_mainnet_http(monkeypatch, args, file, field):
    golden = fixture(file)
    calls = []

    def post(url, *, json, headers, timeout):
        calls.append(json["query"])
        return httpx.Response(200, json=golden, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    result = CliRunner().invoke(main, ["--json", *args])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == golden["data"]
    assert len(calls) == 1
    assert field in json.loads(result.output)


def test_cli_replays_synthetic_positions(monkeypatch, synthetic):
    golden = synthetic["account_positions_response"]

    def post(url, *, json, headers, timeout):
        assert json["variables"] == {"trader": synthetic["address"]}
        assert "isOpen:true" in json["query"]
        return httpx.Response(200, json=golden, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    result = CliRunner().invoke(
        main, ["--json", "account", "positions", synthetic["address"]]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == golden["data"]


def test_cli_replays_synthetic_intent_status(monkeypatch, synthetic):
    golden = synthetic["intent_status_response"]

    def post(url, *, json, headers, timeout):
        assert json["variables"] == {"user": synthetic["address"], "key": None}
        return httpx.Response(200, json=golden, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    result = CliRunner().invoke(
        main, ["--json", "intent", "status", synthetic["address"]]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == golden["data"]


def test_synthetic_trading_request_goldens(synthetic):
    expected = synthetic["intents"]
    position = synthetic["account_positions_response"]["data"]["perp"]["trades"][0]
    assert (
        intents.open_intent(3, "long", "limit", "20", "2", "100", "1", "110", None)
        == expected["open_limit"]
    )
    assert intents.close_intent(7) == expected["close"]
    assert intents.close_intent(7, cancel=True) == expected["cancel"]
    assert intents.partial_intent(position, 50, Decimal(1)) == expected["partial_close"]
    assert intents.tpsl_intent(7, "110", None, False, True) == expected["tpsl"]
    assert intents.leverage_intent(7, "5") == expected["leverage"]
    quote = margin.margin_quote(
        {"collateral_amount": "10000000", "leverage": "10"},
        {"available_to_trade": "10000000"},
        {"minLeverage": 1, "maxLeverage": 100},
        "add",
        "5",
        effective_leverage=Decimal(10),
    )
    assert (
        intents.leverage_intent(7, quote["requestedLeverage"]) == expected["margin_add"]
    )


def test_close_admission_wire_golden(tmp_path, monkeypatch, synthetic):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    session = synthetic["session_response"]["data"]["createUserIntentSession"]
    recorded = []

    def post(url, *, json, headers, timeout):
        assert headers == {"Authorization": "Bearer SYNTHETIC_NOT_A_REAL_TOKEN"}
        actual = copy.deepcopy(json["variables"]["input"])
        key = actual["intentKey"]
        assert re.fullmatch(r"[0-9]{8}T[0-9]{9}Z_[0-9A-HJKMNP-TV-Z]{16}", key)
        assert (
            actual["intent"]["tsIntentInit"] == actual["lifecycle"]["tsLifecycleStep"]
        )
        actual["intentKey"] = "<intent-key>"
        actual["intent"]["tsIntentInit"] = "<timestamp>"
        actual["lifecycle"]["tsLifecycleStep"] = "<timestamp>"
        recorded.append(actual)
        golden = copy.deepcopy(synthetic["admission_ack"])
        golden["data"]["recordUserIntent"]["intentKey"] = key
        return httpx.Response(200, json=golden, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    result = intents.submit(keeper.MAINNET_HTTP, session, intents.close_intent(7))
    assert result["status"] == "accepted"
    assert recorded == [synthetic["close_admission_request"]]
    assert (
        storage.list_pending(keeper.MAINNET_HTTP, synthetic["address"])[0]["status"]
        == "accepted"
    )


def test_synthetic_wallet_auth_exchanges_challenge_without_leaking_token(
    monkeypatch, synthetic
):
    calls = []
    signature = "0x" + "1" * 130

    def post(url, *, json, headers, timeout):
        calls.append(json)
        response = (
            synthetic["challenge_response"]
            if len(calls) == 1
            else synthetic["session_response"]
        )
        return httpx.Response(200, json=response, request=httpx.Request("POST", url))

    def sign(args, **kwargs):
        typed = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
        assert typed["domain"] == {"name": "Sai", "version": "1"}
        return SimpleNamespace(stdout=signature)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(keeper.subprocess, "run", sign)
    result = keeper.wallet_session(keeper.MAINNET_HTTP, synthetic["address"], "fixture")
    assert result == synthetic["session_response"]["data"]["createUserIntentSession"]
    assert calls[0]["variables"] == {"address": synthetic["address"]}
    assert calls[1]["variables"] == {
        "signature": signature,
        "challenge": synthetic["challenge_response"]["data"][
            "createUserIntentAuthChallenge"
        ]["nonce"],
    }
