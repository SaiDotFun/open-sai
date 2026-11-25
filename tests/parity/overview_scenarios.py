"""Replay the account overview's Keeper and height-pinned LCD reads."""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote

from keeper_wire import WireRequest, same_variables
from sai_cli.core import keeper
from sai_cli.tests.loopback import fixture
from sai_cli.tests.test_overview import (
    RISK_BLOCK,
    install_contract_trade,
    install_oracle_quote,
    populate_trades,
)

ORACLE = "nibi1oraclefixture"
BLOCKS = "/cosmos/base/tendermint/v1beta1/blocks/"
SMART = "/cosmwasm/wasm/v1/contract/"
PAGINATED = {"overview-paginated", "overview-confirmation-fails"}
PRIVATE_URL = "https://overview-fixture.invalid/private"
RELATIVE_URL = "//overview-fixture.invalid/private?fixture=marker"
SESSION_MARKER = "synthetic-overview-session-marker"
HEX_MARKER = "D" * 64


class OverviewReplay:
    def __init__(self, scenario: str, addr: str, contract: str):
        self.scenario = scenario
        self.addr = addr
        self.contract = contract
        anchor = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=3)
        recorded = fixture("mainnet_account_overview.json")
        self.state: dict[str, Any] = {
            "riskTime": anchor.isoformat(),
            "markTime": (anchor - timedelta(seconds=17)).isoformat(),
            "recorded": recorded,
            "contractTrades": {},
            "oracleRates": {},
            "contractMarkets": {},
            "http": {},
        }
        long, short = populate_trades(self.state)
        if scenario == "overview-stale":
            rate = self.state["oracleRates"][
                long["perpBorrowing"]["baseToken"]["id"], 0
            ]
            rate["rate"]["base_per_quote"] = "45"
            rate["rate"]["base_last_update_time"] -= 400
            rate["base"]["price_block_time_ms"] -= 400_000
        if scenario == "overview-liquidation":
            collateral = {"id": 2, "symbol": "stNIBI"}
            long["perpBorrowing"] = {
                **long["perpBorrowing"],
                "collateralToken": collateral,
            }
            install_contract_trade(self.state, long, mark=100, liquidation=100)
            install_oracle_quote(self.state, collateral["id"], 0, mark="0.04")
        if scenario == "overview-pnl-total":
            extra = {**long, "id": 4}
            for amount, row in enumerate((long, short, extra), 1):
                row.update(
                    isLong=True,
                    leverage=1,
                    openPrice=100,
                    collateralAmount=amount * 1_000_000,
                )
                install_contract_trade(self.state, row, mark=110, liquidation=50)
            self.state["http"][keeper.OVERVIEW_TRADES]["data"]["perp"]["trades"].append(
                extra
            )
        self.balances = recorded["balances"]
        self.balances["data"]["userBalances"].append(
            {
                "usd_value": 12.125,
                "lastUpdatedBlock": RISK_BLOCK,
                "token_info": {"type": "sai", "name": "USDC Unsettled on Sai"},
            }
        )
        self.trades = self.state["http"][keeper.OVERVIEW_TRADES]
        self.orders = self.state["http"][keeper.PENDING_MARKET_ORDERS]
        if scenario in {"overview-fill-check-fails", "overview-fill-check-malformed"}:
            self.trades["data"]["perp"]["trades"].clear()
            self.state["contractTrades"].clear()
        queued = self.orders["data"]["perp"]["marketOrders"][0]
        queued["orderReqTxHash"] = "A" * 64
        if scenario in {
            "overview-queue-valid",
            "overview-queue-mixed",
            "overview-queue-paged",
        }:
            orders = self.orders["data"]["perp"]["marketOrders"]
            for sequence, (side, collateral) in enumerate(
                [
                    ("close", None),
                    ("increase", 42),
                    ("decrease", None),
                    ("update_leverage", None),
                ],
                start=queued["sequence"] + 1,
            ):
                orders.append(
                    {
                        **queued,
                        "sequence": sequence,
                        "side": side,
                        "collateralAmount": collateral,
                    }
                )
            if scenario != "overview-queue-valid":
                orders.insert(1, {**queued, "sequence": "bad sequence"})
                orders.insert(2, {**queued, "sequence": 401, "side": PRIVATE_URL})
            if scenario == "overview-queue-paged":
                orders[3:3] = [
                    {**queued, "sequence": sequence} for sequence in range(501, 599)
                ]
        if scenario == "overview-tainted":
            self.balances["data"]["userBalances"][-1]["lastUpdatedBlock"] = {
                "nested": PRIVATE_URL
            }
            queued["orderReqTxHash"] = PRIVATE_URL
            long["perpBorrowing"]["baseToken"]["symbol"] = SESSION_MARKER
            long["perpBorrowing"]["quoteToken"]["name"] = HEX_MARKER
            short["perpBorrowing"]["baseToken"]["symbol"] = RELATIVE_URL

        if self.scenario in PAGINATED:
            rows = self.trades["data"]["perp"]["trades"]
            pending = rows[-1]
            rows.extend({**pending, "id": index} for index in range(len(rows), 101))

    def keeper(
        self, name: str, variables: Any, requests: list[WireRequest]
    ) -> dict[str, Any] | None:
        if name == "pendingMarketOrders" and self.scenario == "overview-queue-paged":
            calls = sum(request.label == name for request in requests)
            offset = 0 if calls in (1, 3) else 100
            expected = {"trader": self.addr, "limit": 100, "offset": offset}
            if calls not in (1, 2, 3, 4) or not same_variables(variables, expected):
                raise ValueError("Unexpected queued-order pagination request")
            rows = self.orders["data"]["perp"]["marketOrders"]
            return {"data": {"perp": {"marketOrders": rows[offset : offset + 100]}}}
        if name == "overviewTrades" and self.scenario in PAGINATED:
            calls = sum(request.label == name for request in requests)
            # First scan and confirmation each require offsets 0 and 100.
            offset = 0 if calls in (1, 3) else 100
            expected = {"trader": self.addr, "limit": 100, "offset": offset}
            if calls not in (1, 2, 3, 4) or not same_variables(variables, expected):
                raise ValueError("Unexpected pagination request for overviewTrades")
            if self.scenario == "overview-confirmation-fails" and calls == 4:
                return {"errors": [{"message": "confirmation page unavailable"}]}
            rows = self.trades["data"]["perp"]["trades"]
            return {"data": {"perp": {"trades": rows[offset : offset + 100]}}}
        args = {
            "overviewTrades": {"trader": self.addr, "limit": 100, "offset": 0},
            "pendingMarketOrders": {"trader": self.addr, "limit": 100, "offset": 0},
            "tokenPricesUsd": {},
            "userIntents": {"user": self.addr},
        }
        if name not in args:
            return None
        if not same_variables(variables, args[name]):
            raise ValueError(f"Wrong Keeper variables for {name}")
        if self.scenario == "overview-partial" and name in {
            "pendingMarketOrders",
            "tokenPricesUsd",
        }:
            return {"errors": [{"message": "synthetic unavailable section"}]}
        if name == "userIntents" and self.scenario in {
            "overview-fill-check-fails",
            "overview-fill-and-lcd-fail",
        }:
            return {"errors": [{"message": "synthetic fill check failure"}]}
        if name == "userIntents" and self.scenario == "overview-fill-check-malformed":
            return {"data": {"userIntents": [{}]}}
        return {
            "overviewTrades": self.trades,
            "pendingMarketOrders": self.orders,
            "tokenPricesUsd": fixture("mainnet_prices_http.json"),
            "userIntents": {"data": {"userIntents": []}},
        }[name]

    def lcd(
        self, path: str, query_string: str, requested_height: str | None
    ) -> tuple[WireRequest, dict[str, Any], int | None] | None:
        if path.startswith(BLOCKS):
            height = path.removeprefix(BLOCKS)
            if height != "latest" or requested_height is not None:
                raise ValueError("Unexpected overview block lookup")
            self.check_nonce(query_string)
            return (
                WireRequest("lcd:latest"),
                {
                    "block": {
                        "header": {
                            "height": str(RISK_BLOCK),
                            "chain_id": "cataclysm-1",
                            "time": self.state["riskTime"],
                        }
                    }
                },
                None,
            )
        if not path.startswith(SMART):
            return None
        self.check_nonce(query_string)
        if requested_height != str(RISK_BLOCK):
            raise ValueError("Overview smart query was not pinned to the anchor height")
        contract, encoded = path.removeprefix(SMART).split("/smart/")
        query = json.loads(base64.b64decode(unquote(encoded), validate=True))
        [name] = query
        args = query[name]
        expected = {
            "get_base_size_seed_status": {},
            "live_trades_by_user": {
                "trader": self.addr,
                "start_after": None,
                "limit": 100,
            },
            "get_trade": {"trader": self.addr, "index": args.get("index")},
            "get_market": {"index": args.get("index")},
            "get_oracle_address": {},
            "get_exchange_rate_stamped": {
                "base": args.get("base"),
                "quote": args.get("quote"),
            },
            "get_group": {"index": "GroupIndex(0)"},
        }
        if not same_variables(args, expected[name]):
            raise ValueError(f"Wrong LCD variables for {name}")
        served = RISK_BLOCK
        if name == "get_trade" and self.scenario == "overview-wrong-height":
            served -= 1
        if name == "get_trade":
            data = self.state["contractTrades"][args["index"]]
            if self.scenario == "overview-lcd-malformed" and args["index"] == 0:
                data = {"trade": {}}
        elif name == "get_market":
            data = self.state["contractMarkets"][args["index"]]
        elif name == "get_exchange_rate_stamped":
            data = self.state["oracleRates"][args["base"], args["quote"]]
        else:
            data = {
                "get_base_size_seed_status": "complete",
                "live_trades_by_user": [
                    {"trade_index": index}
                    for index in sorted(self.state["contractTrades"])
                ],
                "get_oracle_address": ORACLE,
                "get_group": {"stale_price_expiration_secs": 300},
            }[name]
        if contract != (
            ORACLE if name == "get_exchange_rate_stamped" else self.contract
        ):
            raise ValueError("Overview queried the wrong contract")
        return WireRequest(name, variables=args), {"data": data}, served

    @staticmethod
    def check_nonce(query: str) -> None:
        [nonce] = parse_qs(query, strict_parsing=True)["_sai_query"]
        if not re.fullmatch(r"[a-f0-9]{32}", nonce):
            raise ValueError("Expected a fresh LCD query nonce")
