"""Check the Keeper replay over HTTP, with a fresh connection per attempt."""

import json
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from scenarios import ADDRESS, loopback
from trade_replay import SESSION, TradeReplay

FIRST = "20261124T120000000Z_0123456789ABCDEF"
SECOND = "20261124T120001000Z_0123456789ABCDEF"
STAMP = "2026-11-24T12:00:00.000Z"
OPERATIONS = json.loads(Path(__file__).with_name("keeper-requests.json").read_text())
OPEN_BODY = {
    "market": {"marketIndex": "MarketIndex(3)"},
    "side": "long",
    "orderType": "market",
    "amountUsd": "20",
    "intentPrice": "100",
    "leverage": "2",
    "maxSlippagePct": "1",
}
KEEP = {"operation": "keep"}
UPDATE_BODY = {"takeProfit": KEEP, "stopLoss": KEEP, "requestedLeverage": "2"}
PARTIAL_BODY = {
    "collateral": {"amountUsdc": "5000000", "amountUsd": "5"},
    "leverageDelta": "0",
}


def post(endpoint: str, operation: str, variables: dict, *, authenticated: bool):
    url = urlsplit(endpoint)
    connection = HTTPConnection(url.netloc, timeout=3)
    headers = {"Authorization": f"Bearer {SESSION['token']}"} if authenticated else {}
    try:
        connection.request(
            "POST",
            url.path,
            body=json.dumps({"query": OPERATIONS[operation], "variables": variables}),
            headers=headers,
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def report(
    key: str, body: object, stamp: str = STAMP, *, kind: str = "CLOSE_POSITION"
) -> dict:
    return {
        "input": {
            "intentKey": key,
            "intent": {
                "intentSchemaVersion": "v0",
                "tsIntentInit": stamp,
                "kind": kind,
                **({"target": {"userTradeId": "7"}} if kind != "OPEN_TRADE" else {}),
                "body": body,
            },
            "lifecycle": {
                "sequence": 1,
                "lifecycleStep": "jwt_pass",
                "tsLifecycleStep": stamp,
            },
        },
    }


def test_rejects_invalid_body_before_admission():
    replay = TradeReplay(phase="pending")
    with loopback("default", trade_replay=replay) as endpoints:
        status, result = post(
            endpoints.keeper,
            "recordUserIntent",
            report(FIRST, {"unsupported": True}),
            authenticated=True,
        )
        assert status == 400
        assert "body must be empty" in result["errors"][0]["message"]
        assert replay.attempts() == {FIRST: 1}
        assert replay.admitted_keys() == set()
        assert not replay.arrived.is_set()
        assert post(
            endpoints.keeper,
            "userIntents",
            {"user": ADDRESS, "key": FIRST},
            authenticated=False,
        ) == (200, {"data": {"userIntents": []}})


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("OPEN_TRADE", {key: val for key, val in OPEN_BODY.items() if key != "market"}),
        ("OPEN_TRADE", {**OPEN_BODY, "market": {}}),
        ("OPEN_TRADE", {**OPEN_BODY, "market": {"marketIndex": "MarketIndex(-1)"}}),
        (
            "OPEN_TRADE",
            {**OPEN_BODY, "market": {"marketIndex": f"MarketIndex({2**64})"}},
        ),
        ("OPEN_TRADE", {**OPEN_BODY, "amountUsd": "twenty"}),
        ("OPEN_TRADE", {**OPEN_BODY, "amountUsd": "1.0000001"}),
        ("OPEN_TRADE", {**OPEN_BODY, "intentPrice": "0"}),
        ("OPEN_TRADE", {**OPEN_BODY, "leverage": str(2**128)}),
        ("OPEN_TRADE", {**OPEN_BODY, "maxSlippagePct": "100.001"}),
        ("OPEN_TRADE", {**OPEN_BODY, "maxSlippagePct": "0.00000000000000001"}),
        ("OPEN_TRADE", {**OPEN_BODY, "stopLoss": 50}),
        ("CANCEL_PENDING_TRADE", {"invalid": True}),
        ("UPDATE_TRADE", {"takeProfit": KEEP, "stopLoss": KEEP}),
        ("UPDATE_TRADE", {**UPDATE_BODY, "requestedLeverage": "0"}),
        ("UPDATE_TRADE", {**UPDATE_BODY, "stopLoss": {"operation": "clear"}}),
        (
            "UPDATE_TRADE",
            {"takeProfit": {"operation": "set", "value": "abc"}, "stopLoss": KEEP},
        ),
        ("DECREASE_POSITION_SIZE", {"sizeDelta": "1", "leverageDelta": "0"}),
        ("DECREASE_POSITION_SIZE", {"collateral": PARTIAL_BODY["collateral"]}),
        (
            "DECREASE_POSITION_SIZE",
            {**PARTIAL_BODY, "collateral": {"amountUsdc": "5000000", "amountUsd": "6"}},
        ),
        (
            "DECREASE_POSITION_SIZE",
            {
                **PARTIAL_BODY,
                "collateral": {"amountUsdc": str(2**128), "amountUsd": "5"},
            },
        ),
    ],
)
def test_rejects_invalid_action_before_admission(kind, body):
    replay = TradeReplay(kind=kind, entity="" if kind == "OPEN_TRADE" else "7")
    with loopback("default", trade_replay=replay) as endpoints:
        status, _ = post(
            endpoints.keeper,
            "recordUserIntent",
            report(FIRST, body, kind=kind),
            authenticated=True,
        )
        assert status == 400
        assert replay.attempts() == {FIRST: 1}
        assert not replay.admitted_keys()
        assert not replay.arrived.is_set()


@pytest.mark.parametrize(
    "fields",
    [{"evmAddress": "0x" + "f" * 40}, {"signedTx": ""}, {"execActor": "server"}],
)
def test_rejects_invalid_handoff_before_admission(fields):
    replay = TradeReplay()
    variables = report(FIRST, {})
    variables["input"].update(fields)
    with loopback("default", trade_replay=replay) as endpoints:
        status, _ = post(
            endpoints.keeper, "recordUserIntent", variables, authenticated=True
        )
        assert status == 400
        assert replay.attempts() == {FIRST: 1}
        assert not replay.admitted_keys()


@pytest.mark.parametrize(
    "fields",
    [
        {"tsIntentInit": "2026-11-24T12:00Z"},
        {"blockTsIntentInit": "2026-11-24T12:00Z"},
        {"blockIntentInit": 2**63},
        {"execActor": "server"},
    ],
)
def test_rejects_invalid_intent_input_before_admission(fields):
    replay = TradeReplay()
    variables = report(FIRST, {}, fields.get("tsIntentInit", STAMP))
    variables["input"]["intent"].update(fields)
    with loopback("default", trade_replay=replay) as endpoints:
        status, _ = post(
            endpoints.keeper, "recordUserIntent", variables, authenticated=True
        )
        assert status == 400
        assert replay.attempts() == {FIRST: 1}
        assert not replay.admitted_keys()


def test_replays_identical_report_and_admits_distinct_intent():
    replay = TradeReplay(phase="pending")
    with loopback("default", trade_replay=replay) as endpoints:
        responses = [
            post(endpoints.keeper, "recordUserIntent", variables, authenticated=True)
            for variables in (report(FIRST, {}), report(FIRST, {}), report(SECOND, {}))
        ]
        assert [status for status, _ in responses] == [200, 200, 200]
        assert responses[0] == responses[1]
        assert replay.attempts() == {FIRST: 2, SECOND: 1}
        assert replay.admitted_keys() == {FIRST, SECOND}
        conflict, _ = post(
            endpoints.keeper,
            "recordUserIntent",
            report(FIRST, {}, "2026-11-24T12:00:01.000Z"),
            authenticated=True,
        )
        assert conflict == 400
        assert replay.attempts() == {FIRST: 3, SECOND: 1}
        assert replay.admitted_keys() == {FIRST, SECOND}
        assert replay.intent(FIRST)["tsIntentInit"] == STAMP
        for key in (FIRST, SECOND):
            status, result = post(
                endpoints.keeper,
                "userIntents",
                {"user": ADDRESS, "key": key},
                authenticated=False,
            )
            assert status == 200
            assert [row["intentKey"] for row in result["data"]["userIntents"]] == [key]
