"""Check the Keeper replay over HTTP, with a fresh connection per attempt."""

import json
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

from scenarios import ADDRESS, loopback
from trade_replay import SESSION, TradeReplay

FIRST = "20261124T120000000Z_0123456789ABCDEF"
SECOND = "20261124T120001000Z_0123456789ABCDEF"
STAMP = "2026-11-24T12:00:00.000Z"
OPERATIONS = json.loads(Path(__file__).with_name("keeper-requests.json").read_text())


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


def report(key: str, body: object, stamp: str = STAMP) -> dict:
    return {
        "input": {
            "intentKey": key,
            "intent": {
                "intentSchemaVersion": "v0",
                "tsIntentInit": stamp,
                "kind": "CLOSE_POSITION",
                "target": {"userTradeId": "7"},
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
