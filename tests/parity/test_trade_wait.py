"""Compare trade --wait through CLI subprocesses and a loopback Keeper."""

from __future__ import annotations

import base64
import json
import os
import select
import signal
import sqlite3
import subprocess
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from sai_cli.core import keeper, log, storage
from test_intent_status import (
    ADDR,
    CLI,
    ReplayReply,
    Reply,
    Request,
    cli_environment,
    invoke,
    read_events,
    replay,
    response,
)
from test_intent_wait import (
    FILL_BLOCK,
    REQUEST_BLOCK,
    history,
    indexed,
    intent,
    ledger_state,
    without_legacy_fill_placeholders,
)

OPEN = (
    "open",
    "--market",
    "1",
    "--side",
    "long",
    "--type",
    "market",
    "--amount",
    "4",
    "--leverage",
    "2",
    "--slippage",
    "1",
)
CLOSE = ("close", "17")
CANCEL = (*CLOSE, "--cancel-order")
TPSL = ("tpsl", "17", "--take-profit", "110.0100", "--clear-stop-loss")
TP = ("tpsl", "17", "--take-profit", "110.0100")
SL = ("tpsl", "17", "--stop-loss", "89.2500")
CLEAR_SL = ("tpsl", "17", "--clear-stop-loss")
LEVERAGE = ("leverage", "17", "--leverage", "5.2500")
UPDATES = {
    TPSL: {
        "takeProfit": {"operation": "set", "value": "110.0100"},
        "stopLoss": {"operation": "clear"},
    },
    TP: {
        "takeProfit": {"operation": "set", "value": "110.0100"},
        "stopLoss": {"operation": "keep"},
    },
    SL: {
        "takeProfit": {"operation": "keep"},
        "stopLoss": {"operation": "set", "value": "89.2500"},
    },
    CLEAR_SL: {
        "takeProfit": {"operation": "keep"},
        "stopLoss": {"operation": "clear"},
    },
    LEVERAGE: {
        "takeProfit": {"operation": "keep"},
        "stopLoss": {"operation": "keep"},
        "requestedLeverage": "5.2500",
    },
}
POSITION_ACTIONS = (TPSL, LEVERAGE)
INDEXED_POSITIONS = {
    TPSL: {"tp": "110.0100", "sl": "0", "leverage": "2.5000"},
    TP: {"tp": "110.0100", "sl": "85.5000", "leverage": "2.5000"},
    SL: {"tp": "115.1250", "sl": "89.2500", "leverage": "2.5000"},
    CLEAR_SL: {"tp": "115.1250", "sl": "0", "leverage": "2.5000"},
    LEVERAGE: {"tp": "115.1250", "sl": "85.5000", "leverage": "5.2500"},
}
CONTRACT = "nibi1contract"
NODE_INFO = "/cosmos/base/tendermint/v1beta1/node_info"
SMART_QUERY = f"/cosmwasm/wasm/v1/contract/{CONTRACT}/smart/"
ROUTE = {"enabled": True, "chainId": "cataclysm-1", "contractAddress": CONTRACT}
MARKETS = {
    "data": {
        "perp": {
            "borrowings": [
                {
                    "marketId": 1,
                    "collateralToken": {"symbol": "USDC"},
                    "isOpen": True,
                    "price": "81000.50",
                }
            ]
        }
    }
}


@dataclass(frozen=True)
class Case:
    name: str
    args: tuple[str, ...]
    kind: str
    change: str
    outcome: str = "filled"
    admission: str = "accepted"
    alternate_rust_args: tuple[str, ...] = ()
    direct_update: bool = False
    provisional: bool = False


CASES = (
    Case("open", OPEN, "OPEN_TRADE", "position_opened"),
    Case("close", CLOSE, "CLOSE_POSITION", "position_closed_user"),
    Case(
        "close-cancel", CANCEL, "CANCEL_PENDING_TRADE", "order_closed_user", "succeeded"
    ),
    Case(
        "cancel-alias",
        CANCEL,
        "CANCEL_PENDING_TRADE",
        "order_closed_user",
        "succeeded",
        alternate_rust_args=("cancel", "17"),
    ),
    Case(
        "lost-ack-reconciles",
        CLOSE,
        "CLOSE_POSITION",
        "position_closed_user",
        admission="lost",
    ),
    Case(
        "rejected",
        CLOSE,
        "CLOSE_POSITION",
        "position_closed_user",
        "rejected",
        admission="rejected",
    ),
    Case(
        "failed-after-admission",
        CLOSE,
        "CLOSE_POSITION",
        "position_closed_user",
        "failed",
    ),
    Case("tpsl", TPSL, "UPDATE_TRADE", "tpsl_updated", "succeeded"),
    Case("tp-set", TP, "UPDATE_TRADE", "tp_updated", "succeeded", direct_update=True),
    Case("sl-set", SL, "UPDATE_TRADE", "sl_updated", "succeeded", direct_update=True),
    Case(
        "sl-clear",
        CLEAR_SL,
        "UPDATE_TRADE",
        "sl_updated",
        "succeeded",
        direct_update=True,
    ),
    Case(
        "leverage",
        LEVERAGE,
        "UPDATE_TRADE",
        "leverage_updated",
        "succeeded",
        direct_update=True,
    ),
    Case("tpsl-lost-ack", TPSL, "UPDATE_TRADE", "tpsl_updated", "succeeded", "lost"),
    Case(
        "leverage-lost-ack",
        LEVERAGE,
        "UPDATE_TRADE",
        "leverage_updated",
        "succeeded",
        "lost",
    ),
    Case("tpsl-rejected", TPSL, "UPDATE_TRADE", "tpsl_updated", "rejected", "rejected"),
    Case("leverage-failed", LEVERAGE, "UPDATE_TRADE", "leverage_updated", "failed"),
    Case(
        "tpsl-index-lag",
        TPSL,
        "UPDATE_TRADE",
        "tpsl_updated",
        "succeeded",
        direct_update=True,
        provisional=True,
    ),
    Case(
        "leverage-index-lag",
        LEVERAGE,
        "UPDATE_TRADE",
        "leverage_updated",
        "succeeded",
        direct_update=True,
        provisional=True,
    ),
)


def trade_wait_pair(
    request: pytest.FixtureRequest, args: tuple[str, ...]
) -> tuple[CLI, CLI]:
    fixture = "position_wait_cli_pair" if args in UPDATES else "wait_cli_pair"
    return request.getfixturevalue(fixture)


@pytest.fixture
def position_wait_cli_pair(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[CLI, CLI]:
    if "SAI_PARITY_BIN" not in os.environ:
        pytest.skip("set SAI_PARITY_BIN to the Rust sai executable")
    reference = os.environ.get("SAI_PARITY_POSITION_WAIT_PYTHON")
    if not reference:
        pytest.fail(
            "set SAI_PARITY_POSITION_WAIT_PYTHON to the pinned Python sai launcher"
        )
    monkeypatch.setenv("SAI_PARITY_PYTHON", reference)
    return request.getfixturevalue("cli_pair")


def connect(home: Path, endpoint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAI_HOME", str(home))
    with log.silenced():
        storage.save_session(
            {
                "evmAddress": ADDR,
                "endpoint": endpoint,
                "token": "fixture-token",
                "expiresAt": "2099-01-01T00:00:00Z",
            }
        )


def submitted_key(request: Request, kind: str) -> str:
    wire = json.loads(request.body)
    assert wire["query"].startswith("mutation")
    assert request.headers["authorization"] == "Bearer fixture-token"
    submission = wire["variables"]["input"]
    assert submission["intent"]["kind"] == kind
    return submission["intentKey"]


def target_lcd(request: Request, *, trade_type: str = "trade") -> Reply:
    assert "authorization" not in request.headers
    if request.path == NODE_INFO:
        return response({"default_node_info": {"network": ROUTE["chainId"]}})
    path = urlsplit(request.path).path
    assert path.startswith(SMART_QUERY)
    query = json.loads(base64.b64decode(unquote(path.removeprefix(SMART_QUERY))))
    assert query == {"get_trade_raw": {"trader": ADDR, "index": 17}}
    return response(
        {
            "data": {
                "user": ADDR,
                "user_trade_index": "UserTradeIndex(17)",
                "is_open": True,
                "trade_type": trade_type,
            }
        }
    )


def queue_update_preflight(replies: deque[ReplayReply]) -> int:
    def get_position(request: Request) -> Reply:
        assert "authorization" not in request.headers
        assert json.loads(request.body) == {
            "query": keeper.POSITION,
            "variables": {"trader": ADDR, "id": 17},
        }
        return response({"data": {"perp": {"trade": {"id": 17, "trader": ADDR}}}})

    def get_route(request: Request) -> Reply:
        assert "authorization" not in request.headers
        assert json.loads(request.body) == {"query": keeper.ROUTE, "variables": {}}
        return response({"data": {"oneClickTrading": ROUTE}})

    replies.extend((get_position, get_route))
    return 4  # Indexed position, route, LCD chain ID and contract position.


def close_preflight(replies: deque[ReplayReply], rust: bool) -> int:
    # The pinned wait reference predates the market-close owner check.
    return queue_update_preflight(replies) if rust else 0


def trade_wait_args(endpoint: str, args: CLI) -> CLI:
    return (
        "--endpoint",
        endpoint,
        "--lcd-endpoint",
        endpoint.removesuffix("/query"),
        "--json",
        "trade",
        *args,
        "--wait",
    )


def accepted(key: str, kind: str) -> Reply:
    return response(
        {
            "data": {
                "recordUserIntent": {
                    "intentKey": key,
                    "kind": kind,
                    "execActor": "server",
                    "lifecycleStep": "jwt_pass",
                    "evmAddress": ADDR,
                }
            }
        }
    )


def filled_close(
    replies: deque[ReplayReply], requests: list[Request], rust: bool
) -> int:
    preflight = close_preflight(replies, rust)
    kind = "CLOSE_POSITION"

    def key() -> str:
        return submitted_key(requests[preflight], kind)

    replies.extend(
        (
            lambda request: accepted(submitted_key(request, kind), kind),
            lambda _: response(intent("filled", kind=kind, intentKey=key())),
            lambda _: response(
                history(indexed("position_closed_user", intentKey=key()))
            ),
        )
    )
    return preflight


def normalized(document: Any, key: str) -> Any:
    if isinstance(document, dict):
        return {name: normalized(value, key) for name, value in document.items()}
    if isinstance(document, (list, tuple)):
        return [normalized(value, key) for value in document]
    return document.replace(key, "<intent>") if isinstance(document, str) else document


def indexed_update(change: str, key: str, args: tuple[str, ...]) -> dict[str, Any]:
    row = indexed(change, intentKey=key)
    row.pop("marketOrder")
    row["trade"].update(
        id=17,
        openBlock={"block": REQUEST_BLOCK - 100},
        openPrice="100.1250",
        **INDEXED_POSITIONS[args],
    )
    return row


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_trade_wait(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: Case,
) -> None:
    documents = []
    keys: list[str] = []
    update = case.args in UPDATES
    trade_type = "limit" if case.kind == "CANCEL_PENDING_TRADE" else "trade"
    with replay(get=lambda request: target_lcd(request, trade_type=trade_type)) as (
        endpoint,
        replies,
        requests,
    ):
        for index, cli in enumerate(trade_wait_pair(request, case.args)):
            home = tmp_path / str(index) / "sai"
            connect(home, endpoint, monkeypatch)
            requests.clear()
            keys.clear()

            def admit(request: Request) -> Reply:
                wire = json.loads(request.body)
                if (
                    case.kind == "CANCEL_PENDING_TRADE"
                    and wire["query"] == keeper.ROUTE
                ):
                    assert len(requests) == 1
                    assert wire["variables"] == {}
                    assert "authorization" not in request.headers
                    replies.appendleft(admit)
                    return response({"data": {"oneClickTrading": ROUTE}})
                key = submitted_key(request, case.kind)
                keys.append(key)
                if update:
                    assert (
                        wire["variables"]["input"]["intent"]["body"]
                        == UPDATES[case.args]
                    )
                if case.args == OPEN:
                    assert (
                        json.loads(request.body)["variables"]["input"]["intent"][
                            "body"
                        ]["intentPrice"]
                        == "81000.50"
                    )
                if case.admission == "rejected":
                    return response(
                        {"errors": [{"extensions": {"code": "INTENT_NOT_ACCEPTED"}}]}
                    )
                if case.admission == "lost":
                    return Reply(disconnect=True)
                return accepted(key, case.kind)

            def poll(request: Request, name: str) -> Reply:
                assert "authorization" not in request.headers
                assert json.loads(request.body)["variables"] == {
                    "user": ADDR,
                    "key": keys[0],
                    "includeAction": True,
                }
                body = (
                    {"orderType": "market"}
                    if case.kind == "OPEN_TRADE"
                    else UPDATES.get(case.args, {})
                )
                return response(
                    intent(name, intentKey=keys[0], kind=case.kind, body=body)
                )

            def history_reply(request: Request, *, present: bool) -> Reply:
                assert "authorization" not in request.headers
                assert json.loads(request.body) == {
                    "query": keeper.INTENT_HISTORY,
                    "variables": {"trader": ADDR, "limit": 25, "offset": 0},
                }
                if not present:
                    return response(history())
                row = (
                    indexed_update(case.change, keys[0], case.args)
                    if case.direct_update
                    else indexed(case.change, intentKey=keys[0])
                )
                return response(history(row))

            preflight = 0
            if case.kind == "CLOSE_POSITION":
                preflight = close_preflight(replies, rust=index == 1)
            elif case.args == OPEN:
                replies.append(response(MARKETS))
            if update:
                queue_update_preflight(replies)
            replies.append(admit)
            if case.provisional:
                replies.extend(
                    (
                        lambda request: poll(request, "outcome_not_indexed"),
                        lambda request: history_reply(request, present=False),
                        lambda request: history_reply(request, present=False),
                        lambda request: poll(request, "outcome_not_indexed"),
                        lambda request: history_reply(request, present=True),
                    )
                )
            elif case.admission != "rejected":
                replies.append(lambda request: poll(request, "pending_exec"))
                if case.outcome == "failed":
                    replies.append(
                        lambda request: poll(request, "market_order_fill_failed")
                    )
                else:
                    replies.extend(
                        (
                            lambda request: poll(request, "filled"),
                            lambda request: history_reply(request, present=True),
                        )
                    )
            args = (
                case.alternate_rust_args
                if index and case.alternate_rust_args
                else case.args
            )
            result = invoke(cli, home, *trade_wait_args(endpoint, args))
            expected_exit = {"rejected": 4, "failed": 1}.get(case.outcome, 0)
            assert result.returncode == expected_exit, result
            assert result.stderr == b""
            document = json.loads(result.stdout)
            key = document["intentKey"]
            assert keys == [key]
            assert not replies
            wire = [
                json.loads(request.body)
                if request.body
                else {"path": urlsplit(request.path).path}
                for request in requests[preflight:]
            ]
            if case.kind == "CANCEL_PENDING_TRADE":
                assert wire[0]["query"] == keeper.ROUTE
                assert requests[1].path == NODE_INFO
                assert requests[2].path.startswith(SMART_QUERY)
            elif update:
                assert wire[0]["query"] == keeper.POSITION
                assert wire[1]["query"] == keeper.ROUTE
                assert requests[2].path == NODE_INFO
                assert requests[3].path.startswith(SMART_QUERY)
            else:
                if preflight:
                    assert requests[2].path == NODE_INFO
                    assert requests[3].path.startswith(SMART_QUERY)
                assert all(request.body for request in requests[preflight:])
            assert (
                sum(item.get("query", "").startswith("mutation") for item in wire) == 1
            )
            assert document["outcome"] == document["status"] == case.outcome
            assert document["addr"] == ADDR
            if case.args == OPEN:
                assert document["referencePrice"] == "81000.50"
            if case.outcome != "rejected":
                receipt = document["receipt"]
                assert receipt["requestBlock"] == (
                    None if case.provisional else REQUEST_BLOCK
                )
                assert receipt["requestTxHash"] == "a" * 64
                if case.outcome != "failed":
                    assert receipt["execution"]["block"] == FILL_BLOCK
                    if update:
                        assert receipt["execution"]["tradeChangeType"] == case.change
                        assert receipt["fill"] is None
                    if case.direct_update:
                        position = receipt["positionNow"]
                        assert receipt["execution"]["marketOrderSequence"] is None
                        assert position["id"] == 17
                        assert position["openPrice"] == "100.1250"
                        assert {
                            field: position[field] for field in ("tp", "sl", "leverage")
                        } == INDEXED_POSITIONS[case.args]
            if update and expected_exit:
                assert (
                    document["error"]["code"]
                    == {"rejected": "ADMISSION_REJECTED", "failed": "KEEPER_ERROR"}[
                        case.outcome
                    ]
                )
            ledger = ledger_state(home)
            statuses = (
                ("rejected", None)
                if case.outcome == "rejected"
                else ("terminal", "terminal")
            )
            assert ledger == [(key, *statuses)]
            for item in wire:
                if item.get("query", "").startswith("mutation"):
                    submission = item["variables"]["input"]
                    stamp = submission["intent"].pop("tsIntentInit")
                    assert submission["lifecycle"].pop("tsLifecycleStep") == stamp
            stdout = result.stdout
            if index == 0 and case.outcome == "filled":
                stdout = without_legacy_fill_placeholders(stdout)
            documents.append(
                (
                    stdout.replace(key.encode(), b"<intent>"),
                    normalized([wire, ledger], key),
                )
            )
    assert documents[0] == documents[1]


@pytest.mark.parametrize("wait", [(), ("--wait",)], ids=["submit", "wait"])
def test_trade_cancel_refuses_a_filled_target_before_mutation(
    wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait: tuple[str, ...],
) -> None:
    def route(request: Request) -> Reply:
        wire = json.loads(request.body)
        if wire["query"].startswith("mutation"):
            # Reject a premature mutation so the failing case returns promptly.
            return response(
                {"errors": [{"extensions": {"code": "INTENT_NOT_ACCEPTED"}}]}
            )
        assert wire == {"query": keeper.ROUTE, "variables": {}}
        return response({"data": {"oneClickTrading": ROUTE}})

    with replay(get=target_lcd) as (endpoint, replies, requests):
        for index, cli in enumerate(wait_cli_pair):
            home = tmp_path / str(index) / "sai"
            connect(home, endpoint, monkeypatch)
            requests.clear()
            replies.append(route)
            result = invoke(
                cli,
                home,
                "--endpoint",
                endpoint,
                "--lcd-endpoint",
                endpoint.removesuffix("/query"),
                "--json",
                "trade",
                *CANCEL,
                *wait,
            )
            assert not any(
                request.body
                and json.loads(request.body)["query"].startswith("mutation")
                for request in requests
            )
            assert len(requests) == 3
            assert result.returncode == 1
            assert result.stderr == b""
            assert json.loads(result.stdout)["error"]["code"] == "INVALID_REQUEST"
            assert not (home / "trader/intents.sqlite3").exists()


@pytest.mark.parametrize(
    "args", [CLOSE, TPSL, LEVERAGE], ids=["close", "tpsl", "leverage"]
)
def test_trade_wait_dry_run(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
) -> None:
    update = args in UPDATES
    with replay(get=target_lcd) as (endpoint, replies, requests):
        for index, cli in enumerate(trade_wait_pair(request, args)):
            home = tmp_path / str(index) / "sai"
            if update or index == 1:
                connect(home, endpoint, monkeypatch)
            else:
                home.mkdir(parents=True)
            documents = []
            for wait in ((), ("--wait",)):
                requests.clear()
                preflight = (
                    queue_update_preflight(replies)
                    if update
                    else close_preflight(replies, rust=index == 1)
                )
                result = invoke(
                    cli,
                    home,
                    "--endpoint",
                    endpoint,
                    "--lcd-endpoint",
                    endpoint.removesuffix("/query"),
                    "--json",
                    "trade",
                    *args,
                    "--dry-run",
                    *wait,
                )
                assert result.returncode == 0, result
                assert result.stderr == b""
                document = json.loads(result.stdout)
                assert document["dryRun"] is True
                assert "receipt" not in document
                assert len(requests) == preflight
                assert not replies
                if update:
                    assert requests[2].path == NODE_INFO
                    assert requests[3].path.startswith(SMART_QUERY)
                    assert "positionOwnerVerified" not in document
                assert all(
                    not request.body
                    or not json.loads(request.body)["query"].startswith("mutation")
                    for request in requests
                )
                documents.append(document)
            assert not (home / "trader/intents.sqlite3").exists()
            assert documents[0] == documents[1]


@pytest.mark.parametrize("args", POSITION_ACTIONS, ids=lambda args: args[0])
def test_position_update_submits_same_request_with_wait(
    position_wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
) -> None:
    kind = "UPDATE_TRADE"
    change = "tpsl_updated" if args == TPSL else "leverage_updated"
    with replay(get=target_lcd) as (endpoint, replies, requests):

        def dispatch(cli: CLI, home: Path, wait_flag: tuple[str, ...]) -> Any:
            connect(home, endpoint, monkeypatch)
            requests.clear()
            keys: list[str] = []
            admissions = []

            def admit(request: Request) -> Reply:
                key = submitted_key(request, kind)
                keys.append(key)
                with closing(
                    sqlite3.connect(home / "trader/intents.sqlite3")
                ) as ledger:
                    reservation = ledger.execute(
                        "SELECT id, endpoint, account, kind, entity, status, keeper_status FROM intents"
                    ).fetchall()
                assert reservation == [
                    (key, endpoint, ADDR, kind, "17", "unknown", None)
                ]
                wire = json.loads(request.body)
                assert wire["query"] == keeper.SUBMIT
                envelope = wire["variables"]["input"]
                stamp = envelope["intent"].pop("tsIntentInit")
                assert stamp and envelope["lifecycle"].pop("tsLifecycleStep") == stamp
                preflight = [
                    (
                        urlsplit(item.path).path,
                        json.loads(item.body) if item.body else None,
                    )
                    for item in requests[:-1]
                ]
                assert len(preflight) == 4
                admissions.append(normalized([preflight, wire, reservation], key))
                return accepted(key, kind)

            queue_update_preflight(replies)
            replies.append(admit)
            if wait_flag:
                replies.extend(
                    (
                        lambda _: response(
                            intent(
                                "filled",
                                kind=kind,
                                intentKey=keys[0],
                                body=UPDATES[args],
                            )
                        ),
                        lambda _: response(
                            history(indexed_update(change, keys[0], args))
                        ),
                    )
                )
            result = invoke(
                cli,
                home,
                "--endpoint",
                endpoint,
                "--lcd-endpoint",
                endpoint.removesuffix("/query"),
                "--json",
                "trade",
                *args,
                *wait_flag,
            )
            assert (result.returncode, result.stderr) == (0, b""), result
            document = json.loads(result.stdout)
            key = document["intentKey"]
            assert keys == [key]
            assert not replies
            assert (
                sum(
                    json.loads(item.body)["query"].startswith("mutation")
                    for item in requests
                    if item.body
                )
                == 1
            )
            if wait_flag:
                assert document["status"] == "succeeded"
                assert ledger_state(home) == [(key, "terminal", "terminal")]
            else:
                assert document["status"] == "accepted"
                assert ledger_state(home) == [(key, "accepted", None)]
            return admissions[0]

        for index, cli in enumerate(position_wait_cli_pair):
            root = tmp_path / str(index)
            assert dispatch(cli, root / "submit" / "sai", ()) == dispatch(
                cli, root / "wait" / "sai", ("--wait",)
            )


@pytest.mark.parametrize(
    ("args", "phase"),
    [
        pytest.param(args, phase, id=f"{args[0]}-{phase}")
        for args in (CLOSE, *POSITION_ACTIONS)
        for phase in (
            ("submit", "poll", "accepted", "pending")
            if args in POSITION_ACTIONS
            else ("submit", "poll")
        )
    ],
)
def test_trade_wait_interrupt_retains_reservation(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    args: tuple[str, ...],
) -> None:
    documents = []
    update = args in UPDATES
    kind = "UPDATE_TRADE" if update else "CLOSE_POSITION"
    acknowledged = phase in ("accepted", "pending")
    status = phase if acknowledged else "unknown"
    ledger_status = ("unknown", "pending") if phase == "pending" else (status, None)
    with replay(get=target_lcd) as (endpoint, replies, requests):
        for index, cli in enumerate(trade_wait_pair(request, args)):
            home = tmp_path / str(index) / "sai"
            connect(home, endpoint, monkeypatch)
            requests.clear()
            preflight_reads = (
                queue_update_preflight(replies)
                if update
                else close_preflight(replies, rust=index == 1)
            )
            command = trade_wait_args(endpoint, args)
            # A zero-length body completes as soon as the headers arrive.
            delayed = Reply(body=b"{}", body_delay=4.0)
            if acknowledged:
                replies.extend(
                    (
                        lambda request: accepted(submitted_key(request, kind), kind),
                        lambda _, preflight_reads=preflight_reads: response(
                            intent(
                                "pending_exec" if phase == "pending" else "accepted",
                                kind=kind,
                                intentKey=submitted_key(
                                    requests[preflight_reads], kind
                                ),
                                body=UPDATES[args],
                            )
                        ),
                    )
                )
            elif phase == "poll":
                replies.append(Reply(disconnect=True))
            replies.append(delayed)
            expected_requests = preflight_reads + 1 + (phase != "submit") + acknowledged
            with subprocess.Popen(
                [*cli, *command],
                env=cli_environment(home),
                cwd=home.parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as process:
                try:
                    end = time.monotonic() + 5
                    while len(requests) < expected_requests and time.monotonic() < end:
                        time.sleep(0.01)
                    assert len(requests) == expected_requests
                    key = submitted_key(requests[preflight_reads], kind)
                    assert ledger_state(home) == [(key, *ledger_status)]
                    process.send_signal(signal.SIGINT)
                    stdout, stderr = process.communicate(timeout=2)
                finally:
                    if process.poll() is None:
                        process.kill()
                assert process.returncode == 5, (stdout, stderr)
            assert stderr == b""
            document = json.loads(stdout)
            assert document["intentKey"] == key
            assert document["outcome"] == "unknown"
            assert document["localStatus"] == status
            assert document["interrupted"] is True
            assert document["code"] == "OUTCOME_UNKNOWN"
            assert document["receipt"] is None
            assert ledger_state(home) == [(key, *ledger_status)]
            for poll in requests[preflight_reads + 1 :]:
                assert json.loads(poll.body)["variables"]["key"] == key
            retry_preflight = (
                queue_update_preflight(replies)
                if update
                else close_preflight(replies, rust=index == 1)
            )
            again = invoke(cli, home, *command)
            assert again.returncode == 1, again
            assert json.loads(again.stdout)["error"]["intentKey"] == key
            assert len(requests) == expected_requests + retry_preflight
            assert ledger_state(home) == [(key, *ledger_status)]
            assert not replies
            documents.append(normalized(document, key))
    assert documents[0] == documents[1]


def test_position_trade_wait_times_out(
    position_wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_wait(
        cli: CLI,
        home: Path,
        endpoint: str,
        args: tuple[str, ...],
        replies: deque[ReplayReply],
        requests: list[Request],
    ) -> dict[str, Any]:
        keys: list[str] = []
        poll_times: list[float] = []
        deadline_seconds = 30

        def admit(request: Request) -> Reply:
            key = submitted_key(request, "UPDATE_TRADE")
            keys.append(key)
            return accepted(key, "UPDATE_TRADE")

        def reconcile(request: Request) -> Reply:
            assert "authorization" not in request.headers
            wire = json.loads(request.body)
            replies.append(reconcile)
            if wire["query"] == keeper.INTENT_HISTORY:
                assert wire["variables"] == {"trader": ADDR, "limit": 25, "offset": 0}
                return response(history())
            assert wire["query"] == keeper.WAIT_INTENTS
            assert wire["variables"] == {
                "user": ADDR,
                "key": keys[0],
                "includeAction": True,
            }
            poll_times.append(time.monotonic())
            return response(
                intent(
                    "outcome_not_indexed",
                    intentKey=keys[0],
                    kind="UPDATE_TRADE",
                    body=UPDATES[args],
                )
            )

        queue_update_preflight(replies)
        replies.extend((admit, reconcile))
        command = (
            "--endpoint",
            endpoint,
            "--lcd-endpoint",
            endpoint.removesuffix("/query"),
            "--json",
            "trade",
            *args,
            "--wait",
        )
        result = subprocess.run(
            [*cli, *command],
            env=cli_environment(home),
            cwd=home.parent,
            capture_output=True,
            check=False,
            timeout=deadline_seconds + 15,
        )
        ended = time.monotonic()
        assert (result.returncode, result.stderr) == (5, b"")
        document = json.loads(result.stdout)
        key = document["intentKey"]
        assert keys == [key]
        assert len(poll_times) >= 2
        elapsed = ended - poll_times[0]
        assert elapsed >= deadline_seconds - 1.5, elapsed
        assert document["outcome"] == document["status"] == "unknown"
        assert document["code"] == document["error"]["code"] == "OUTCOME_UNKNOWN"
        assert document["receipt"]["requestBlock"] is None
        assert document["receipt"]["requestTxHash"] == "a" * 64
        assert document["receipt"]["execution"] is None
        assert ledger_state(home) == [(key, "unknown", "pending")]
        waits = [
            (event["event"], event.get("timeoutMs"), event.get("code"))
            for event in read_events(home, cli)
            if event.get("intentKey") == key
            and event["event"].startswith("intent.wait.")
        ]
        assert waits == [
            ("intent.wait.start", deadline_seconds * 1000, None),
            ("intent.wait.end", None, "OUTCOME_UNKNOWN"),
        ]
        replies.clear()
        queue_update_preflight(replies)
        again = invoke(cli, home, *command)
        assert again.returncode == 1, again
        assert json.loads(again.stdout)["error"]["intentKey"] == key
        assert ledger_state(home) == [(key, "unknown", "pending")]
        assert not replies
        assert (
            sum(
                json.loads(item.body)["query"].startswith("mutation")
                for item in requests
                if item.body
            )
            == 1
        )
        return normalized(document, key)

    # Both commands and runtimes exercise the real 30-second deadline concurrently.
    with ExitStack() as servers, ThreadPoolExecutor(max_workers=4) as batch:
        pairs = []
        for args in POSITION_ACTIONS:
            pending = []
            for index, cli in enumerate(position_wait_cli_pair):
                endpoint, replies, requests = servers.enter_context(
                    replay(get=target_lcd)
                )
                home = tmp_path / args[0] / str(index) / "sai"
                connect(home, endpoint, monkeypatch)
                pending.append(
                    batch.submit(
                        timed_wait, cli, home, endpoint, args, replies, requests
                    )
                )
            pairs.append(pending)
        for pair in pairs:
            assert pair[0].result() == pair[1].result()


def test_trade_wait_recovers_when_stdout_closes(
    wait_cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = []
    kind = "CLOSE_POSITION"
    with replay(get=target_lcd) as (endpoint, replies, requests):
        for index, cli in enumerate(wait_cli_pair):
            home = tmp_path / str(index) / "sai"
            connect(home, endpoint, monkeypatch)
            requests.clear()
            preflight = filled_close(replies, requests, rust=index == 1)
            read, write = os.pipe()
            os.close(read)
            try:
                result = subprocess.run(
                    [*cli, *trade_wait_args(endpoint, CLOSE)],
                    env=cli_environment(home),
                    cwd=home.parent,
                    stdout=write,
                    stderr=subprocess.PIPE,
                    timeout=4,
                    check=False,
                )
            finally:
                os.close(write)
            stderr = result.stderr
            if index == 0 and result.returncode == 120:
                # CPython can override the exit code when its final stdout flush fails.
                stderr, flush_error = stderr.split(b"\n", 1)
                assert b"BrokenPipeError" in flush_error
            else:
                assert result.returncode == 1, result
            assert len(requests) == preflight + 3
            key = submitted_key(requests[preflight], kind)
            document = json.loads(stderr)
            assert document["intentKey"] == key
            assert document["localStatus"] == document["outcome"] == "terminal"
            assert document["receipt"] is None
            assert document["interrupted"] is False
            assert ledger_state(home) == [(key, "terminal", "terminal")]
            documents.append(normalized(document, key))
    assert documents[0] == documents[1]


@pytest.mark.parametrize("args", [CLOSE, *POSITION_ACTIONS], ids=lambda args: args[0])
def test_trade_wait_interrupts_a_full_stdout_pipe(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
) -> None:
    update = args in UPDATES
    kind = "UPDATE_TRADE" if update else "CLOSE_POSITION"
    change = {TPSL: "tpsl_updated", LEVERAGE: "leverage_updated"}.get(
        args, "position_closed_user"
    )
    with replay(get=target_lcd) as (endpoint, replies, requests):
        for index, cli in enumerate(trade_wait_pair(request, args)):
            home = tmp_path / str(index) / "sai"
            connect(home, endpoint, monkeypatch)
            requests.clear()
            if update:
                preflight_reads = queue_update_preflight(replies)

                def index_receipt(
                    _: Request, preflight_reads: int = preflight_reads
                ) -> Reply:
                    key = submitted_key(requests[preflight_reads], kind)
                    return response(history(indexed_update(change, key, args)))

                replies.extend(
                    (
                        lambda request: accepted(submitted_key(request, kind), kind),
                        lambda _, preflight_reads=preflight_reads: response(
                            intent(
                                "filled",
                                kind=kind,
                                intentKey=submitted_key(
                                    requests[preflight_reads], kind
                                ),
                                body=UPDATES[args],
                            )
                        ),
                        index_receipt,
                    )
                )
            else:
                preflight_reads = filled_close(replies, requests, rust=index == 1)
            read_fd, write_fd = os.pipe()
            os.set_blocking(write_fd, False)
            try:
                while True:
                    os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                os.set_blocking(write_fd, True)
            try:
                with subprocess.Popen(
                    [*cli, *trade_wait_args(endpoint, args)],
                    env=cli_environment(home),
                    cwd=home.parent,
                    stdout=write_fd,
                    stderr=subprocess.PIPE,
                ) as process:
                    try:
                        end = time.monotonic() + 5
                        expected = preflight_reads + 3
                        while len(requests) < expected and time.monotonic() < end:
                            time.sleep(0.01)
                        assert len(requests) == expected
                        # Receiving the history request does not mean the receipt
                        # has reached stdout. Rust writes it from a worker thread.
                        threads = Path(f"/proc/{process.pid}/task")
                        if threads.is_dir():
                            while time.monotonic() < end:
                                with suppress(FileNotFoundError):
                                    if any(
                                        channel.read_text()
                                        .strip()
                                        .endswith("pipe_write")
                                        for channel in threads.glob("*/wchan")
                                    ):
                                        break
                                time.sleep(0.01)
                            else:
                                pytest.fail("receipt writer never blocked on stdout")
                        else:
                            time.sleep(0.15)
                        assert process.poll() is None
                        assert os.get_blocking(write_fd), (
                            "CLI changed the inherited pipe's flags"
                        )
                        process.send_signal(signal.SIGINT)
                        assert process.stderr is not None
                        assert select.select([process.stderr], [], [], 3)[0], (
                            "no stderr recovery with stdout blocked"
                        )
                        document = json.loads(process.stderr.readline())
                        if index == 1:
                            assert process.wait(timeout=3) == 1
                        # Let Python's final stdout flush finish after reading recovery.
                        os.set_blocking(read_fd, False)
                        while select.select([read_fd], [], [], 0)[0]:
                            if not os.read(read_fd, 65536):
                                break
                        assert process.wait(timeout=3) == 1
                    finally:
                        if process.poll() is None:
                            process.kill()
            finally:
                os.close(read_fd)
                os.close(write_fd)
            key = submitted_key(requests[preflight_reads], kind)
            assert document["intentKey"] == key
            assert document["interrupted"] is True
            assert document["localStatus"] == document["outcome"] == "terminal"
            assert document["receipt"] is None
            assert ledger_state(home) == [(key, "terminal", "terminal")]
            assert not replies
