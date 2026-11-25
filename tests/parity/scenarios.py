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
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlsplit

from compare import INTENT_ID, timestamp
from keeper_wire import WireRequest, graphql_request, same_variables
from overview_scenarios import OverviewReplay
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
HEIGHT_HEADER = "x-cosmos-block-height"
POSITION_HEIGHT = "120"
POSITION_SCENARIOS = {
    "position-default",
    "position-precision",
    "position-after-hours",
    "position-index-lag",
    "position-wrong-chain",
    "position-foreign-trader",
    "position-foreign-contract-owner",
    "position-equivalent-owner",
    "position-wrong-index",
    "position-wrong-contract-index",
    "position-chain-changed",
    "position-contract-changed",
    "position-scientific-reduction",
    "position-no-oracle",
    "position-wrong-height",
    "partial-preview-changed",
    "partial-price-changed",
    "partial-price-recovers",
    "margin-accepted",
    "margin-interrupted",
    "margin-rejected",
    "margin-unknown",
    "partial-accepted",
    "partial-interrupted",
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
if malformed_expiry := data["malformed_expiry"]:
    accounts = storage.load_accounts()
    session = accounts["sessions"][data["address"]]
    if malformed_expiry == "missing":
        del session["expiresAt"]
    else:
        session["expiresAt"] = 123
    storage._save_accounts(accounts)
if embedded_address := data["embedded_address"]:
    accounts = storage.load_accounts()
    accounts["sessions"][data["address"]]["evmAddress"] = embedded_address
    storage._save_accounts(accounts)
if data["ledger"]:
    if data.get("accepted_blockers"):
        for key, kind, entity in (
            ("accepted-close", "CLOSE_POSITION", "1"),
            ("accepted-open", "OPEN_TRADE", ""),
        ):
            storage.pending(key, data["endpoint"], data["address"], kind, entity)
            storage.set_status(key, data["endpoint"], data["address"], "accepted")
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
    overview_expiry: str = field(
        default_factory=lambda: (
            datetime.now(UTC) + timedelta(hours=49, minutes=30)
        ).isoformat()
    )
    overview_times: tuple[str, str] | None = None

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
    if seed in {"doctor-ledger-v0", "doctor-loose-profile"}:
        trader = sai_home / "trader"
        trader.mkdir(mode=0o700)
        with sqlite3.connect(path := trader / "intents.sqlite3") as conn:
            conn.execute(
                "CREATE TABLE intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL,"
                " account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,"
                " entity TEXT NOT NULL)"
            )
        path.chmod(0o600)
        if seed == "doctor-loose-profile":
            sai_home.chmod(0o750)
            config = sai_home / "config.toml"
            config.write_text("[logs]\nenabled = false\n")
            config.chmod(0o600)
            trader.chmod(0o755)
            (trader / "history").touch(mode=0o644)
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
    if seed in {"config", "config-logs-disabled", "invalid-expiry"}:
        path = sai_home / "config.toml"
        text = CONFIG if seed == "config" else "[logs]\nenabled = false\n"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        if seed != "invalid-expiry":
            return
    if seed == "mcp-info-cache":
        path = sai_home / "update.json"
        path.write_text(
            '{"schema": 1, "latest": "sai-cli/v0.99.0", "checkedAt": "2000-01-01T00:00:00Z"}',
            encoding="utf-8",
        )
        path.chmod(0o600)
        return
    sessions = []
    malformed_expiry = {
        "mcp-numeric-expiry": "numeric",
        "mcp-missing-expiry": "missing",
    }.get(seed)
    if malformed_expiry or seed in {
        "session",
        "accounts",
        "doctor-near-expiry",
        "doctor-ledger-session",
        "history-token",
        "mcp-auth-switch",
        "mcp-auth-redaction",
        "mcp-invalid-expiry",
        "invalid-expiry",
    }:
        sessions.append(session_fixture(endpoints, "session"))
        if seed == "doctor-near-expiry":
            sessions[0].update(expiresAt=endpoints.near_expiry, keyName="fixture-key")
        if seed == "history-token":
            sessions[0]["token"] = HISTORY_SESSION_TOKEN
        if malformed_expiry or seed in {
            "accounts",
            "mcp-auth-switch",
            "mcp-auth-redaction",
            "invalid-expiry",
            "mcp-invalid-expiry",
        }:
            sessions.append({**sessions[0], "evmAddress": OTHER_ADDRESS})
        if seed in {"invalid-expiry", "mcp-invalid-expiry"}:
            sessions[0]["expiresAt"] = "synthetic-expiry-credential-marker"
        if malformed_expiry or seed == "mcp-invalid-expiry":
            sessions.reverse()
        if seed == "mcp-auth-redaction":
            marker = "synthetic-session-origin-credential"
            sessions[0].update(token=marker, expiresAt=endpoints.near_expiry)
            sessions.append(
                {
                    **sessions[0],
                    "evmAddress": OTHER_ADDRESS,
                    "token": sessions[0]["expiresAt"],
                    "endpoint": f"{endpoints.keeper}/{marker}",
                }
            )
    if seed in {"overview-session", "overview-expiry", "overview-blockers"}:
        session = session_fixture(endpoints, "session")
        expires_at = (
            endpoints.overview_expiry
            if seed == "overview-expiry"
            else session["expiresAt"]
        )
        claim = base64.urlsafe_b64encode(b'{"iat":946684800}').decode()
        sessions.append(
            {
                **session,
                "token": f"fixture.{claim}.signature",
                "endpoint": endpoints.keeper,
                "expiresAt": expires_at,
                **(
                    {"source": "imported"}
                    if seed == "overview-expiry"
                    else {"keyName": "fixture"}
                ),
            }
        )
    if seed == "doctor-ledger-session":
        config = sai_home / "config.toml"
        config.write_text("[logs]\nenabled = false\n")
        config.chmod(0o600)
    write_seed(
        home,
        endpoints,
        sessions,
        ledger=seed
        in {"ledger", "doctor-ledger-session", "overview-session", "overview-blockers"},
        accepted_blockers=seed == "overview-blockers",
        disable_logs=seed
        in {
            "doctor-near-expiry",
            "doctor-ledger-session",
            "mcp-auth-redaction",
        },
        malformed_expiry=malformed_expiry,
    )
    if malformed_expiry or seed in {
        "mcp-auth-switch",
        "mcp-auth-redaction",
        "invalid-expiry",
        "mcp-invalid-expiry",
    }:
        (sai_home / "trader" / "sessions.lock").chmod(0o600)


def session_fixture(endpoints: Endpoints, state: str) -> dict[str, Any]:
    session = PROTOCOL["session_response"]["data"]["createUserIntentSession"]
    return {
        **session,
        "endpoint": endpoints.keeper if state != "other-endpoint" else endpoints.http,
        "expiresAt": "2000-01-01T00:00:00Z"
        if state == "expired"
        else session["expiresAt"],
    }


def write_seed(
    home: Path,
    endpoints: Endpoints,
    sessions: list[dict[str, Any]],
    *,
    ledger: bool = False,
    accepted_blockers: bool = False,
    disable_logs: bool = False,
    malformed_expiry: str | None = None,
    embedded_address: str | None = None,
) -> None:
    env = child_env(home, endpoints)
    if disable_logs:
        env["SAI_LOG"] = "false"
    subprocess.run(
        [sys.executable, "-c", SEED],
        input=json.dumps(
            {
                "sessions": sessions,
                "malformed_expiry": malformed_expiry,
                "embedded_address": embedded_address,
                "ledger": ledger,
                "accepted_blockers": accepted_blockers,
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
    trade_replay: Callable[[WireRequest, str | None], dict[str, Any] | None]
    | None = None,
    market_price: float | None = None,
    cancel_target: str | None = None,
    close_target: str | None = None,
    lcd_query: bool = False,
    overview_delay: float = 0,
    wrong_balance_at: Literal["snapshot", "pinned"] | None = None,
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
    overview = (
        OverviewReplay(scenario, ADDRESS, CONTRACT)
        if scenario.startswith("overview-")
        else None
    )
    markets = fixture("mainnet_markets_http.json")
    prices = fixture("mainnet_prices_http.json")
    if scenario == "read-price-epoch-spellings":
        rows = prices["data"]["oracle"]["tokenPricesUsd"]
        stamps = (
            "1970-01-01t00:00:00z",
            "1970-01-01 01:00:00.0000000000+01:00",
            "1969-12-31T23:00:00−01:00",
            "1970-01-01t00:00:00.0000000001z",
        )
        for row, stamp in zip(rows[: len(stamps)], stamps, strict=True):
            row["lastUpdatedBlock"]["block_ts"] = stamp
    position = PROTOCOL["account_positions_response"]["data"]["perp"]["trades"][0]
    borrowing = position["perpBorrowing"]
    recorded_markets = markets["data"]["perp"]["borrowings"]
    market = {**recorded_markets[0], "marketId": borrowing["marketId"]}
    if market_price is not None:
        market["price"] = market_price
    if scenario == "quote-null-price":
        market["price"] = None
    recorded_markets.append(market)
    collateral = market["collateralToken"]
    trader = {
        "position-foreign-trader": OTHER_ADDRESS,
        "position-equivalent-owner": ADDRESS.upper(),
        "quote-foreign-keeper-owner": OTHER_ADDRESS,
    }.get(scenario, position["trader"])
    if close_target == "keeper-owner":
        trader = OTHER_ADDRESS
    trade = {
        **position,
        "trader": trader,
        "perpBorrowing": {**borrowing, "collateralToken": collateral},
    }
    if close_target == "keeper-index" or scenario in {
        "position-wrong-index",
        "quote-wrong-position-index",
    }:
        trade["id"] = 8
    status = PROTOCOL["intent_status_response"]["data"]["userIntents"][0]
    if scenario in {"mcp-reads", "mcp-wait-final"}:
        status = {**status, "txHash": TRADE_REQUEST_HASH}
    elif scenario == "mcp-pending-malformed-hash":
        status = {
            **status,
            "lifecycleStep": "exec_fail",
            "errorCode": "market_order_outcome_not_indexed",
        }
    history = {
        "id": 7,
        "intentKey": INTENT,
        "txHash": status["txHash"],
        "trade": {"id": 7, "trader": ADDRESS},
    }
    chains = {"unknown-chain": None, "empty-chain": ""}
    chain = (
        "fixture-chain-2"
        if scenario == "position-chain-changed"
        else chains.get(cancel_target, "cataclysm-1")
    )
    contract = (
        "nibi1replacementfixture"
        if scenario == "position-contract-changed"
        else ""
        if cancel_target == "missing-contract"
        else CONTRACT
    )
    network = "different-chain" if cancel_target == "missing-contract" else chain
    enabled = 1 if cancel_target == "non-boolean-route" else True
    responses = {
        "oneClickTrading": {
            "data": {
                "oneClickTrading": {
                    "enabled": enabled,
                    "chainId": None
                    if scenario == "doctor-lcd-unknown-keeper-chain"
                    else chain,
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
    if scenario == "mcp-wait-final":
        history.update(
            tradeChangeType="position_closed_user", block={"block": status["execBlock"]}
        )
    elif scenario == "mcp-wait-pending":
        responses["waitUserIntents"] = {
            "data": {
                "userIntents": [
                    {**status, "intentKey": INTENT, "lifecycleStep": "pending_exec"}
                ]
            }
        }
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
    if overview is not None:
        queue_payloads["userBalancesWithBlock"] = overview.balances
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
                else trader
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
        "market_order_config": {"fill_at_next_oracle_price": False},
        "get_collateral_price": "1",
        "get_perp_prices": {"market": {"base_per_quote": "100"}, "collateral": "1"},
        "get_liquidation_price": {"liquidation_price": "60"},
        "quote_trade": {"data": None, "attributes": [], "events": [], "messages": []},
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
    if scenario in {
        "partial-preview",
        "partial-price-changed",
        "partial-price-recovers",
    }:
        contracts["get_perp_prices"]["market"]["base_last_update_time"] = 1
        contracts["market_order_config"]["fill_at_next_oracle_price"] = True
    if scenario in {"position-precision", "position-after-hours"}:
        trade.update(collateralAmount=10000003, leverage=2.1234567890123457)
        contracts["get_trade_raw"].update(
            collateral_amount="10000003", leverage="2.123456789012345678"
        )
        contracts["get_collateral_price"] = "0.987654321098765432"
    if scenario == "position-index-lag":
        contracts["get_trade_raw"]["collateral_amount"] = "10000001"
    if scenario in POSITION_SCENARIOS:
        contracts["get_trade_raw"].pop("long", None)
    if scenario.startswith("position-execution-"):
        contracts["get_group"]["stale_price_expiration_secs"] = 0
        if scenario != "position-execution-no-oracle-time":
            contracts["get_perp_prices"]["market"]["base_last_update_time"] = 1
        if scenario == "position-execution-close-only":
            contracts["get_market"]["market_trading_state"] = "close_only"
        if scenario == "position-execution-stale":
            contracts["get_group"]["stale_price_expiration_secs"] = 30
    if scenario in {"position-foreign-contract-owner", "quote-foreign-contract-owner"}:
        contracts["get_trade_raw"]["user"] = OTHER_ADDRESS
    if scenario == "position-wrong-contract-index":
        contracts["get_trade_raw"]["user_trade_index"] = "UserTradeIndex(8)"
    if scenario == "position-equivalent-owner":
        contracts["get_trade_raw"]["user"] = position["trader"]
    if scenario == "position-scientific-reduction":
        trade["collateralAmount"] = 10**29
        contracts["get_trade_raw"]["collateral_amount"] = str(10**29)
    if scenario == "position-after-hours":
        contracts.update(
            is_after_hours_market=True,
            get_market={"block_last_opened": 110, "block_last_closed": 100},
            get_trade_infos=[{"created_block": 90}],
            get_after_hours_leverage="1.234567890123456789",
        )
    if scenario == "quote-close-only":
        contracts["get_market"]["market_trading_state"] = "close_only"
    if scenario == "quote-pair-cap":
        contracts["get_pair_custom_max_leverage"] = "1.5"
    if scenario == "quote-minimum":
        contracts["get_fees"]["min_position_size_usd"] = "10000000000"
    if scenario == "quote-trigger-fee":
        contracts["get_fees"]["trigger_order_fee_p"] = "0.0007"
    if scenario == "quote-liquidation":
        contracts["get_liquidation_price"]["liquidation_price"] = "100"
    if scenario in {
        "quote-close",
        "quote-close-fee-cap",
        "quote-fee-read-error",
        "quote-liquidation",
        "quote-after-hours",
    }:
        contracts["get_perp_prices"]["market"]["base_last_update_time"] = 1
        contracts["get_group"]["stale_price_expiration_secs"] = 0
    if scenario in {"quote-stale", "quote-queued"}:
        contracts["get_perp_prices"]["market"]["base_last_update_time"] = 1
        contracts["get_group"]["stale_price_expiration_secs"] = 30
        contracts["market_order_config"] = {
            "fill_at_next_oracle_price": scenario == "quote-queued"
        }
    if scenario == "quote-after-hours":
        contracts.update(is_after_hours_market=True, get_after_hours_leverage="1.1")
        contracts["get_market"].update(block_last_opened=50, block_last_closed=100)
    if scenario == "quote-close-fee-cap":
        contracts["get_fees"]["close_fee_p"] = "2"
    contract_args = {
        "get_trade_raw": {
            "trader": (
                cancel_trader
                if scenario in {"pending-order", "concurrent-close"}
                or cancel_target is not None
                or close_target is not None
                else trader
            ),
            "index": 7,
        },
        "get_exchange_balance": {
            "user": trader,
            "collateral_index": token_index,
        },
        "is_after_hours_market": {"market_index": market_index},
        "market_order_config": {},
        "get_collateral_price": {"collateral_index": token_index},
        "get_market": {"index": market_index},
        "get_trade_infos": {
            "trader": trader,
            "start_idx": 7,
            "scan_limit": 1,
        },
        "get_after_hours_leverage": {},
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
        "quote_trade": {
            "trader": position["trader"],
            "funds": [],
            "action": {
                "update_leverage": {
                    "trade_index": "UserTradeIndex(7)",
                    "new_leverage": "1.818181818181818182",
                }
            },
        },
        "get_fees": {"index": "FeeIndex(0)"},
    }
    close_admission = PROTOCOL["close_admission_request"]
    expected_intent = {
        "concurrent-close": close_admission["intent"],
        "margin-accepted": MARGIN_INTENT,
        "margin-interrupted": MARGIN_INTENT,
        "margin-rejected": MARGIN_INTENT,
        "margin-unknown": MARGIN_INTENT,
        "position-equivalent-owner": MARGIN_INTENT,
        "partial-accepted": PARTIAL_INTENT,
        "partial-price-changed": PARTIAL_INTENT,
        "partial-price-recovers": PARTIAL_INTENT,
        "partial-interrupted": PARTIAL_INTENT,
        "partial-unknown": PARTIAL_INTENT,
    }.get(scenario)

    def contract_data(name: str, *, pinned: bool) -> Any:
        data = contracts[name]
        if name == "get_exchange_balance" and wrong_balance_at == (
            "pinned" if pinned else "snapshot"
        ):
            return {**data, "collateral_index": "TokenIndex(999)"}
        return data

    class Handler(BaseHTTPRequestHandler):
        def reply(
            self,
            value: object,
            status_code: int = 200,
            *,
            lcd: bool = False,
            height: str | int | None = None,
            duplicate_height: int | None = None,
            next_page: bool = False,
        ) -> None:
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            if lcd:
                if scenario == "quote-height-mismatch" and self.headers.get(
                    HEIGHT_HEADER
                ):
                    height = 121
                elif height is None:
                    height = 120
            if height is not None:
                self.send_header(f"grpc-metadata-{HEIGHT_HEADER}", str(height))
            if duplicate_height is not None:
                self.send_header(
                    f"grpc-metadata-{HEIGHT_HEADER}", str(duplicate_height)
                )
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
            if scenario in {
                "concurrent-close",
                "margin-interrupted",
                "partial-interrupted",
            }:
                if not admission.release.wait(15):
                    self.reject("Synthetic admission gate timed out")
                    return
                if scenario.endswith("-interrupted"):
                    self.close_connection = True
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
                if trade_replay is not None and name in {
                    "recordUserIntent",
                    "userIntents",
                    "waitUserIntents",
                    "tradeHistory",
                    "intentTradeHistory",
                }:
                    try:
                        reply = trade_replay(request, self.headers.get("Authorization"))
                    except (TypeError, ValueError) as exc:
                        self.reject(str(exc))
                    else:
                        if reply is None:
                            self.close_connection = True
                        else:
                            with suppress(BrokenPipeError, ConnectionResetError):
                                self.reply(reply)
                    return
                if name == "recordUserIntent" and expected_intent is not None:
                    self.admit(request.variables, expected_intent)
                    return
                if (
                    scenario == "confirmation-account-change"
                    and name == "trade"
                    and same_variables(
                        request.variables, {"trader": OTHER_ADDRESS, "id": 7}
                    )
                ):
                    self.reply({"data": {"perp": {"trade": None}}})
                    return
                if overview is not None:
                    if self.headers.get("Authorization") is not None:
                        self.reject("Unexpected authorization on overview read")
                        return
                    try:
                        response = overview.keeper(name, request.variables, wire)
                    except ValueError as exc:
                        self.reject(str(exc))
                        return
                    if response is not None:
                        if (
                            scenario == "overview-interrupt-http"
                            and name == "overviewTrades"
                        ):
                            interrupt_ready.set()
                            interrupt_closed.wait(8)
                        else:
                            if name == "overviewTrades" and overview_delay:
                                time.sleep(overview_delay)
                            self.reply(response)
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
                    expected = variables.get(name, {})
                    if (
                        scenario == "mcp-reads"
                        and name == "userIntents"
                        and request.variables.get("key") is None
                    ):
                        expected = {**expected, "key": None}
                    if not same_variables(request.variables, expected):
                        self.reject(f"Wrong Keeper variables for {name}")
                    elif scenario == "mcp-cancel":
                        interrupt_ready.set()
                        self.connection.settimeout(5)
                        if self.rfile.read(1) == b"":
                            interrupt_closed.set()
                    elif scenario == "interrupt-http":
                        interrupt_ready.set()
                        interrupt_closed.wait(8)
                    elif scenario == "keeper-error":
                        self.reply({"errors": [{"message": "fixture unavailable"}]})
                    elif (
                        scenario == "quote-route-changed"
                        and name == "oneClickTrading"
                        and sum(entry.label == name for entry in wire) > 1
                    ):
                        self.reply(
                            {
                                "data": {
                                    "oneClickTrading": {
                                        "enabled": True,
                                        "chainId": chain,
                                        "contractAddress": "nibi1replacement",
                                    }
                                },
                            }
                        )
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

        def drip_lcd(self) -> None:
            started = time.monotonic()
            try:
                self.send_response(200)
                self.send_header(
                    "grpc-metadata-x-cosmos-block-height",
                    self.headers["x-cosmos-block-height"],
                )
                self.end_headers()
                while time.monotonic() - started < 4:
                    self.wfile.write(b" ")
                    time.sleep(0.02)
                errors.append(
                    "Overview left a trickling LCD response open after its deadline"
                )
            except (BrokenPipeError, ConnectionResetError):
                if not 1.7 <= time.monotonic() - started < 3:
                    errors.append(
                        "Overview disconnected outside the LCD request deadline"
                    )

        def do_GET(self) -> None:
            if not self.path.startswith("/"):
                self.reject("Unexpected external HTTP GET")
                return
            request_url = urlsplit(self.path)
            path = request_url.path
            if path.startswith(scope):
                if lcd_query and parse_qs(request_url.query).get("project") != [
                    "fixture"
                ]:
                    self.reject("Missing LCD project query parameter")
                    return
                path = path.removeprefix(scope)
                if overview is not None:
                    try:
                        entry = overview.lcd(
                            path,
                            urlsplit(self.path).query,
                            self.headers.get("x-cosmos-block-height"),
                        )
                    except (ValueError, KeyError, TypeError) as exc:
                        self.reject(f"Invalid overview LCD request: {exc}")
                        return
                    if entry is not None:
                        request, response, height = entry
                        wire.append(request)
                        if (
                            scenario
                            in {"overview-lcd-fails", "overview-fill-and-lcd-fail"}
                            and request.label == "get_trade"
                            and request.variables["index"] == 0
                        ):
                            self.reply({}, 503)
                            return
                        if (
                            scenario == "overview-deadline"
                            and request.label == "get_trade"
                            and request.variables["index"] == 0
                        ):
                            self.drip_lcd()
                            return
                        duplicate_height = (
                            height - 1
                            if height is not None
                            and scenario == "overview-conflicting-height"
                            and request.label == "get_trade"
                            else None
                        )
                        self.reply(
                            response,
                            lcd=height is not None,
                            height=height,
                            duplicate_height=duplicate_height,
                        )
                        return
                if (
                    scenario == "doctor-lcd-unreachable"
                    and path == "/private-fixture-credential" + NODE_INFO
                ):
                    wire.append(WireRequest("node_info"))
                    self.reply({}, 503)
                    return
                if scenario == "doctor-lcd-chain-mismatch":
                    prefix, separator, _ = path.partition(NODE_INFO)
                    if separator:
                        wire.append(WireRequest("node_info"))
                        self.reply({"default_node_info": {"network": prefix[1:]}})
                        return
                if path == NODE_INFO:
                    wire.append(WireRequest("node_info"))
                    if scenario == "position-wrong-chain":
                        self.reply(
                            {"default_node_info": {"network": "wrong-fixture-chain"}}
                        )
                        return
                    self.reply(
                        {
                            "default_node_info": {}
                            if network is None
                            else {
                                "network": "invalid chain ID"
                                if scenario == "doctor-lcd-invalid-chain"
                                else network
                            }
                        }
                    )
                    return
                if path == "/cosmos/base/tendermint/v1beta1/blocks/120":
                    wire.append(WireRequest("block"))
                    self.reply(
                        {
                            "block": {
                                "header": {
                                    "height": "120",
                                    "time": "2026-01-01T00:00:00Z",
                                }
                            }
                        }
                    )
                    return
                if path == RELEASES:
                    self.releases()
                    return
                prefix = f"/cosmwasm/wasm/v1/contract/{contract}/smart/"
                if path.startswith(prefix):
                    try:
                        query = json.loads(
                            base64.b64decode(unquote(path.removeprefix(prefix)))
                        )
                        [name] = query
                        expected = contract_args[name]
                        if (
                            scenario == "position-equivalent-owner"
                            and name == "get_trade_raw"
                            and "_sai_query" in parse_qs(request_url.query)
                        ):
                            expected = {**expected, "trader": position["trader"]}
                        allowed = [{name: expected}]
                        # Ownership uses the account address. The next read
                        # uses Perp's stored owner to pin the quote.
                        owner_read = (
                            scenario.startswith("quote-")
                            and name == "get_trade_raw"
                            and query == {name: {"trader": ADDRESS, "index": 7}}
                        )
                        if name == "get_trade_raw":
                            if scenario == "concurrent-close":
                                allowed.append(
                                    {name: {"trader": position["trader"], "index": 7}}
                                )
                            if trade_replay is not None or owner_read:
                                allowed.append({name: {"trader": ADDRESS, "index": 7}})
                        if not any(same_variables(query, args) for args in allowed):
                            self.reject(f"Wrong LCD variables for {name}")
                            return
                        if scenario.startswith("quote-"):
                            pinned = name == "get_perp_prices" or any(
                                request.label == "get_perp_prices" for request in wire
                            )
                            anchor = name == "get_trade_raw" and any(
                                request.label == "get_exchange_balance"
                                for request in wire
                            )
                            if (
                                pinned
                                and self.headers.get("x-cosmos-block-height") != "120"
                            ):
                                self.reject(f"Quote must pin {name} to block 120")
                                return
                            if (
                                pinned or anchor or owner_read
                            ) and "_sai_query" not in parse_qs(
                                urlsplit(self.path).query
                            ):
                                self.reject(f"Expected a fresh {name} query")
                                return
                        wire.append(WireRequest(name, variables=query))
                        if scenario in POSITION_SCENARIOS or scenario.startswith(
                            "position-execution-"
                        ):
                            self.position_query(name, request_url.query)
                            return
                        if (
                            name == "get_pair_custom_max_leverage"
                            and scenario == "quote-absent-cap"
                        ):
                            self.reply(
                                {
                                    "message": "type: cosmwasm_std::math::decimal::Decimal; key: [00] not found"
                                },
                                500,
                            )
                            return
                        if name == "get_fees" and scenario == "quote-fee-read-error":
                            self.reply({}, 503)
                            return
                        if owner_read and scenario == "quote-owner-read-error":
                            self.reply({}, 503)
                            return
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
                        data = contract_data(name, pinned=HEIGHT_HEADER in self.headers)
                        self.reply({"data": data}, lcd=True)
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
            final_position = snapshot.count("get_trade_raw") >= 2
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
            if (
                scenario == "position-execution-quote-liquidates"
                and name == "quote_trade"
            ):
                self.reply(
                    {
                        "message": "liquidation price reached. Liq price 90, current price 100"
                    },
                    400,
                    height=POSITION_HEIGHT,
                )
                return
            if (
                scenario == "partial-price-recovers"
                and name == "get_collateral_price"
                and sum(item.label == name for item in wire) == 1
            ):
                self.reply({}, 503)
                return
            data = contract_data(name, pinned=pinned)
            if name == "get_exchange_balance" and (
                scenario == "position-execution-unfunded"
                or (scenario == "position-execution-pinned-unfunded" and pinned)
            ):
                data = {**data, "available_to_trade": "0"}
            if (
                scenario == "position-equivalent-owner"
                and name == "get_trade_raw"
                and final_position
            ):
                data = {**data, "user": ADDRESS}
            if scenario == "partial-preview-changed" and name == "get_trade_raw":
                data = {**data, "leverage": str(self.position_leverage())}
            if scenario == "partial-price-changed" and name in {
                "get_collateral_price",
                "get_perp_prices",
            }:
                # Tick on the quote reads and again at the final pinned read.
                ticks = sum(item.label == "get_collateral_price" for item in wire)
                if name == "get_perp_prices":
                    data = {**data, "collateral": f"1.{ticks + 1:05d}"}
                else:
                    data = f"1.{ticks:05d}"
            served = (
                "121"
                if scenario == "position-wrong-height" and pinned
                else POSITION_HEIGHT
            )
            self.reply({"data": data}, height=served)

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
            if scenario == "overview-balance-pings":
                # Withhold the balance payload while keeping GraphQL traffic
                # flowing. The client should close at its snapshot deadline.
                while True:
                    connection.send(json.dumps({"type": "ping"}))
                    if json.loads(connection.recv(timeout=5)) != {"type": "pong"}:
                        errors.append("Expected GraphQL pong")
                        return
                    wire.append(WireRequest("ws:pong"))
                    time.sleep(1)
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
            overview_times=(overview.state["riskTime"], overview.state["markTime"])
            if overview is not None
            else None,
        )
