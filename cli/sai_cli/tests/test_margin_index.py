"""Exercise margin removal against a lagging Keeper index."""

from __future__ import annotations

import base64
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import pytest
from click.testing import CliRunner
from sai_cli.core import keeper, storage
from sai_cli.sai_cli import main

ADDR = "0x" + "a" * 40
TRADER = "nibi1424242424242424242424242424242422mldj0"
NETWORK = "cataclysm-1"


@pytest.mark.parametrize("scenario", ["catches_up", "lags", "changes_at_market_lookup"])
def test_remove_margin_when_index_lags_or_changes(scenario):
    indexed_reads = 0
    market_reads = 0
    collateral = 11000000
    submitted: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def reply(self, data):
            height = self.headers.get("x-cosmos-block-height")
            body = json.dumps(
                {"data": data} if self.command == "POST" else data
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if self.command == "GET":
                self.send_header("grpc-metadata-x-cosmos-block-height", height or "900")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path.endswith("/node_info"):
                self.reply({"default_node_info": {"network": NETWORK}})
                return
            encoded = unquote(path.rsplit("/", 1)[-1])
            query = json.loads(base64.b64decode(encoded))
            if "get_trade_raw" in query:
                assert query["get_trade_raw"] == {"trader": TRADER, "index": 7}
                self.reply(
                    {
                        "data": {
                            "user": TRADER,
                            "user_trade_index": "UserTradeIndex(7)",
                            "market_index": "MarketIndex(0)",
                            "collateral_index": "TokenIndex(1)",
                            "collateral_amount": str(collateral),
                            "trade_type": "trade",
                            "is_open": True,
                            "leverage": "5",
                        }
                    }
                )
            elif "is_after_hours_market" in query:
                assert query["is_after_hours_market"] == {
                    "market_index": "MarketIndex(0)"
                }
                self.reply({"data": False})
            else:
                assert query["get_exchange_balance"] == {
                    "user": TRADER,
                    "collateral_index": "TokenIndex(1)",
                }
                self.reply(
                    {
                        "data": {
                            "collateral_index": "TokenIndex(1)",
                            "available_to_trade": "0",
                        }
                    }
                )

        def do_POST(self):
            nonlocal collateral, indexed_reads, market_reads
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            query = request["query"]
            if query == keeper.ROUTE:
                self.reply(
                    {
                        "oneClickTrading": {
                            "enabled": True,
                            "chainId": NETWORK,
                            "contractAddress": "nibi1contract",
                        }
                    }
                )
            elif query == keeper.POSITION:
                assert request["variables"] == {"trader": ADDR, "id": 7}
                indexed_reads += 1
                self.reply(
                    {
                        "perp": {
                            "trade": {
                                "id": 7,
                                "trader": TRADER,
                                "isOpen": True,
                                "tradeType": "trade",
                                "leverage": 5,
                                "collateralAmount": 10000000
                                if scenario == "lags"
                                or (scenario == "catches_up" and indexed_reads == 1)
                                else collateral,
                                "perpBorrowing": {
                                    "marketId": 0,
                                    "collateralToken": {"id": 1, "symbol": "USDC"},
                                },
                            }
                        }
                    }
                )
            elif query == keeper.MARKET:
                market_reads += 1
                if scenario == "changes_at_market_lookup" and market_reads == 2:
                    collateral = 10000000
                self.reply(
                    {
                        "perp": {
                            "borrowing": {
                                "marketId": 0,
                                "isOpen": True,
                                "minLeverage": 1,
                                "maxLeverage": 100,
                                "collateralToken": {"id": 1},
                            }
                        }
                    }
                )
            else:
                assert query == keeper.SUBMIT
                intent = request["variables"]["input"]
                submitted.append(intent)
                self.reply(
                    {
                        "recordUserIntent": {
                            "intentKey": intent["intentKey"],
                            "evmAddress": ADDR,
                            "execActor": "server",
                            "lifecycleStep": "jwt_pass",
                        }
                    }
                )

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
        thread = threading.Thread(target=http.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{http.server_port}"
            endpoint = f"{url}/query"
            storage.save_session(
                {
                    "token": "test-token",
                    "evmAddress": ADDR,
                    "endpoint": endpoint,
                    "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            )
            result = CliRunner().invoke(
                main,
                [
                    "--endpoint",
                    endpoint,
                    "--lcd-endpoint",
                    url,
                    "--json",
                    "trade",
                    "margin",
                    "7",
                    "--direction",
                    "remove",
                    "--amount",
                    "1",
                ],
            )
        finally:
            http.shutdown()
            thread.join()

    assert indexed_reads == 3
    if scenario == "catches_up":
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["quote"]["collateralBefore"] == "11000000"
        assert len(submitted) == 1
        assert submitted[0]["intent"]["body"]["requestedLeverage"] == "5.5"
    elif scenario == "lags":
        assert result.exit_code == 1, result.output
        assert "Retry after Keeper synchronizes" in result.output
        assert submitted == []
        assert storage.list_pending(endpoint, ADDR) == []
    else:
        assert result.exit_code == 4, result.output
        assert json.loads(result.stdout)["error"]["code"] == "PREVIEW_MISMATCH"
        assert submitted == []
        assert storage.list_pending(endpoint, ADDR) == []
