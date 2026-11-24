"""Run with python3 scripts/release-rehearsal.py --self-test."""

import json
import runpy
import tempfile
import unittest
import urllib.request
from pathlib import Path

REHEARSAL = runpy.run_path(str(Path(__file__).with_name("release-rehearsal.py")))


class KeeperReplayTest(unittest.TestCase):
    def test_requires_the_original_intent_key_and_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            wheel = Path(temp) / "sai_cli-0.1.1-py3-none-any.whl"
            wheel.write_bytes(b"unused wheel")
            replay = REHEARSAL["Replay"](wheel, "0.1.1")
            key = "20261124T080000000Z_ABCDEFGHJKMNPQRS"
            replay.submitted.append(key)
            query = (
                "query($user:String!, $key:String) {"
                " userIntents(where:{user:$user, intentKey:$key}) { intentKey } }"
            )
            with REHEARSAL["keeper_server"](replay.handler()) as base:
                for text, user, intent, expected in (
                    (query, replay.addr.upper(), key, [key]),
                    (query, "0x" + "b" * 40, key, []),
                    (query, replay.addr, "wrong-key", []),
                    (query, replay.addr, None, []),
                    (
                        query.replace("intentKey:$key", 'intentKey:"wrong"'),
                        replay.addr,
                        key,
                        [],
                    ),
                ):
                    with self.subTest(user=user, key=intent, query=text):
                        request = urllib.request.Request(
                            f"{base}/query",
                            data=json.dumps(
                                {
                                    "query": text,
                                    "variables": {"user": user, "key": intent},
                                }
                            ).encode(),
                            headers={"Content-Type": "application/json"},
                        )
                        with urllib.request.urlopen(request, timeout=2) as response:
                            rows = json.load(response)["data"]["userIntents"]
                        self.assertEqual([row["intentKey"] for row in rows], expected)
