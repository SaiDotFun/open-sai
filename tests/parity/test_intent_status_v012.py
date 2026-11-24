"""Intent status replays for sai-cli/v0.1.2 (8557a7e13dceab0accc369191182c8e01423c91b)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sai_cli.core import keeper
from scenarios import ADDRESS, INTENT, loopback
from test_intent_status import (
    ADDR,
    CLI,
    KEY,
    NIBI,
    RESPONSES,
    invoke,
    read_events,
    read_state,
    replay,
    response,
    seed,
    wire,
)
from test_mcp_stdio import server


@pytest.fixture
def release_pair(cli_pair: tuple[CLI, CLI], tmp_path: Path) -> tuple[CLI, CLI]:
    version = invoke(cli_pair[0], tmp_path / "version-home", "--version")
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == b"sai, version 0.1.2", (
        "set SAI_PARITY_PYTHON to the sai-cli/v0.1.2 executable"
    )
    return cli_pair


def history_page(rows: object) -> dict[str, Any]:
    return {"data": {"perp": {"tradeHistory": rows}}}


def replay_status(
    release_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payloads: tuple[str | dict[str, Any], ...],
    offsets: tuple[int, ...],
    expected_status: str,
) -> dict[str, Any]:
    with replay() as (endpoint, replies, requests):
        outputs = []
        for index, cli in enumerate(release_pair):
            home = tmp_path / str(index)
            home.mkdir(mode=0o700)
            seed(home, endpoint, monkeypatch, "unknown")
            requests.clear()
            replies.extend(response(payload) for payload in payloads)
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
                overrides={"SAI_LOG": "0"},
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert result.stderr == b""
            assert [item.body for item in requests] == [
                wire(keeper.INTENTS, user=ADDR, key=KEY),
                *(
                    wire(keeper.INTENT_HISTORY, trader=ADDR, limit=25, offset=offset)
                    for offset in offsets
                ),
            ]
            assert not replies
            state = read_state(home)
            row = next(row for row in state[3] if row[0] == KEY)
            assert (row[7] or row[4]) == expected_status
            outputs.append((result.stdout, state))
        assert outputs[0] == outputs[1]
        return json.loads(outputs[0][0])


def test_history_pages_deduplicate_matching_fills_and_stop_at_older_block(
    release_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fill = RESPONSES["filled_history"]["data"]["perp"]["tradeHistory"][0]
    height = fill["block"]["block"]
    newer = {"block": {"block": height + 1}}
    older = {"block": {"block": height - 1}}
    second_fill = {
        **fill,
        "id": fill["id"] + 1,
        "txHash": 123,
        "evmTxHash": fill["txHash"],
    }
    output = replay_status(
        release_pair,
        tmp_path,
        monkeypatch,
        (
            "outcome_not_indexed",
            history_page([*([newer] * 24), fill]),
            history_page([fill, second_fill, None, *([older] * 22)]),
        ),
        (0, 25),
        "terminal",
    )
    assert output["outcome"] == "terminal-filled"
    assert [row["id"] for row in output["matchedTradeHistory"]] == [
        fill["id"],
        second_fill["id"],
    ]
    assert output["matchedTradeHistory"][1]["txHashInvalid"] is True


@pytest.mark.parametrize(
    "full_page", [False, True], ids=["boolean-id", "boolean-block"]
)
def test_history_treats_booleans_as_integers_when_scanning(
    release_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_page: bool,
) -> None:
    fill = {**RESPONSES["filled_history"]["data"]["perp"]["tradeHistory"][0], "id": 1}
    rows = [fill, {**fill, "id": True}]
    if full_page:
        rows.extend([{}] * 22)
        rows.append({"block": {"block": True}})
    output = replay_status(
        release_pair,
        tmp_path,
        monkeypatch,
        ("outcome_not_indexed", history_page(rows)),
        (0,),
        "terminal",
    )
    assert [row["id"] for row in output["matchedTradeHistory"]] == [1]


def test_pending_history_retries_once_with_five_request_cap(
    release_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_match = history_page([{}] * 25)
    output = replay_status(
        release_pair,
        tmp_path,
        monkeypatch,
        ("outcome_not_indexed", *([no_match] * 5)),
        (0, 25, 50, 75, 0),
        "pending",
    )
    assert output["outcome"] == "pending"
    assert output["matchedTradeHistory"] == []


def test_malformed_unicode_hash_cannot_resolve_pending_intent(
    release_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = RESPONSES["outcome_not_indexed"]["data"]["userIntents"][0]
    fill = RESPONSES["filled_history"]["data"]["perp"]["tradeHistory"][0]
    page = history_page([{**fill, "txHash": "σ"}])
    output = replay_status(
        release_pair,
        tmp_path,
        monkeypatch,
        ({"data": {"userIntents": [{**intent, "txHash": "0xΣ"}]}}, page, page),
        (0, 0),
        "pending",
    )
    assert output["outcome"] == "pending"
    assert output["matchedTradeHistory"] == []
    assert output["userIntents"][0]["txHashInvalid"] is True


@pytest.mark.parametrize("pending", [False, True], ids=["executed", "pending"])
def test_mcp_equal_malformed_hash_uses_one_history_page(
    release_pair: tuple[CLI, CLI], tmp_path: Path, pending: bool
) -> None:
    scenario = "mcp-pending-malformed-hash" if pending else "normal"
    for index, (cli,) in enumerate(release_pair):
        native = index == 1
        home = tmp_path / str(index)
        with (
            loopback(scenario) as endpoints,
            server(Path(cli), home, endpoints, seed="ledger") as stdio,
        ):
            result = stdio.call(2, "intent_status", addr=ADDRESS, key=INTENT)
            assert result["isError"] is False
            assert not endpoints.errors
            assert [request.label for request in endpoints.wire] == [
                "userIntents",
                "intentTradeHistory",
            ]
            assert endpoints.wire[1].variables == {
                "trader": ADDRESS,
                "limit": 25,
                "offset": 0,
            }
        document = json.loads(result["content"][0]["text"])
        assert document["userIntents"][0]["txHashInvalid"] is True
        # History can corroborate a fill only with a validated transaction hash.
        assert len(document["matchedTradeHistory"]) == (0 if native else 1)
        [row] = read_state(home / "sai")[3]
        assert (row[7] or row[4]) == ("pending" if native and pending else "terminal")


@pytest.mark.parametrize(
    "malformed", [{"data": {}}, history_page(None), history_page({})]
)
def test_malformed_history_stops_the_scan_and_keeps_pending(
    release_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed: dict[str, Any],
) -> None:
    output = replay_status(
        release_pair,
        tmp_path,
        monkeypatch,
        ("outcome_not_indexed", malformed),
        (0,),
        "pending",
    )
    assert output["outcome"] == "pending"
    assert output["matchedTradeHistory"] == []


def test_malformed_intent_list_leaves_ledger_alone(
    release_pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = replay_status(
        release_pair,
        tmp_path,
        monkeypatch,
        ({"data": {"userIntents": 123}},),
        (),
        "unknown",
    )
    assert output == {}


def test_status_projects_public_fields_and_correlates_events(
    release_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    source = RESPONSES["market_order_expired"]["data"]["userIntents"][0]
    row = {
        **source,
        "execBlockTs": "2026-11-20 15:43:31.1234567890123456Z",
        "txHash": "synthetic-invalid-hash",
        "errorCode": "wallet_rejected",
        "errorMsg": "synthetic-private-diagnostic",
    }
    outputs = []
    with replay() as (endpoint, replies, requests):
        for index, cli in enumerate(release_pair):
            home = tmp_path / str(index)
            requests.clear()
            replies.append(response({"data": {"userIntents": [row]}}))
            result = invoke(
                cli,
                home,
                "--json",
                "--endpoint",
                endpoint,
                "intent",
                "status",
                NIBI,
                "--key",
                KEY,
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert not result.stderr
            assert [item.body for item in requests] == [
                wire(keeper.INTENTS, user=NIBI, key=KEY)
            ]
            assert not replies
            events = read_events(home)
            [request] = [
                event for event in events if event["event"] == "keeper.request"
            ]
            [observed] = [
                event for event in events if event["event"] == "intent.keeper"
            ]
            assert request["intentKey"] == KEY == observed["intentKey"]
            assert observed["addr"] == ADDR
            assert observed["errorCode"] == "wallet_rejected"
            assert observed["txHashInvalid"] is True
            assert "errorMsg" not in observed
            outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    [public] = json.loads(outputs[0])["userIntents"]
    assert public["execBlockTsInvalid"] is True
    assert public["errorCode"] == "wallet_rejected"
    assert public["errorMsg"] == "Keeper reported an intent error."
