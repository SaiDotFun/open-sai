"""Standard-library HTTP transport for Keeper fixture replays."""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

POSITION_ROUTE = {
    "oneClickTrading": {
        "enabled": True,
        "chainId": "fixture-1",
        "contractAddress": "nibi1fixture",
    }
}


def position_data(variables: dict[str, Any]) -> dict[str, Any]:
    """Return the selected account's synthetic position in a Keeper response."""
    return {
        "perp": {
            "trade": {
                "id": variables["id"],
                "trader": variables["trader"],
                "isOpen": True,
                "tradeType": "trade",
            }
        }
    }


class KeeperHandler(BaseHTTPRequestHandler):
    def read_json(self) -> dict[str, Any]:
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def reply(self, value: Any) -> None:
        self.send_body(json.dumps(value).encode(), "application/json")

    def send_body(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class PositionKeeperHandler(KeeperHandler):
    """Serve an LCD trade target for tests concerned with intent admission."""

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/cosmos/base/tendermint/v1beta1/node_info":
            self.reply({"default_node_info": {"network": "fixture-1"}})
            return
        prefix = "/cosmwasm/wasm/v1/contract/nibi1fixture/smart/"
        assert path.startswith(prefix)
        request = json.loads(base64.b64decode(unquote(path[len(prefix) :])))
        trade = request["get_trade_raw"]
        self.reply(
            {
                "data": {
                    "user": trade["trader"],
                    "user_trade_index": f"UserTradeIndex({trade['index']})",
                }
            }
        )


@contextmanager
def keeper_server(handler: type[KeeperHandler]) -> Generator[str, None, None]:
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as http:
        thread = threading.Thread(target=http.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{http.server_port}"
        finally:
            http.shutdown()
            thread.join()
