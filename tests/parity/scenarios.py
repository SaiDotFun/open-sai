"""Set up isolated CLI homes and replay fixtures on loopback."""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from compare import INTENT_ID, timestamp
from keeper_wire import WireRequest, graphql_request, same_variables
from sai_cli.tests.loopback import fixture, graphql_ws, http_server
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROTOCOL = fixture("synthetic_protocol.json")
ADDRESS = PROTOCOL["address"]
OTHER_ADDRESS = "0x" + "2" * 40
INTENT = "parity-close-7"
HISTORY_SESSION_TOKEN = "e7" * 32
CONTRACT = "nibi1syntheticfixture"
TRADE_REQUEST_HASH = "AB" * 32
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
    near_expiry: str = field(
        default_factory=lambda: (
            datetime.now(UTC) + timedelta(hours=50, minutes=30)
        ).isoformat()
    )
    interrupt_ready: threading.Event = field(default_factory=threading.Event)
    interrupt_closed: threading.Event = field(default_factory=threading.Event)
    keeper_path: str = "/query"

    @property
    def keeper(self) -> str:
        return self.http + self.keeper_path

    def expand(self, value: str) -> str:
        for key, replacement in {
            "{addr}": ADDRESS,
            "{other_addr}": OTHER_ADDRESS,
            "{intent}": INTENT,
            "{keeper}": self.keeper,
            "{http}": self.http,
            "{ws}": self.ws,
            "{ws_http}": self.ws.replace("ws://", "http://", 1),
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
    if seed == "doctor-malformed-toml":
        config = sai_home / "config.toml"
        config.write_text("[keeper\n")
        config.chmod(0o600)
        return
    if seed == "doctor-invalid-endpoint":
        config = sai_home / "config.toml"
        config.write_text('[keeper]\nendpoint = "https://fixture.invalid:bad/api"\n')
        config.chmod(0o600)
        return
    if seed == "doctor-ledger-v0":
        trader = sai_home / "trader"
        trader.mkdir(mode=0o700)
        with sqlite3.connect(path := trader / "intents.sqlite3") as conn:
            conn.execute(
                "CREATE TABLE intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL,"
                " account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,"
                " entity TEXT NOT NULL)"
            )
        path.chmod(0o600)
        return
    if seed == "doctor-loose-profile":
        sai_home.chmod(0o750)
        config = sai_home / "config.toml"
        config.write_text("[logs]\nenabled = false\n")
        config.chmod(0o600)
        trader = sai_home / "trader"
        trader.mkdir(mode=0o755)
        history = trader / "history"
        history.touch(mode=0o644)
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
    if seed in {
        "session",
        "accounts",
        "doctor-near-expiry",
        "doctor-ledger-session",
        "history-token",
    }:
        sessions.append({**session, "endpoint": endpoints.keeper})
        if seed == "doctor-near-expiry":
            sessions[0].update(expiresAt=endpoints.near_expiry, keyName="fixture-key")
        if seed == "history-token":
            sessions[0]["token"] = HISTORY_SESSION_TOKEN
        if seed == "accounts":
            sessions.append({**sessions[0], "evmAddress": OTHER_ADDRESS})
    env = child_env(home, endpoints)
    if seed in {"doctor-near-expiry", "doctor-ledger-session"}:
        env["SAI_LOG"] = "false"
    if seed == "doctor-ledger-session":
        config = sai_home / "config.toml"
        config.write_text("[logs]\nenabled = false\n")
        config.chmod(0o600)
    subprocess.run(
        [sys.executable, "-c", SEED],
        input=json.dumps(
            {
                "sessions": sessions,
                "ledger": seed in {"ledger", "doctor-ledger-session"},
                "key": INTENT,
                "endpoint": endpoints.keeper,
                "address": ADDRESS,
            }
        ),
        env=env,
        cwd=home,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )


@contextmanager
def loopback(
    scenario: str,
    *,
    market_price: float | None = None,
    cancel_target: str | None = None,
    close_target: str | None = None,
) -> Generator[Endpoints]:
    errors: list[str] = []
    wire: list[WireRequest] = []
    admission = Admission()
    interrupt_ready, interrupt_closed = threading.Event(), threading.Event()
    scope = f"/parity/{uuid.uuid4().hex}"
    keeper_path = (
        "/query/fixture-path-key?key=fixture-query-key"
        if scenario == "keeper-echo-error"
        else "/query"
    )
    markets = fixture("mainnet_markets_http.json")
    prices = fixture("mainnet_prices_http.json")
    position = PROTOCOL["account_positions_response"]["data"]["perp"]["trades"][0]
    borrowing = position["perpBorrowing"]
    recorded_markets = markets["data"]["perp"]["borrowings"]
    market = {**recorded_markets[0], "marketId": borrowing["marketId"]}
    if market_price is not None:
        market["price"] = market_price
    recorded_markets.append(market)
    collateral = market["collateralToken"]
    trade = {**position, "perpBorrowing": {**borrowing, "collateralToken": collateral}}
    if close_target == "keeper-owner":
        trade["trader"] = OTHER_ADDRESS
    if close_target == "keeper-index":
        trade["id"] = 8
    status = PROTOCOL["intent_status_response"]["data"]["userIntents"][0]
    history = {
        "id": 7,
        "intentKey": INTENT,
        "txHash": status["txHash"],
        "trade": {"id": 7, "trader": ADDRESS},
    }
    chains = {"unknown-chain": None, "empty-chain": ""}
    chain = chains.get(cancel_target, "cataclysm-1")
    network = "different-chain" if cancel_target == "missing-contract" else chain
    enabled = 1 if cancel_target == "non-boolean-route" else True
    contract = "" if cancel_target == "missing-contract" else CONTRACT
    responses = {
        "oneClickTrading": {
            "data": {
                "oneClickTrading": {
                    "enabled": enabled,
                    "chainId": chain,
                    "contractAddress": contract,
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
    if scenario == "history-projection":
        responses["tradeHistory"] = {
            "data": {
                "unexpected": HISTORY_SESSION_TOKEN,
                "perp": {
                    "unexpected": HISTORY_SESSION_TOKEN,
                    "tradeHistory": [
                        {
                            "id": "001-23:4",
                            "intentKey": "20261120T102030123Z_0123456789ABCDEF",
                            "tradeChangeType": "position_liquidated",
                            "txHash": "0X" + "AB" * 32,
                            "evmTxHash": "unvalidated",
                            "trade": {"id": 7, "trader": ADDRESS, "unexpected": "omit"},
                            "block": {
                                "block": 42,
                                "block_ts": "2026-11-20T15:43:31.123456789+23:59",
                                "unexpected": "omit",
                            },
                        },
                        {
                            "id": 2**63,
                            "txHash": HISTORY_SESSION_TOKEN,
                            "tradeChangeType": "unknown",
                            "trade": {"id": True, "trader": "invalid"},
                            "block": {"block": -1, "block_ts": "2026-11-20 15:43:31Z"},
                        },
                        {"block": {"block_ts": "2026-11-20T15:43:60Z"}},
                        {"block": {"block_ts": "0000-01-01T00:00:00Z"}},
                        {"block": {"block_ts": None}},
                        None,
                        "skip non-objects",
                        {"unexpected": "preserve the empty projected row"},
                    ],
                },
            },
        }
    elif scenario == "history-null":
        responses["tradeHistory"] = {
            "data": {"perp": {"tradeHistory": None, "extra": 1}}
        }
    elif scenario == "history-null-perp":
        responses["tradeHistory"] = {"data": {"perp": None, "extra": 1}}
    elif scenario == "history-invalid":
        responses["tradeHistory"] = {
            "data": {"perp": {"tradeHistory": True}, "extra": 1}
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
    cancel_trader = OTHER_ADDRESS if cancel_target == "other-account" else ADDRESS
    pending_order = scenario == "pending-order" or cancel_target in {
        "pending",
        "foreign-owner",
        "unknown-chain",
        "empty-chain",
    }
    contracts = {
        "get_trade_raw": {
            "user": (
                OTHER_ADDRESS
                if cancel_target in {"foreign-owner", "filled-foreign-owner"}
                or close_target == "contract-owner"
                else ADDRESS
                if cancel_target is not None or pending_order
                else position["trader"]
            ),
            "user_trade_index": (
                "UserTradeIndex(8)"
                if close_target == "contract-index"
                else "UserTradeIndex(7)"
            ),
            "market_index": market_index,
            "collateral_index": token_index,
            "is_open": True,
            "trade_type": (
                "stop"
                if cancel_target == "foreign-owner"
                else "limit"
                if pending_order
                else "trade"
            ),
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
            "trader": (
                cancel_trader
                if scenario in {"pending-order", "concurrent-close"}
                or cancel_target is not None
                or close_target is not None
                else position["trader"]
            ),
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
            self, value: object, status_code: int = 200, *, lcd: bool = False
        ) -> None:
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            if lcd:
                self.send_header("grpc-metadata-x-cosmos-block-height", "120")
            self.send_header("Content-Length", str(len(body)))
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

        def admit_trade(self, variables: Any) -> None:
            session = PROTOCOL["session_response"]["data"]["createUserIntentSession"]
            if self.headers.get("Authorization") != f"Bearer {session['token']}":
                self.reject("Wrong fixture authorization")
                return
            key = variables["input"]["intentKey"]
            if scenario == "trade-reconcile-offline":
                admission.key = key
                admission.arrived.set()
                self.close_connection = True
                return
            if scenario == "trade-timeout" and not admission.arrived.is_set():
                admission.key = key
                admission.arrived.set()
                admission.release.wait(35)
                self.close_connection = True
                return
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
                self.path.startswith(scope) and self.path != scope + keeper_path
            ):
                self.reject("Unexpected HTTP POST")
                return
            if self.path != scope + keeper_path:
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
                if name == "recordUserIntent" and scenario.startswith("trade-"):
                    self.admit_trade(request.variables)
                    return
                if name == "userIntents" and scenario in {
                    "trade-timeout",
                    "trade-reconcile-offline",
                }:
                    if not admission.arrived.is_set() or not same_variables(
                        request.variables, {"user": ADDRESS, "key": admission.key}
                    ):
                        self.reject("Wrong timed-out intent lookup")
                    else:
                        row = {
                            **status,
                            "intentKey": admission.key,
                            "kind": "OPEN_TRADE"
                            if scenario == "trade-timeout"
                            else "CLOSE_POSITION",
                            "lifecycleStep": "exec_fail",
                            "execActor": "server",
                            "nibiAddress": None,
                            "errorCode": "chain_execution_failed",
                        }
                        if scenario == "trade-reconcile-offline":
                            row["txHash"] = TRADE_REQUEST_HASH
                        self.reply({"data": {"userIntents": [row]}})
                    return
                if name in responses:
                    if not same_variables(request.variables, variables.get(name, {})):
                        self.reject(f"Wrong Keeper variables for {name}")
                    elif scenario == "interrupt-http":
                        interrupt_ready.set()
                        interrupt_closed.wait(8)
                    elif scenario == "keeper-error":
                        self.reply({"errors": [{"message": "fixture unavailable"}]})
                    elif scenario == "keeper-echo-error":
                        self.reply(
                            {
                                "errors": [
                                    {
                                        "message": f"Rejected request {self.path}",
                                        "extensions": {"code": "FORBIDDEN"},
                                    },
                                    {
                                        "extensions": {
                                            "code": self.path.partition("?key=")[2]
                                        }
                                    },
                                ]
                            }
                        )
                    elif (
                        scenario == "read-number-spelling" and name == "tokenPricesUsd"
                    ):
                        self.reply(
                            b'{"data":{"oracle":{"tokenPricesUsd":['
                            b'{"priceUsd":0.000001},{"priceUsd":1.2300},'
                            b'{"priceUsd":0.00001230},{"priceUsd":1e16},'
                            b'{"priceUsd":-0.00},{"priceUsd":1e400},'
                            b'{"priceUsd":18446744073709551616123456},'
                            b'{"priceUsd":"1.2300"}]}}}'
                        )
                    elif (scenario, name) in {
                        ("history-graphql-errors", "tradeHistory"),
                        ("doctor-graphql-errors", "oneClickTrading"),
                    }:
                        self.reply(
                            {
                                **responses[name],
                                "errors": [
                                    {
                                        "message": "opaque Keeper diagnostic",
                                        "extensions": {
                                            "code": "GRAPHQL_VALIDATION_FAILED"
                                        },
                                    },
                                    {
                                        "message": "opaque Keeper diagnostic",
                                        "extensions": {"code": "unvalidated"},
                                    },
                                    {"message": "opaque Keeper diagnostic"},
                                    {"extensions": {"code": "FORBIDDEN"}},
                                ],
                            }
                        )
                    elif scenario == "read-partial-error":
                        self.reply(
                            {**responses[name], "errors": [{"message": "partial read"}]}
                        )
                    elif scenario == "read-partial" and name == "trades":
                        partial = {
                            **position,
                            "state": None,
                            "collateralAmount": "18446744073709551616123456",
                        }
                        self.reply({"data": {"perp": {"trades": [partial]}}})
                    elif scenario == "read-empty" and name == "tradeHistory":
                        self.reply({"data": {"perp": {"tradeHistory": []}}})
                    else:
                        if scenario == "trade-market-session" and name == "borrowings":
                            admission.arrived.set()
                            if not admission.release.wait(5):
                                self.reject("Market session gate timed out")
                                return
                        self.reply(responses[name])
                    return
            except (ValueError, KeyError, TypeError):
                pass
            self.reject("Unsupported Keeper request")

        def do_GET(self) -> None:
            if not self.path.startswith("/"):
                self.reject("Unexpected external HTTP GET")
                return
            path = urlsplit(self.path).path
            if path.startswith(scope):
                path = path.removeprefix(scope)
                if (
                    scenario == "doctor-lcd-unreachable"
                    and path == "/private-fixture-credential" + NODE_INFO
                ):
                    wire.append(WireRequest("node_info"))
                    self.reply({}, 503)
                    return
                if path == NODE_INFO:
                    wire.append(WireRequest("node_info"))
                    self.reply(
                        {
                            "default_node_info": {}
                            if network is None
                            else {"network": network}
                        }
                    )
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
                        if name == "get_trade_raw" and cancel_target is not None:
                            [nonce] = parse_qs(urlsplit(self.path).query).get(
                                "_sai_query", [""]
                            )
                            if len(nonce) != 32 or any(
                                c not in "0123456789abcdef" for c in nonce
                            ):
                                self.reject("Expected a fresh cancellation query")
                                return
                            if cancel_target in {"missing", "other-account"}:
                                self.reply(
                                    {
                                        "message": f"Trade not found for trader {cancel_trader} with index 7"
                                    },
                                    500,
                                )
                                return
                            if scenario == "trade-cancel-session":
                                admission.arrived.set()
                                if not admission.release.wait(5):
                                    self.reject("Cancellation session gate timed out")
                                    return
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

    redirect_path = f"{scope}/subscriptions?moved=1"

    def websocket_redirect(
        connection: ServerConnection, request: Request
    ) -> Response | None:
        if scenario == "ws-redirect-limit":
            location = request.path
        elif scenario == "ws-redirect" and request.path == f"{scope}/query":
            location = "subscriptions?moved=1"
        else:
            return None
        wire.append(WireRequest("ws:redirect"))
        response = connection.respond(HTTPStatus.TEMPORARY_REDIRECT, "Use another path")
        response.headers["Location"] = location
        return response

    def websocket_response(
        _connection: ServerConnection, request: Request, response: Response
    ) -> None:
        if scenario not in {"ws-no-subprotocol", "ws-wrong-subprotocol"}:
            return
        if request.headers.get("Sec-WebSocket-Protocol") != "graphql-transport-ws":
            errors.append("Expected graphql-transport-ws in the upgrade request")
        if response.status_code == HTTPStatus.SWITCHING_PROTOCOLS:
            del response.headers["Sec-WebSocket-Protocol"]
            if scenario == "ws-wrong-subprotocol":
                response.headers["Sec-WebSocket-Protocol"] = "wrong-protocol"

    class StalledClose(ServerConnection):
        def send_data(self) -> None:
            if self.protocol.close_rcvd is None:
                super().send_data()
            else:
                # Keep the socket open without acknowledging the client's close.
                self.protocol.data_to_send()
                interrupt_ready.set()

    disconnected_at = 0.0

    def websocket(connection: ServerConnection) -> None:
        nonlocal disconnected_at
        try:
            expected_path = (
                redirect_path if scenario == "ws-redirect" else f"{scope}/query"
            )
            if connection.request is None or connection.request.path != expected_path:
                connection.close()
                return
            initial = json.loads(connection.recv(timeout=5))
            if initial["type"] != "connection_init":
                errors.append("Expected WebSocket connection_init")
                return
            wire.append(WireRequest("ws:init"))
            if scenario == "doctor-ws-timeout":
                connection.recv(timeout=7)
                return
            if scenario == "interrupt-handshake":
                interrupt_ready.set()
                connection.recv(timeout=8)
                errors.append("Watch continued the handshake after SIGINT")
                return
            if scenario == "ws-no-ack":
                connection.send(json.dumps({"type": "connection_error"}))
                return
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
            if scenario == "interrupt-balance":
                interrupt_ready.set()
                connection.recv(timeout=8)
                errors.append("Balance read continued the subscription after SIGINT")
                return
            payload = queue_payloads[name]
            if scenario == "ws-partial":
                connection.send(json.dumps({"type": "ping"}))
                if json.loads(connection.recv(timeout=5)) != {"type": "pong"}:
                    errors.append("Expected GraphQL pong")
                    return
                wire.append(WireRequest("ws:pong"))
                payload = {**payload, "errors": [{"message": "partial balance"}]}
            count = sum(item.label == "ws:userBalances" for item in wire)
            if scenario == "watch-reconnect":
                if count > 1 and not 1.9 <= time.monotonic() - disconnected_at < 7:
                    errors.append("Watch did not reconnect after the two-second delay")
                    return
                payload = {"data": {"userBalances": [{"amount": str(count)}]}}
            message = json.dumps({"id": request_id, "type": "next", "payload": payload})
            if scenario == "watch-number-spelling":
                message = (
                    f'{{"id":{json.dumps(request_id)},"type":"next",'
                    '"payload":{"data":{"userBalances":[{"amount":1E-7}]}}}'
                )
            connection.send(message)
            if scenario in {
                "watch",
                "watch-number-spelling",
                "ws-redirect",
                "ws-no-subprotocol",
            }:
                connection.send(json.dumps({"id": request_id, "type": "complete"}))
            if scenario == "watch-reconnect" and count == 1:
                disconnected_at = time.monotonic()
                connection.close()
            if scenario in {"watch-idle", "watch-reconnect", "interrupt-balance-close"}:
                # Wait for the interrupted client to close the connection.
                connection.recv(timeout=10)
        except ConnectionClosed as exc:
            if scenario in {
                "interrupt-handshake",
                "interrupt-balance",
                "interrupt-balance-close",
            }:
                if exc.rcvd is None or exc.rcvd.code != 1011:
                    errors.append("Expected WebSocket close code 1011 after SIGINT")
                else:
                    wire.append(WireRequest("ws:close"))
                interrupt_closed.set()
            # Doctor only needs the handshake, and balances closes after its snapshot.
        except (ValueError, KeyError, StopIteration, TimeoutError, TypeError):
            errors.append("Unsupported Keeper WebSocket exchange")

    with (
        http_server(Handler) as http,
        graphql_ws(
            websocket,
            process_request=websocket_redirect,
            process_response=websocket_response,
            create_connection=StalledClose
            if scenario == "interrupt-balance-close"
            else None,
        ) as ws,
    ):
        yield Endpoints(
            http + scope,
            ws.removesuffix("/query") + scope + "/query",
            errors,
            wire,
            admission,
            interrupt_ready=interrupt_ready,
            interrupt_closed=interrupt_closed,
            keeper_path=keeper_path,
        )
