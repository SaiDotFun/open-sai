"""TP/SL and leverage from v0.1.2 (8557a7e) ownership from PR 148, and leverage estimates from 7c7b00d."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from compare import INTENT_ID, timestamp
from keeper_wire import WireRequest, graphql_request, same_variables
from sai_cli.core.confirmation import preview_digest
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
CLEAR_TP = ("trade", "tpsl", "7", "--clear-take-profit")
CLEAR_SL = ("trade", "tpsl", "7", "--clear-stop-loss")
SET_BOTH = ("trade", "tpsl", "7", "--take-profit", "110.0100", "--stop-loss", "90.0100")
LEVERAGE = ("trade", "leverage", "7", "--leverage", "10.00")
UPDATE_WIRE = ["trade", "oneClickTrading", "node_info", "get_trade_raw"]
QUOTE_ESTIMATE_WIRE = [
    "get_exchange_balance",
    "get_trade_raw",
    "is_after_hours_market",
]
QUOTE_WIRE = [*UPDATE_WIRE, "borrowing", *QUOTE_ESTIMATE_WIRE]
# PR 148's Python preview reads the borrowing before checking the contract owner.
PYTHON_QUOTE_WIRE = [
    *UPDATE_WIRE[:-1],
    "borrowing",
    "get_trade_raw",
    *QUOTE_ESTIMATE_WIRE,
]
LEVERAGE_MARKET_WIRE = ["get_market", "market_order_config"]
LEVERAGE_ORACLE_WIRE = ["get_perp_prices", "get_group", "block"]
LEVERAGE_LIMITS_WIRE = [
    "is_after_hours_market",
    "get_group",
    "get_pair_custom_max_leverage",
]
LEVERAGE_WIRE = LEVERAGE_MARKET_WIRE + LEVERAGE_ORACLE_WIRE + LEVERAGE_LIMITS_WIRE
CHAIN = "cataclysm-1"
HEIGHT = "120"
ENTRY = "100.000000000000000000"
SHORT_ENTRY = "100.000000000000000001"
OUTSIDE = "The leverage request is outside the market's collateral or leverage limits."
GUARDED = "The after-hours guard can change or close this position."
UNKNOWN = "Cannot verify that the Perp after-hours guard is inactive."
UNKNOWN_LEVERAGE = (
    "Cannot verify the Perp execution inputs and after-hours guard at one block."
)
STALE = "Perp rejects the leverage change at the observed stale oracle price."
LIQUIDATION = (
    "The observed oracle price has reached the prospective liquidation threshold."
)
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
SHORT_LIQUIDATION = {
    **QUOTED_LIQUIDATION,
    "price": "108.000000000000000001",
    "entry": {**QUOTED_LIQUIDATION["entry"], "price": SHORT_ENTRY},
}


def unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def body(command: CLI) -> dict[str, Any]:
    keep = {"operation": "keep"}
    clear = {"operation": "clear"}
    take_profit = {"operation": "set", "value": "110.0100"}
    stop_loss = {"operation": "set", "value": "90.0100"}
    return {
        TPSL: {"takeProfit": take_profit, "stopLoss": clear},
        CLEAR_TP: {"takeProfit": clear, "stopLoss": keep},
        CLEAR_SL: {"takeProfit": keep, "stopLoss": clear},
        SET_BOTH: {"takeProfit": take_profit, "stopLoss": stop_loss},
        LEVERAGE: {"takeProfit": keep, "stopLoss": keep, "requestedLeverage": "10.00"},
    }[command]


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


TRIGGER_ESTIMATES = estimates(
    {"status": "unchanged", "reason": "This action changes only TP/SL triggers."}
)


@contextmanager
def update_loopback(mode: str) -> Generator[Endpoints]:
    wire: list[WireRequest] = []
    errors: list[str] = []
    submitted: list[str] = []
    short = mode in {"short", "short-liquidation"}
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
        "open_price": SHORT_ENTRY if short else ENTRY,
        "long": not short,
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
        "get_exchange_balance": (
            {}
            if mode == "balance-missing-index"
            else {"collateral_index": "TokenIndex(0)"}
        ),
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
        "get_collateral_price": "1.01",
        "get_perp_prices": {
            "market": {
                "base_per_quote": {
                    "queued": "85",
                    "stale": "85",
                    "liquidation": "85",
                    "cap-missing-height": "85",
                    "cap-wrong-height": "85",
                    "short-liquidation": "110",
                }.get(mode, "99"),
                "base_last_update_time": 995,
                "quote_last_update_time": 980 if mode == "stale" else 995,
            }
        },
        "market_order_config": {"fill_at_next_oracle_price": mode == "queued"},
        "quote_trade": {"data": None, "attributes": [], "events": [], "messages": []},
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
        "get_collateral_price": {"collateral_index": "TokenIndex(0)"},
        "get_perp_prices": {
            "market_index": "MarketIndex(3)",
            "collateral_index": "TokenIndex(0)",
        },
        "market_order_config": {},
        "quote_trade": {
            "trader": ADDRESS,
            "funds": [],
            "action": {
                "update_leverage": {
                    "trade_index": "UserTradeIndex(7)",
                    "new_leverage": "10.00",
                }
            },
        },
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
    pin = (
        "SAI_PARITY_POSITION_V012_PYTHON"
        if released
        else "SAI_PARITY_POSITION_OWNER_PYTHON"
    )
    reference = os.environ.get(pin)
    if reference is None or not Path(reference).is_file():
        pytest.fail(f"Set {pin} to the installed pinned Python sai launcher")
    return [("python", (reference,)), ("rust", cli_pair[1])]


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
    preview = json.loads(result.stdout)
    # The ownership fixture predates CLI confirmations.
    if command != LEVERAGE:
        preview.pop("previewDigest", None)
        preview.pop("positionRoute", None)
    serialized = (json.dumps(preview, sort_keys=command == LEVERAGE) + "\n").encode()
    liq = preview["estimates"]["liquidationPrice"]
    if entry := liq.get("entry"):
        observed_at = entry["observedAt"]
        assert timestamp(observed_at)
        return serialized.replace(json.dumps(observed_at).encode(), b'"<timestamp>"', 1)
    return serialized


@pytest.mark.parametrize(
    ("command", "mode", "suffix", "expected"),
    [
        (TPSL, "default", [], TRIGGER_ESTIMATES),
        (CLEAR_TP, "default", [], TRIGGER_ESTIMATES),
        (CLEAR_SL, "default", [], TRIGGER_ESTIMATES),
        (SET_BOTH, "default", [], TRIGGER_ESTIMATES),
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
            LEVERAGE_WIRE + ["get_fees", "quote_trade"],
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
            dict.fromkeys(estimates({}), unavailable(UNKNOWN_LEVERAGE)),
        ),
        (
            LEVERAGE,
            "queued",
            LEVERAGE_MARKET_WIRE + LEVERAGE_LIMITS_WIRE + ["get_fees"],
            {**estimates(QUOTED_LIQUIDATION), "execution": unavailable(QUEUED)},
        ),
        (
            LEVERAGE,
            "stale",
            LEVERAGE_MARKET_WIRE + LEVERAGE_ORACLE_WIRE,
            {
                **dict.fromkeys(estimates({}), unavailable(STALE)),
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
            LEVERAGE_WIRE + ["get_fees"],
            {
                **dict.fromkeys(estimates({}), unavailable(LIQUIDATION)),
                "execution": {
                    "status": "would_be_rejected",
                    "code": "LiquidationReached",
                    "reason": LIQUIDATION,
                },
            },
        ),
        (
            LEVERAGE,
            "short",
            LEVERAGE_WIRE + ["get_fees", "quote_trade"],
            estimates(SHORT_LIQUIDATION),
        ),
        (
            LEVERAGE,
            "short-liquidation",
            LEVERAGE_WIRE + ["get_fees"],
            {
                **dict.fromkeys(estimates({}), unavailable(LIQUIDATION)),
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
        "clear-tp",
        "clear-sl",
        "set-both",
        "after-hours",
        "wrong-height",
        "group-fallback",
        "override",
        "cap-unavailable",
        "queued",
        "stale",
        "liquidation",
        "short",
        "short-liquidation",
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
    if command == LEVERAGE:
        # Compare the full current preview, including the route and digest.
        implementations = [
            ("python", (str(Path(sys.executable).with_name("sai")),)),
            ("rust", cli_pair[1]),
        ]
    else:
        implementations = runners(cli_pair)
    outputs = []
    for implementation, cli in implementations:
        home = tmp_path / implementation
        with update_loopback(mode) as endpoints:
            seed_home(home, endpoints, "session")
            output = dry_run_bytes(cli, home, endpoints, command)
            document = json.loads(output)
            assert document["addr"] == ADDRESS
            assert document["dryRun"] is True
            assert document["intent"] == intent(command)
            assert document["estimates"] == expected
            wire = PYTHON_QUOTE_WIRE if implementation == "python" else QUOTE_WIRE
            if command == LEVERAGE:
                wire = QUOTE_WIRE[:-1]
            expected_wire = wire + suffix
            if (
                command == LEVERAGE
                and expected["liquidationPrice"]["status"] == "estimate"
                and implementation == "python"
            ):
                # Issue 231 tracks the collateral field against current Python.
                assert document.pop("collateral") == {
                    "beforeUsdc": "20.000000",
                    "afterUsdc": "10.000000",
                    "beforeUsd": "20.2",
                    "afterUsd": "10.1",
                }
                assert document["collateralBaseUnits"] == {
                    "before": "20000000",
                    "after": "10000000",
                }
                assert document.pop("previewDigest") == preview_digest(document)
                document.pop("collateralBaseUnits")
                document["previewDigest"] = preview_digest(document)
                expected_wire += ["get_collateral_price"]
                output = (json.dumps(document, sort_keys=True) + "\n").encode()
            outputs.append(output)
            assert [entry.label for entry in endpoints.wire] == expected_wire
            assert not endpoints.errors
            assert not (home / "sai/trader/intents.sqlite3").exists()
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("mode", ["cap-missing-height", "cap-wrong-height"])
@pytest.mark.parametrize("confirmed", [False, True], ids=["direct", "confirmed"])
def test_missing_pair_cap_still_checks_liquidation(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, confirmed: bool, mode: str
) -> None:
    args = LEVERAGE
    python = (str(Path(sys.executable).with_name("sai")),)
    if confirmed:
        with update_loopback("default") as endpoints:
            home = tmp_path / "preview"
            seed_home(home, endpoints, "session")
            code, preview, stderr = run(
                python, home, endpoints, (*args, "--dry-run"), python=True
            )
            assert (code, stderr) == (0, "")
            args = (*args, "--preview-digest", preview["previewDigest"])
    for index, cli in enumerate((python, cli_pair[1])):
        home = tmp_path / str(index)
        with update_loopback(mode) as endpoints:
            seed_home(home, endpoints, "session")
            code, result, stderr = run(cli, home, endpoints, args, python=index == 0)
            assert not endpoints.errors
            assert all(entry.label != "recordUserIntent" for entry in endpoints.wire)
            assert not rows(home)
            assert (code, stderr) == (4, "")
            error = result["error"]
            assert error["code"] == "PREVIEW_MISMATCH"
            assert error["details"]["preview"]["estimates"]["execution"]["code"] == (
                "LiquidationReached"
            )


def test_confirmed_leverage_with_unavailable_preliminary_balance(
    cli_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    _, rust = cli_pair
    python = (str(Path(sys.executable).with_name("sai")),)
    with update_loopback("default") as endpoints:
        home = tmp_path / "preview"
        seed_home(home, endpoints, "session")
        code, preview, stderr = run(
            python, home, endpoints, (*LEVERAGE, "--dry-run"), python=True
        )
        assert (code, stderr) == (0, "")
        assert not endpoints.errors
        assert not rows(home)

    args = (*LEVERAGE, "--preview-digest", preview["previewDigest"])
    for index, cli in enumerate((python, rust)):
        home = tmp_path / str(index)
        with update_loopback("balance-missing-index") as endpoints:
            seed_home(home, endpoints, "session")
            code, _, stderr = run(cli, home, endpoints, args, python=index == 0)
            assert (code, stderr) == (0, "")
            assert not endpoints.errors
            (request,) = [
                entry.variables["input"]
                for entry in endpoints.wire
                if entry.label == "recordUserIntent"
            ]
            assert {key: request["intent"][key] for key in intent(LEVERAGE)} == intent(
                LEVERAGE
            )
            assert [row[:6] for row in rows(home)] == [
                (request["intentKey"], "UPDATE_TRADE", "accepted", "7", ADDRESS, None)
            ]


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
        reads = (
            QUOTE_WIRE[:-1] + LEVERAGE_WIRE + ["get_fees", "quote_trade"]
            if command == LEVERAGE and implementation == "rust"
            else UPDATE_WIRE
        )
        home = tmp_path / implementation
        with update_loopback(mode) as endpoints:
            seed_home(home, endpoints, "session")
            actual, output, stderr = run(cli, home, endpoints, command, python=False)
            assert (actual, stderr) == (code, "")
            assert [entry.label for entry in endpoints.wire] == [
                *reads,
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
                    *reads,
                    "recordUserIntent",
                    *reads,
                ]
                assert len(rows(home)) == 1
                reconcile = ("intent", "status", ADDRESS, "--key", key)
                assert run(cli, home, endpoints, reconcile, python=False)[0] == 0
                assert rows(home)[0][2:6] == ("terminal", "7", ADDRESS, "terminal")
                assert run(cli, home, endpoints, command, python=False)[0] == 0
                assert [entry.label for entry in endpoints.wire][
                    -(len(reads) + 2) :
                ] == [
                    "userIntents",
                    *reads,
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
