"""Set up isolated CLI homes and replay fixtures on loopback."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from compare import INTENT_ID, timestamp
from keeper_wire import WireRequest, graphql_request, same_variables
from sai_cli.tests.loopback import fixture, graphql_ws, http_server
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROTOCOL = fixture("synthetic_protocol.json")
ADDRESS = PROTOCOL["address"]
OTHER_ADDRESS = "0x" + "2" * 40
INTENT = "parity-close-7"
CONTRACT = "nibi1syntheticfixture"
TRADE_REQUEST_HASH = "AB" * 32
NODE_INFO = "/cosmos/base/tendermint/v1beta1/node_info"
HEIGHT_HEADER = "x-cosmos-block-height"
POSITION_HEIGHT = "120"
POSITION_SCENARIOS = {
    "position-precision",
    "position-after-hours",
    "position-index-lag",
    "position-wrong-chain",
    "position-foreign-trader",
    "position-no-oracle",
    "position-wrong-height",
    "partial-preview-changed",
    "margin-accepted",
    "margin-rejected",
    "margin-unknown",
    "partial-accepted",
    "partial-unknown",
}
MARGIN_INTENT = {
    "kind": "UPDATE_TRADE",
    "target": {"userTradeId": "7"},
    "body": {
        "takeProfit": {"operation": "keep"},
        "stopLoss": {"operation": "keep"},
        "requestedLeverage": "1.818181818181818182",
    },
}
PARTIAL_INTENT = {
    "kind": "DECREASE_POSITION_SIZE",
    "target": {"userTradeId": "7"},
    "body": {
        "collateral": {"amountUsdc": "5000000", "amountUsd": "5"},
        "leverageDelta": "0",
    },
}
RELEASES = "/repos/SaiDotFun/open-sai/releases"
CONFIG = """# Keep this comment when editing
[update]
auto   =  true # Keep this inline comment

[logs]
keep_days  =  3
"""
# Use the same public storage operations as the CLI tests, in a subprocess
# so the driver's own environment and storage never change.
SEED = """
import json
import sys
from sai_cli.core import storage

data = json.load(sys.stdin)
for session in data["sessions"]:
    storage.save_session(session)
if data["ledger"]:
    storage.pending(data["key"], data["endpoint"], data["address"], "CLOSE_POSITION", "7")
    with storage.db() as conn:
        conn.execute("UPDATE intents SET created_at=946684800")
"""


@dataclass
class Admission:
    arrived: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    key: str = ""


@dataclass
class Endpoints:
    http: str
    ws: str
    errors: list[str] = field(default_factory=list)
    wire: list[WireRequest] = field(default_factory=list)
    admission: Admission = field(default_factory=Admission)

    @property
    def keeper(self) -> str:
        return self.http + "/query"

    def expand(self, value: str) -> str:
        for key, replacement in {
            "{addr}": ADDRESS,
            "{other_addr}": OTHER_ADDRESS,
            "{intent}": INTENT,
            "{keeper}": self.keeper,
            "{http}": self.http,
            "{ws}": self.ws,
        }.items():
            value = value.replace(key, replacement)
        return value


def child_env(home: Path, endpoints: Endpoints) -> dict[str, str]:
    # Construct the environment instead of forwarding credentials, local
    # client configuration, or proxy settings from the parent.
    return {
        "HOME": str(home),
        "SAI_HOME": str(home / "sai"),
        "SAI_ENDPOINT": endpoints.keeper,
        "SAI_WS_ENDPOINT": endpoints.ws,
        "SAI_LCD_ENDPOINT": endpoints.http,
        "UV_PROJECT_ENVIRONMENT": sys.prefix,
        "UV_NO_CONFIG": "1",
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.defpath]),
        "CI": "1",
        "TERM": "dumb",
        "TZ": "UTC",
        "LC_ALL": "C",
        "PYTHONUTF8": "1",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }


def seed_home(home: Path, endpoints: Endpoints, seed: str) -> None:
    home.mkdir(mode=0o700)
    sai_home = home / "sai"
    sai_home.mkdir(mode=0o700)
    if seed == "empty":
        return
    if seed == "legacy":
        shutil.copytree(HERE / "v0.1.0", sai_home, dirs_exist_ok=True)
        sai_home.chmod(0o700)
        (sai_home / "trader").chmod(0o700)
        for name in ("config.toml", "trader/sessions.json"):
            (sai_home / name).chmod(0o600)
        return
    if seed in {"config", "config-logs-disabled"}:
        path = sai_home / "config.toml"
        text = CONFIG if seed == "config" else "[logs]\nenabled = false\n"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return
    session = PROTOCOL["session_response"]["data"]["createUserIntentSession"]
    sessions = []
    if seed in {"session", "accounts"}:
        sessions.append({**session, "endpoint": endpoints.keeper})
        if seed == "accounts":
            sessions.append({**sessions[0], "evmAddress": OTHER_ADDRESS})
    subprocess.run(
        [sys.executable, "-c", SEED],
        input=json.dumps(
            {
                "sessions": sessions,
                "ledger": seed == "ledger",
                "key": INTENT,
                "endpoint": endpoints.keeper,
                "address": ADDRESS,
            }
        ),
        env=child_env(home, endpoints),
        cwd=home,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )


@contextmanager
def loopback(scenario: str) -> Generator[Endpoints]:
    errors: list[str] = []
    wire: list[WireRequest] = []
    admission = Admission()
    scope = f"/parity/{uuid.uuid4().hex}"
    markets = fixture("mainnet_markets_http.json")
    prices = fixture("mainnet_prices_http.json")
    position = PROTOCOL["account_positions_response"]["data"]["perp"]["trades"][0]
    borrowing = position["perpBorrowing"]
    recorded_markets = markets["data"]["perp"]["borrowings"]
    market = {**recorded_markets[0], "marketId": borrowing["marketId"]}
    recorded_markets.append(market)
    collateral = market["collateralToken"]
    trader = OTHER_ADDRESS if scenario == "position-foreign-trader" else ADDRESS.upper()
    trade = {
        **position,
        "trader": trader,
        "perpBorrowing": {**borrowing, "collateralToken": collateral},
    }
    status = PROTOCOL["intent_status_response"]["data"]["userIntents"][0]
    history = {
        "id": 7,
        "intentKey": INTENT,
        "txHash": status["txHash"],
        "trade": {"id": 7, "trader": ADDRESS},
    }
    responses = {
        "oneClickTrading": {
            "data": {
                "oneClickTrading": {
                    "enabled": True,
                    "chainId": "cataclysm-1",
                    "contractAddress": CONTRACT,
                }
            }
        },
        "borrowings": markets,
        "borrowing": {"data": {"perp": {"borrowing": market}}},
        "tokenPricesUsd": prices,
        "tradeHistory": {"data": {"perp": {"tradeHistory": [history]}}},
        "trades": PROTOCOL["account_positions_response"],
        "trade": {"data": {"perp": {"trade": trade}}},
        "userIntents": {
            "data": {
                "userIntents": (
                    []
                    if scenario == "absent"
                    else [{**status, "intentKey": INTENT, "nibiAddress": None}]
                )
            }
        },
    }
    variables = {
        "tradeHistory": {"trader": ADDRESS},
        "trades": {"trader": ADDRESS},
        "trade": {"trader": ADDRESS, "id": 7},
        "borrowing": {"market": borrowing["marketId"], "collateral": collateral["id"]},
        "userIntents": {"user": ADDRESS, "key": INTENT},
    }
    queue_payloads = {
        "userBalances": PROTOCOL["account_balance_ws_payload"],
        "perpTrades": {"data": {"perpTrades": [position]}},
        "tokenPricesUsd": fixture("mainnet_price_ws_payload.json"),
    }
    market_index = f"MarketIndex({borrowing['marketId']})"
    token_index = f"TokenIndex({collateral['id']})"
    contracts = {
        "get_trade_raw": {
            "user": trader,
            "user_trade_index": "UserTradeIndex(7)",
            "market_index": market_index,
            "collateral_index": token_index,
            "is_open": True,
            "trade_type": "trade",
            "collateral_amount": str(position["collateralAmount"]),
            "leverage": str(position["leverage"]),
        },
        "get_exchange_balance": {
            "available_to_trade": "10000000",
            "collateral_index": token_index,
        },
        "is_after_hours_market": False,
        "get_collateral_price": "1",
    }
    if scenario in {"position-precision", "position-after-hours"}:
        trade.update(collateralAmount=10000003, leverage=2.1234567890123457)
        contracts["get_trade_raw"].update(
            collateral_amount="10000003", leverage="2.123456789012345678"
        )
        contracts["get_collateral_price"] = "0.987654321098765432"
    if scenario == "position-index-lag":
        contracts["get_trade_raw"]["collateral_amount"] = "10000001"
    if scenario == "position-after-hours":
        contracts.update(
            is_after_hours_market=True,
            get_market={"block_last_opened": 110, "block_last_closed": 100},
            get_trade_infos=[{"created_block": 90}],
            get_after_hours_leverage="1.234567890123456789",
        )
    contract_args = {
        "get_trade_raw": {"trader": trader, "index": 7},
        "get_exchange_balance": {
            "user": trader,
            "collateral_index": token_index,
        },
        "is_after_hours_market": {"market_index": market_index},
        "get_collateral_price": {"collateral_index": token_index},
        "get_market": {"index": market_index},
        "get_trade_infos": {
            "trader": trader,
            "start_idx": 7,
            "scan_limit": 1,
        },
        "get_after_hours_leverage": {},
    }
    close_admission = PROTOCOL["close_admission_request"]
    expected_intent = {
        "concurrent-close": close_admission["intent"],
        "margin-accepted": MARGIN_INTENT,
        "margin-rejected": MARGIN_INTENT,
        "margin-unknown": MARGIN_INTENT,
        "position-foreign-trader": MARGIN_INTENT,
        "partial-accepted": PARTIAL_INTENT,
        "partial-unknown": PARTIAL_INTENT,
    }.get(scenario)

    class Handler(BaseHTTPRequestHandler):
        def reply(
            self, value: object, status_code: int = 200, *, height: str | None = None
        ) -> None:
            body = json.dumps(value).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if height is not None:
                self.send_header(f"grpc-metadata-{HEIGHT_HEADER}", height)
            self.end_headers()
            self.wfile.write(body)

        def reject(self, message: str) -> None:
            errors.append(message)
            self.reply({"errors": [{"message": message}]}, 400)

        def do_CONNECT(self) -> None:
            self.reject("Unexpected external HTTP connection")

        def admit(self, variables: Any, intent: dict[str, Any]) -> None:
            envelope = variables["input"]
            key = envelope["intentKey"]
            intent_stamp = envelope["intent"]["tsIntentInit"]
            lifecycle_stamp = envelope["lifecycle"]["tsLifecycleStep"]
            expected = {
                "intentKey": key,
                "intent": {
                    "intentSchemaVersion": "v0",
                    **intent,
                    "tsIntentInit": intent_stamp,
                },
                "lifecycle": {
                    **close_admission["lifecycle"],
                    "tsLifecycleStep": lifecycle_stamp,
                },
            }
            token = PROTOCOL["session_response"]["data"]["createUserIntentSession"][
                "token"
            ]
            if (
                not same_variables(variables, {"input": expected})
                or not INTENT_ID.fullmatch(key)
                or not timestamp(intent_stamp)
                or intent_stamp != lifecycle_stamp
                or self.headers.get("Authorization") != f"Bearer {token}"
                or admission.arrived.is_set()
            ):
                self.reject("Unexpected synthetic admission request")
                return
            admission.key = key
            admission.arrived.set()
            if scenario == "concurrent-close" and not admission.release.wait(15):
                self.reject("Synthetic admission gate timed out")
                return
            if scenario == "margin-rejected":
                self.reply(
                    {
                        "errors": [
                            {
                                "message": "fixture rejected",
                                "extensions": {"code": "INTENT_NOT_ACCEPTED"},
                            }
                        ]
                    }
                )
                return
            ack = PROTOCOL["admission_ack"]["data"]["recordUserIntent"]
            self.reply(
                {
                    "data": {
                        "recordUserIntent": {
                            **ack,
                            "intentKey": "wrong-fixture-key"
                            if scenario.endswith("-unknown")
                            else key,
                            "kind": intent["kind"],
                            "txHash": "A" * 64
                            if scenario.endswith("-accepted")
                            else None,
                        }
                    }
                }
            )

        def admit_trade(self, variables: Any) -> None:
            session = PROTOCOL["session_response"]["data"]["createUserIntentSession"]
            if self.headers.get("Authorization") != f"Bearer {session['token']}":
                self.reject("Wrong fixture authorization")
                return
            key = variables["input"]["intentKey"]
            if scenario == "trade-disconnect":
                self.close_connection = True
                return
            if scenario == "trade-rejected":
                self.reply(
                    {"errors": [{"extensions": {"code": "INTENT_NOT_ACCEPTED"}}]}
                )
                return
            ack = PROTOCOL["admission_ack"]["data"]["recordUserIntent"]
            if scenario == "trade-bad-ack":
                key = "wrong-intent"
            self.reply(
                {
                    "data": {
                        "recordUserIntent": {
                            **ack,
                            "intentKey": key,
                            "txHash": TRADE_REQUEST_HASH,
                        }
                    }
                }
            )

        def do_POST(self) -> None:
            if not self.path.startswith("/") or (
                self.path.startswith(scope) and self.path != f"{scope}/query"
            ):
                self.reject("Unexpected HTTP POST")
                return
            if self.path != f"{scope}/query":
                self.reply({}, 404)
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                try:
                    request = graphql_request(body)
                except ValueError as exc:
                    self.reject(str(exc))
                    return
                wire.append(request)
                name = request.label
                if name == "recordUserIntent" and expected_intent is not None:
                    self.admit(request.variables, expected_intent)
                    return
                if name == "recordUserIntent" and scenario.startswith("trade-"):
                    self.admit_trade(request.variables)
                    return
                if name in responses:
                    if not same_variables(request.variables, variables.get(name, {})):
                        self.reject(f"Wrong Keeper variables for {name}")
                    elif scenario == "keeper-error":
                        self.reply({"errors": [{"message": "fixture unavailable"}]})
                    elif name == "trade" and scenario == "partial-preview-changed":
                        self.reply(
                            {
                                "data": {
                                    "perp": {
                                        "trade": {
                                            **trade,
                                            "leverage": self.position_leverage(),
                                        }
                                    }
                                }
                            }
                        )
                    else:
                        self.reply(responses[name])
                    return
            except (ValueError, KeyError, TypeError):
                pass
            self.reject("Unsupported Keeper request")

        def do_GET(self) -> None:
            if not self.path.startswith("/"):
                self.reject("Unexpected external HTTP GET")
                return
            url = urlsplit(self.path)
            path = url.path
            if path.startswith(scope):
                path = path.removeprefix(scope)
                if path == NODE_INFO:
                    wire.append(WireRequest("node_info"))
                    chain = (
                        "wrong-fixture-chain"
                        if scenario == "position-wrong-chain"
                        else "cataclysm-1"
                    )
                    self.reply({"default_node_info": {"network": chain}})
                    return
                if path == RELEASES:
                    wire.append(WireRequest("releases"))
                    self.reply(
                        [
                            {
                                "tag_name": "sai-cli/v0.99.0",
                                "draft": False,
                                "prerelease": False,
                            }
                        ]
                    )
                    return
                prefix = f"/cosmwasm/wasm/v1/contract/{CONTRACT}/smart/"
                if path.startswith(prefix):
                    try:
                        query = json.loads(
                            base64.b64decode(unquote(path.removeprefix(prefix)))
                        )
                        [name] = query
                        if not same_variables(query, {name: contract_args[name]}):
                            self.reject(f"Wrong LCD variables for {name}")
                            return
                        wire.append(WireRequest(name, variables=query))
                        if scenario in POSITION_SCENARIOS:
                            self.position_query(name, url.query)
                        else:
                            self.reply({"data": contracts[name]})
                        return
                    except (ValueError, KeyError, TypeError):
                        pass
                self.reject("Unsupported LCD or release request")
            else:
                # Other tests can still have the old address of a reused port.
                self.reply({}, 404)

        def log_message(self, format: str, *args: object) -> None:
            pass

        def position_leverage(self) -> float:
            return 3.0 if sum(request.label == "trade" for request in wire) > 1 else 2.0

        def position_query(self, name: str, query: str) -> None:
            latest = max(i for i, item in enumerate(wire) if item.label == "trade")
            snapshot = [item.label for item in wire[latest + 1 :]]
            final_position = snapshot.count("get_trade_raw") == 2
            pinned = final_position and name != "get_trade_raw"
            fresh = pinned or final_position or name == "get_collateral_price"
            height = self.headers.get(HEIGHT_HEADER)
            if height != (POSITION_HEIGHT if pinned else None):
                self.reject(f"Wrong LCD height for {name}")
                return
            if ("_sai_query" in parse_qs(query)) != fresh:
                self.reject(f"Wrong LCD cache-busting query for {name}")
                return
            if scenario == "position-no-oracle" and name == "get_collateral_price":
                self.reply({}, 503)
                return
            data = contracts[name]
            if scenario == "partial-preview-changed" and name == "get_trade_raw":
                data = {**data, "leverage": str(self.position_leverage())}
            served = (
                "121"
                if scenario == "position-wrong-height" and pinned
                else POSITION_HEIGHT
            )
            self.reply({"data": data}, height=served)

    def websocket(connection: ServerConnection) -> None:
        try:
            if (
                connection.request is None
                or connection.request.path != f"{scope}/query"
            ):
                connection.close()
                return
            initial = json.loads(connection.recv(timeout=5))
            if initial["type"] != "connection_init":
                errors.append("Expected WebSocket connection_init")
                return
            wire.append(WireRequest("ws:init"))
            connection.send(json.dumps({"type": "connection_ack"}))
            subscription = json.loads(connection.recv(timeout=5))
            if subscription["type"] != "subscribe":
                errors.append("Expected WebSocket subscribe")
                return
            request_id = subscription["id"]
            try:
                request = graphql_request(subscription["payload"], websocket=True)
                wire.append(request)
                name = request.label.removeprefix("ws:")
                expected = {"id": 3} if name == "tokenPricesUsd" else {"user": ADDRESS}
                if not same_variables(request.variables, expected):
                    raise ValueError(f"Wrong WebSocket variables for {name}")
            except ValueError as exc:
                errors.append(str(exc))
                connection.send(
                    json.dumps(
                        {
                            "id": request_id,
                            "type": "error",
                            "payload": [{"message": str(exc)}],
                        }
                    )
                )
                return
            payload = queue_payloads[name]
            connection.send(
                json.dumps({"id": request_id, "type": "next", "payload": payload})
            )
            if scenario == "watch":
                connection.send(json.dumps({"id": request_id, "type": "complete"}))
        except ConnectionClosed:
            # Doctor only needs the handshake, and balances closes after its snapshot.
            pass
        except (ValueError, KeyError, StopIteration, TimeoutError, TypeError):
            errors.append("Unsupported Keeper WebSocket exchange")

    with http_server(Handler) as http, graphql_ws(websocket) as ws:
        yield Endpoints(
            http + scope,
            ws.removesuffix("/query") + scope + "/query",
            errors,
            wire,
            admission,
        )
