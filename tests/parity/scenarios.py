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
NODE_INFO = "/cosmos/base/tendermint/v1beta1/node_info"
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
    if seed == "update-state":
        path = sai_home / "update.json"
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "latest": "sai-cli/v0.88.0",
                    "result": {"status": "failed", "to": "0.88.0", "rolledBack": True},
                    "notified": "failed:0.88.0",
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
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
    trade = {**position, "perpBorrowing": {**borrowing, "collateralToken": collateral}}
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
    responses["intentTradeHistory"] = responses["tradeHistory"]
    responses["waitUserIntents"] = responses["userIntents"]
    variables = {
        "tradeHistory": {"trader": ADDRESS},
        "intentTradeHistory": {"trader": ADDRESS, "limit": 25, "offset": 0},
        "waitUserIntents": {"user": ADDRESS, "key": INTENT, "includeAction": True},
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
            "user": position["trader"],
            "user_trade_index": "UserTradeIndex(7)",
            "market_index": market_index,
            "collateral_index": token_index,
            "is_open": True,
            "trade_type": "limit" if scenario == "pending-order" else "trade",
            "collateral_amount": str(position["collateralAmount"]),
            "leverage": str(position["leverage"]),
            "open_price": str(position["openPrice"]),
            "long": position["isLong"],
        },
        "get_exchange_balance": {
            "available_to_trade": "10000000",
            "collateral_index": token_index,
        },
        "is_after_hours_market": False,
        "get_collateral_price": "1",
        "get_perp_prices": {"market": {"base_per_quote": "100"}, "collateral": "1"},
        "get_liquidation_price": {"liquidation_price": "60"},
        "get_market": {"fee_index": "FeeIndex(0)", "group_index": "GroupIndex(0)"},
        "get_group": {"min_leverage": "1", "max_leverage": "100"},
        "get_pair_custom_max_leverage": "0",
        "get_fees": {
            "min_position_size_usd": str(market["minPositionSizeUSD"]),
            "open_fee_p": str(market["openFeePct"]),
            "close_fee_p": str(market["closeFeePct"]),
            "trigger_order_fee_p": "0",
        },
    }
    contract_args = {
        "get_trade_raw": {
            "trader": ADDRESS if scenario == "pending-order" else position["trader"],
            "index": 7,
        },
        "get_exchange_balance": {
            "user": position["trader"],
            "collateral_index": token_index,
        },
        "is_after_hours_market": {"market_index": market_index},
        "get_collateral_price": {"collateral_index": token_index},
        "get_market": {"index": market_index},
        "get_group": {"index": "GroupIndex(0)"},
        "get_pair_custom_max_leverage": {"index": borrowing["marketId"]},
        "get_perp_prices": {
            "market_index": market_index,
            "collateral_index": token_index,
        },
        "get_liquidation_price": {
            "trade_id": "UserTradeIndex(7)",
            "trader": position["trader"],
            "include_borrowing_fees": True,
        },
        "get_fees": {"index": "FeeIndex(0)"},
    }

    class Handler(BaseHTTPRequestHandler):
        def reply(
            self,
            value: object,
            status_code: int = 200,
            *,
            lcd: bool = False,
            next_page: bool = False,
        ) -> None:
            body = json.dumps(value).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            if lcd:
                self.send_header("grpc-metadata-x-cosmos-block-height", "120")
            self.send_header("Content-Length", str(len(body)))
            if next_page:
                self.send_header("Link", '<ignored>; rel="next"')
            self.end_headers()
            self.wfile.write(body)

        def reject(self, message: str) -> None:
            errors.append(message)
            self.reply({"errors": [{"message": message}]}, 400)

        def do_CONNECT(self) -> None:
            self.reject("Unexpected external HTTP connection")

        def admit_close(self, variables: Any) -> None:
            envelope = variables["input"]
            key = envelope["intentKey"]
            intent_stamp = envelope["intent"]["tsIntentInit"]
            lifecycle_stamp = envelope["lifecycle"]["tsLifecycleStep"]
            expected = PROTOCOL["close_admission_request"]
            expected = {
                "intentKey": key,
                "intent": {**expected["intent"], "tsIntentInit": intent_stamp},
                "lifecycle": {
                    **expected["lifecycle"],
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
            if not admission.release.wait(15):
                self.reject("Synthetic admission gate timed out")
                return
            ack = PROTOCOL["admission_ack"]["data"]["recordUserIntent"]
            self.reply({"data": {"recordUserIntent": {**ack, "intentKey": key}}})

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
                if name == "recordUserIntent" and scenario == "concurrent-close":
                    self.admit_close(request.variables)
                    return
                if name in responses:
                    if not same_variables(request.variables, variables.get(name, {})):
                        self.reject(f"Wrong Keeper variables for {name}")
                    elif scenario == "keeper-error":
                        self.reply({"errors": [{"message": "fixture unavailable"}]})
                    else:
                        self.reply(responses[name])
                    return
            except (ValueError, KeyError, TypeError):
                pass
            self.reject("Unsupported Keeper request")

        def do_HEAD(self) -> None:
            if (
                urlsplit(self.path).path
                != f"{scope}/SaiDotFun/open-sai/releases/latest"
            ):
                self.reject("Unsupported release redirect request")
                return
            wire.append(WireRequest("release-redirect"))
            path = f"{scope}/SaiDotFun/open-sai/releases"
            if scenario == "update-rate-limit-tag":
                path += "/tag/sai-cli/v0.99.0"
            self.send_response(302)
            self.send_header("Location", path)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def releases(self) -> None:
            query = parse_qs(urlsplit(self.path).query)
            if (
                query == {"per_page": ["100"], "page": ["2"]}
                and scenario == "update-pages"
            ):
                wire.append(WireRequest("releases-page-2"))
                self.reply([{"tag_name": "sai-cli/v0.99.0"}])
                return
            if query != {"per_page": ["100"], "page": ["1"]}:
                self.reject("Unexpected release-list page")
                return
            wire.append(WireRequest("releases"))
            if scenario.startswith("update-rate-limit-"):
                self.reply({}, 429)
            elif scenario == "update-bad-json":
                self.reply({"not": "a list"})
            elif scenario == "update-no-release":
                self.reply([{"tag_name": "docs/v99.0.0"}])
            elif scenario == "update-pages":
                self.reply(
                    [
                        {"tag_name": "sai-cli/v0.9.0"},
                        {"tag_name": "sai-cli/v9.0.0", "draft": True},
                        {"tag_name": "sai-cli/v8.0.0", "prerelease": True},
                        {"tag_name": "sai-cli/v7.0.0-rc.1"},
                        {"tag_name": "sai-cli/v06.0.0"},
                        {"tag_name": "other/v100.0.0"},
                    ],
                    next_page=True,
                )
            else:
                self.reply([{"tag_name": "sai-cli/v0.99.0"}])

        def do_GET(self) -> None:
            if not self.path.startswith("/"):
                self.reject("Unexpected external HTTP GET")
                return
            path = urlsplit(self.path).path
            if path.startswith(scope):
                path = path.removeprefix(scope)
                if path == NODE_INFO:
                    wire.append(WireRequest("node_info"))
                    self.reply({"default_node_info": {"network": "cataclysm-1"}})
                    return
                if path == RELEASES:
                    self.releases()
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
                        self.reply({"data": contracts[name]}, lcd=True)
                        return
                    except (ValueError, KeyError, TypeError):
                        pass
                self.reject("Unsupported LCD or release request")
            else:
                # Other tests can still have the old address of a reused port.
                self.reply({}, 404)

        def log_message(self, format: str, *args: object) -> None:
            pass

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
