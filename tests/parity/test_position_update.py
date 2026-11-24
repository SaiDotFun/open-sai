"""TP/SL and leverage from v0.1.2 (8557a7e) and ownership from PR 148 (c6d85c2)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from compare import INTENT_ID, timestamp
from keeper_wire import WireRequest, graphql_request, same_variables
from sai_cli.tests.loopback import http_server
from scenarios import (
    ADDRESS,
    CONTRACT,
    NODE_INFO,
    OTHER_ADDRESS,
    PROTOCOL,
    TRADE_REQUEST_HASH,
    Endpoints,
    child_env,
    seed_home,
)
from test_trade import CLI, rows, run

TPSL = ("trade", "tpsl", "7", "--take-profit", "110.0100", "--clear-stop-loss")
LEVERAGE = ("trade", "leverage", "7", "--leverage", "10.00")
UPDATE_WIRE = ["trade", "oneClickTrading", "node_info", "get_trade_raw"]
QUOTE_WIRE = [
    "trade",
    "oneClickTrading",
    "node_info",
    "borrowing",
    "get_trade_raw",
    "get_exchange_balance",
    "get_trade_raw",
    "is_after_hours_market",
]
LEVERAGE_WIRE = ["get_market", "get_group", "get_pair_custom_max_leverage"]
ORACLE_WIRE = ["get_fees", "get_perp_prices"]
CHAIN = "cataclysm-1"
HEIGHT = "120"
ENTRY = "100.000000000000000000"
OUTSIDE = "The leverage request is outside the market's collateral or leverage limits."
GUARDED = "The after-hours guard can change or close this position."
UNKNOWN = "Cannot verify that the Perp after-hours guard is inactive."
STALE = "Perp rejects the leverage change at the observed stale oracle price."
LIQUIDATION = "The observed oracle price is past the prospective liquidation threshold."
QUEUED = "Queued leverage changes settle on a fresh oracle update."
MISSING_CAP = {
    "code": 3,
    "message": "type: cosmwasm_std::math::decimal::Decimal; key: [00, 07] not found",
}
QUOTED_LIQUIDATION = {
    "status": "estimate",
    "price": "92",
    "entry": {
        "price": ENTRY,
        "source": "Sai Perp stored entry price",
        "observedAt": "<timestamp>",
    },
    "excludesBorrowingFees": True,
    "assumption": "The target leverage takes effect if Perp's borrowing-inclusive liquidation check passes.",
}


def unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def body(command: CLI) -> dict[str, Any]:
    if command == TPSL:
        return {
            "takeProfit": {"operation": "set", "value": "110.0100"},
            "stopLoss": {"operation": "clear"},
        }
    return {
        "takeProfit": {"operation": "keep"},
        "stopLoss": {"operation": "keep"},
        "requestedLeverage": "10.00",
    }


def intent(command: CLI) -> dict[str, Any]:
    return {
        "kind": "UPDATE_TRADE",
        "target": {"userTradeId": "7"},
        "body": body(command),
    }


def estimates(liquidation_price: dict[str, Any]) -> dict[str, Any]:
    return {
        "fees": {"status": "exact", "amountUsdc": "0", "excludes": ["Network fees"]},
        "liquidationPrice": liquidation_price,
        "worstCaseFill": {
            "status": "not_applicable",
            "reason": "This action has no price fill.",
        },
    }


@contextmanager
def update_loopback(mode: str) -> Generator[Endpoints]:
    wire: list[WireRequest] = []
    errors: list[str] = []
    submitted: list[str] = []
    token = PROTOCOL["session_response"]["data"]["createUserIntentSession"]["token"]
    borrowing = {"marketId": 3, "collateralToken": {"id": 0, "symbol": "USDC"}}
    position = {
        "id": 8 if mode == "wrong-id" else 7,
        "trader": OTHER_ADDRESS if mode == "keeper-owner" else ADDRESS,
        "isOpen": True,
        "tradeType": "trade",
        "leverage": 5,
        "collateralAmount": "20000000",
        "perpBorrowing": borrowing,
    }
    raw = {
        "user": OTHER_ADDRESS if mode == "contract-owner" else ADDRESS,
        "user_trade_index": "UserTradeIndex(7)",
        "market_index": "MarketIndex(3)",
        "collateral_index": "TokenIndex(0)",
        "is_open": True,
        "trade_type": "trade",
        "collateral_amount": "20000000",
        "leverage": "5",
        "open_price": ENTRY,
        "long": True,
    }
    keeper_replies = {
        "trade": {"perp": {"trade": position}},
        "oneClickTrading": {
            "oneClickTrading": {
                "enabled": True,
                "chainId": CHAIN,
                "contractAddress": CONTRACT,
            }
        },
        "borrowing": {"perp": {"borrowing": {**borrowing, "isOpen": True}}},
    }
    keeper_variables = {
        "trade": {"trader": ADDRESS, "id": 7},
        "oneClickTrading": {},
        "borrowing": {"market": 3, "collateral": 0},
    }
    lcd_replies = {
        "get_trade_raw": raw,
        "get_exchange_balance": {"collateral_index": "TokenIndex(0)"},
        "is_after_hours_market": mode == "after-hours",
        "get_market": {
            "group_index": "GroupIndex(0)",
            "fee_index": "FeeIndex(0)",
            "block_last_opened": 101,
            "block_last_closed": 118,
        },
        "get_after_hours_leverage": "3",
        "get_group": {
            "min_leverage": "1",
            "max_leverage": "100",
            "stale_price_expiration_secs": 10,
        },
        "get_pair_custom_max_leverage": "8" if mode == "pair-cap" else "0",
        "get_fees": {"open_fee_p": "0.001"},
        "get_perp_prices": {
            "market": {
                "base_per_quote": "85"
                if mode in {"queued", "stale", "liquidation"}
                else "99",
                "base_last_update_time": 995,
                "quote_last_update_time": 980 if mode == "stale" else 995,
            }
        },
        "market_order_config": {"fill_at_next_oracle_price": mode == "queued"},
    }
    lcd_variables = {
        "get_trade_raw": {"trader": ADDRESS, "index": 7},
        "get_exchange_balance": {
            "user": ADDRESS,
            "collateral_index": "TokenIndex(0)",
        },
        "is_after_hours_market": {"market_index": "MarketIndex(3)"},
        "get_market": {"index": "MarketIndex(3)"},
        "get_after_hours_leverage": {},
        "get_group": {"index": "GroupIndex(0)"},
        "get_pair_custom_max_leverage": {"index": 3},
        "get_fees": {"index": "FeeIndex(0)"},
        "get_perp_prices": {
            "market_index": "MarketIndex(3)",
            "collateral_index": "TokenIndex(0)",
        },
        "market_order_config": {},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def reply(
            self, value: object, *, status: int = 200, height: str | None = None
        ) -> None:
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if height is not None:
                self.send_header("grpc-metadata-x-cosmos-block-height", height)
            self.end_headers()
            self.wfile.write(data)

        def reject(self, message: str) -> None:
            errors.append(message)
            self.reply({"errors": [{"message": message}]}, status=400)

        def do_POST(self) -> None:
            if self.path != "/query":
                self.reject("Unexpected POST path")
                return
            try:
                request = graphql_request(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
            except (TypeError, ValueError):
                self.reject("Unexpected Keeper request")
                return
            wire.append(request)
            if request.label == "recordUserIntent":
                if self.headers.get("Authorization") != f"Bearer {token}":
                    self.reject("Wrong fixture authorization")
                    return
                key = request.variables["input"]["intentKey"]
                submitted.append(key)
                if mode == "unknown" and len(submitted) == 1:
                    self.close_connection = True
                    return
                if mode == "rejected":
                    self.reply(
                        {"errors": [{"extensions": {"code": "INTENT_NOT_ACCEPTED"}}]}
                    )
                    return
                ack = PROTOCOL["admission_ack"]["data"]["recordUserIntent"]
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
                return
            if request.label == "userIntents" and mode == "unknown" and submitted:
                key = submitted[0]
                if not same_variables(request.variables, {"user": ADDRESS, "key": key}):
                    self.reject("Unexpected reconciliation lookup")
                    return
                row = PROTOCOL["intent_status_response"]["data"]["userIntents"][0]
                self.reply(
                    {
                        "data": {
                            "userIntents": [
                                {
                                    **row,
                                    "intentKey": key,
                                    "kind": "UPDATE_TRADE",
                                    "lifecycleStep": "exec_fail",
                                    "execActor": "server",
                                    "errorCode": "chain_execution_failed",
                                }
                            ]
                        }
                    }
                )
                return
            expected = keeper_variables.get(request.label)
            if expected is None or not same_variables(request.variables, expected):
                self.reject("Unexpected Keeper operation or variables")
                return
            self.reply({"data": keeper_replies[request.label]})

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path == NODE_INFO:
                wire.append(WireRequest("node_info"))
                network = "other-chain" if mode == "wrong-chain" else CHAIN
                self.reply({"default_node_info": {"network": network}})
                return
            if url.path == f"/cosmos/base/tendermint/v1beta1/blocks/{HEIGHT}":
                wire.append(WireRequest("block"))
                self.reply(
                    {
                        "block": {
                            "header": {"height": HEIGHT, "time": "1970-01-01T00:16:40Z"}
                        }
                    }
                )
                return
            prefix = f"/cosmwasm/wasm/v1/contract/{CONTRACT}/smart/"
            if not url.path.startswith(prefix):
                self.reject("Unexpected LCD path")
                return
            try:
                ((name, values),) = json.loads(
                    base64.b64decode(
                        unquote(url.path.removeprefix(prefix)), validate=True
                    )
                ).items()
            except (TypeError, ValueError):
                self.reject("Unexpected Perp query encoding")
                return
            wire.append(WireRequest(name, variables=values))
            if (
                not same_variables(values, lcd_variables.get(name, {}))
                or name not in lcd_replies
            ):
                self.reject("Unexpected Perp query or variables")
                return
            pinned = self.headers.get("x-cosmos-block-height")
            fresh = "_sai_query" in parse_qs(url.query)
            if name not in {"get_trade_raw", "get_exchange_balance"} and (
                pinned != HEIGHT or not fresh
            ):
                self.reject("Perp estimate read was unpinned")
                return
            missing_cap_heights = {
                "group-fallback": HEIGHT,
                "cap-wrong-height": "119",
                "cap-missing-height": None,
            }
            if name == "get_pair_custom_max_leverage" and mode in missing_cap_heights:
                self.reply(MISSING_CAP, status=404, height=missing_cap_heights[mode])
                return
            if mode == "cap-unavailable" and name == "get_pair_custom_max_leverage":
                self.reply({"message": "service unavailable"}, status=503)
                return
            actual_height = "121" if mode == "wrong-height" and pinned else HEIGHT
            self.reply({"data": lcd_replies[name]}, height=actual_height)

    with http_server(Handler) as endpoint:
        yield Endpoints(endpoint, "", errors=errors, wire=wire)


def runners(
    cli_pair: tuple[CLI, CLI], *, released: bool = False
) -> list[tuple[str, CLI]]:
    reference = os.environ.get(
        "SAI_PARITY_POSITION_V012_PYTHON"
        if released
        else "SAI_PARITY_POSITION_OWNER_PYTHON"
    )
    rust = ("rust", cli_pair[1])
    # The expected output below was checked against the pinned reference.
    # Compare the reference subprocess too when its launcher is available.
    return [("python", (reference,)), rust] if reference else [rust]


def dry_run_bytes(cli: CLI, home: Path, endpoints: Endpoints, command: CLI) -> bytes:
    result = subprocess.run(
        [*cli, "--json", *command, "--dry-run"],
        cwd=home,
        env={**child_env(home, endpoints), "SAI_UPDATE_AUTO": "0"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert (result.returncode, result.stderr) == (0, b"")
    liq = json.loads(result.stdout)["estimates"]["liquidationPrice"]
    if entry := liq.get("entry"):
        observed_at = entry["observedAt"]
        assert timestamp(observed_at)
        return result.stdout.replace(
            json.dumps(observed_at).encode(), b'"<timestamp>"', 1
        )
    return result.stdout


@pytest.mark.parametrize(
    ("command", "mode", "suffix", "expected"),
    [
        (
            TPSL,
            "default",
            [],
            estimates(
                {
                    "status": "unchanged",
                    "reason": "This action changes only TP/SL triggers.",
                }
            ),
        ),
        (
            TPSL,
            "after-hours",
            ["get_market", "get_after_hours_leverage"],
            dict.fromkeys(estimates({}), unavailable(GUARDED)),
        ),
        (
            TPSL,
            "wrong-height",
            [],
            dict.fromkeys(estimates({}), unavailable(UNKNOWN)),
        ),
        (
            LEVERAGE,
            "group-fallback",
            LEVERAGE_WIRE + ORACLE_WIRE,
            estimates(QUOTED_LIQUIDATION),
        ),
        (
            LEVERAGE,
            "pair-cap",
            LEVERAGE_WIRE,
            estimates(unavailable(OUTSIDE)),
        ),
        (
            LEVERAGE,
            "cap-unavailable",
            LEVERAGE_WIRE,
            estimates(
                unavailable(
                    "Position entry price or projected collateral is unavailable."
                )
            ),
        ),
        (
            LEVERAGE,
            "queued",
            LEVERAGE_WIRE + ORACLE_WIRE + ["market_order_config"],
            {**estimates(QUOTED_LIQUIDATION), "execution": unavailable(QUEUED)},
        ),
        (
            LEVERAGE,
            "stale",
            LEVERAGE_WIRE + ORACLE_WIRE + ["market_order_config", "get_group", "block"],
            {
                **estimates(unavailable(STALE)),
                "execution": {
                    "status": "would_be_rejected",
                    "code": "OraclePriceStale",
                    "reason": STALE,
                },
            },
        ),
        (
            LEVERAGE,
            "liquidation",
            LEVERAGE_WIRE + ORACLE_WIRE + ["market_order_config", "get_group", "block"],
            {
                **estimates(unavailable(LIQUIDATION)),
                "execution": {
                    "status": "would_be_rejected",
                    "code": "LiquidationReached",
                    "reason": LIQUIDATION,
                },
            },
        ),
    ],
    ids=[
        "tpsl",
        "after-hours",
        "wrong-height",
        "group-fallback",
        "override",
        "cap-unavailable",
        "queued",
        "stale",
        "liquidation",
    ],
)
def test_position_update_dry_run(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    command: CLI,
    mode: str,
    suffix: list[str],
    expected: dict[str, Any],
) -> None:
    # Compare ordered JSON after normalizing the one dynamic timestamp.
    golden = json.dumps(
        {
            "addr": ADDRESS,
            "dryRun": True,
            "intent": intent(command),
            "estimates": expected,
        }
    )
    for implementation, cli in runners(cli_pair):
        home = tmp_path / implementation
        with update_loopback(mode) as endpoints:
            seed_home(home, endpoints, "session")
            assert (
                dry_run_bytes(cli, home, endpoints, command) == (golden + "\n").encode()
            )
            assert [entry.label for entry in endpoints.wire] == QUOTE_WIRE + suffix
            assert not endpoints.errors
            assert not (home / "sai/trader/intents.sqlite3").exists()


@pytest.mark.parametrize("mode", ["cap-wrong-height", "cap-missing-height"])
def test_pair_cap_fallback_requires_the_pinned_height(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, mode: str
) -> None:
    # The pinned Python helper accepts even unverified storage misses.
    _, rust = cli_pair
    with update_loopback(mode) as endpoints:
        seed_home(tmp_path / "rust", endpoints, "session")
        code, output, stderr = run(
            rust, tmp_path / "rust", endpoints, (*LEVERAGE, "--dry-run"), python=False
        )
        assert (code, stderr) == (0, "")
        assert output["estimates"] == estimates(
            unavailable("Position entry price or projected collateral is unavailable.")
        )
        assert [request.label for request in endpoints.wire] == QUOTE_WIRE + LEVERAGE_WIRE
        assert not endpoints.errors
        assert not (tmp_path / "rust/sai/trader/intents.sqlite3").exists()


@pytest.mark.parametrize("command", [TPSL, LEVERAGE], ids=["tpsl", "leverage"])
def test_unsigned_update_preview(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, command: CLI
) -> None:
    for implementation, cli in runners(cli_pair, released=True):
        home = tmp_path / implementation
        with update_loopback("default") as endpoints:
            seed_home(home, endpoints, "empty")
            code, output, stderr = run(
                cli, home, endpoints, (*command, "--dry-run"), python=False
            )
            assert (code, stderr) == (0, "")
            assert output == {
                "addr": None,
                "dryRun": True,
                "intent": intent(command),
                "estimates": dict.fromkeys(estimates({}), unavailable(UNKNOWN)),
            }
            assert not endpoints.errors
            assert not endpoints.wire
            assert not rows(home)


@pytest.mark.parametrize("command", [TPSL, LEVERAGE], ids=["tpsl", "leverage"])
@pytest.mark.parametrize(
    ("mode", "code", "status"),
    [
        ("default", 0, "accepted"),
        ("rejected", 4, "rejected"),
        ("unknown", 5, "unknown"),
    ],
)
def test_position_update_submission(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    command: CLI,
    mode: str,
    code: int,
    status: str,
) -> None:
    for implementation, cli in runners(cli_pair):
        home = tmp_path / implementation
        with update_loopback(mode) as endpoints:
            seed_home(home, endpoints, "session")
            actual, output, stderr = run(cli, home, endpoints, command, python=False)
            assert (actual, stderr) == (code, "")
            assert [entry.label for entry in endpoints.wire] == [
                *UPDATE_WIRE,
                "recordUserIntent",
            ]
            request = endpoints.wire[-1].variables["input"]
            key = request["intentKey"]
            assert INTENT_ID.fullmatch(key)
            stamp = request["intent"]["tsIntentInit"]
            assert timestamp(stamp)
            assert request == {
                "intentKey": key,
                "intent": {
                    "intentSchemaVersion": "v0",
                    "tsIntentInit": stamp,
                    **intent(command),
                },
                "lifecycle": {
                    "sequence": 1,
                    "lifecycleStep": "jwt_pass",
                    "tsLifecycleStep": stamp,
                },
            }
            assert [row[:6] for row in rows(home)] == [
                (key, "UPDATE_TRADE", status, "7", ADDRESS, None)
            ]
            if mode == "unknown":
                assert output["error"]["intentKey"] == key
                retry, failure, _ = run(cli, home, endpoints, command, python=False)
                assert retry == 1
                assert failure["error"]["code"] == "INVALID_REQUEST"
                assert failure["error"]["intentKey"] == key
                assert [entry.label for entry in endpoints.wire] == [
                    *UPDATE_WIRE,
                    "recordUserIntent",
                    *UPDATE_WIRE,
                ]
                assert len(rows(home)) == 1
                reconcile = ("intent", "status", ADDRESS, "--key", key)
                assert run(cli, home, endpoints, reconcile, python=False)[0] == 0
                assert rows(home)[0][2:6] == ("terminal", "7", ADDRESS, "terminal")
                assert run(cli, home, endpoints, command, python=False)[0] == 0
                assert [entry.label for entry in endpoints.wire][-6:] == [
                    "userIntents",
                    *UPDATE_WIRE,
                    "recordUserIntent",
                ]
                assert [row[2] for row in rows(home)] == ["terminal", "accepted"]
            elif mode == "rejected":
                assert output["error"]["code"] == "ADMISSION_REJECTED"
            else:
                assert output == {
                    "addr": ADDRESS,
                    "intentKey": key,
                    "status": "accepted",
                    "message": "Accepted by Keeper; execution is not yet confirmed",
                }
            assert not endpoints.errors


@pytest.mark.parametrize("command", [TPSL, LEVERAGE], ids=["tpsl", "leverage"])
@pytest.mark.parametrize(
    ("mode", "message", "wire"),
    [
        ("keeper-owner", "Keeper position belongs to a different account", ["trade"]),
        ("wrong-id", "Keeper returned a different position index", ["trade"]),
        (
            "contract-owner",
            "Contract position belongs to a different account",
            UPDATE_WIRE,
        ),
        (
            "wrong-chain",
            "Keeper trading route and LCD network do not match",
            UPDATE_WIRE[:-1],
        ),
    ],
)
def test_position_update_refusal(
    cli_pair: tuple[CLI, CLI],
    tmp_path: Path,
    command: CLI,
    mode: str,
    message: str,
    wire: list[str],
) -> None:
    for implementation, cli in runners(cli_pair):
        home = tmp_path / implementation
        with update_loopback(mode) as endpoints:
            seed_home(home, endpoints, "session")
            code, output, stderr = run(cli, home, endpoints, command, python=False)
            assert (code, stderr) == (1, "")
            assert output["error"] == {
                "code": "INVALID_REQUEST",
                "message": message,
                "retryable": False,
            }
            assert [entry.label for entry in endpoints.wire] == wire
            assert not endpoints.errors
            assert not (home / "sai/trader/intents.sqlite3").exists()
