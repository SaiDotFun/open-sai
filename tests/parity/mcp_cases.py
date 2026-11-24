"""Stdio calls for the Python MCP contract at ea09f0d417a53a71079bb8c5e426005fa29051a4."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scenarios import Endpoints


def handshake(version: str) -> tuple[dict[str, Any], ...]:
    return (
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "sai-parity", "version": "1.0.0"},
            },
        },
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
    )


ACCOUNT_READS = (
    "account_positions",
    "account_history",
    "account_balances",
    "intent_local",
    "intent_status",
)
TRADE_TOOLS = (
    "trade_open",
    "trade_close",
    "trade_tpsl",
    "trade_leverage",
    "trade_margin",
    "trade_partial_close",
)


@dataclass(frozen=True)
class Call:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    expected: str = "ok"
    session: str | None = None

    def request(self, id_: int, endpoints: Endpoints) -> dict[str, Any]:
        return {
            "id": id_,
            "method": "tools/call",
            "params": {
                "name": self.tool,
                "arguments": {
                    key: endpoints.expand(value) if isinstance(value, str) else value
                    for key, value in self.args.items()
                },
            },
        }


OPEN_WITH_BAD_AMOUNT = {
    "market_id": 3,
    "side": "long",
    "order_type": "market",
    "amount": False,
    "leverage": "2",
    "slippage": "1",
    "preview_digest": "fixture",
}
CALLS = {
    "initialize": (),
    "info": (Call("mcp_info"),),
    "inventory": (
        Call("mcp_info"),
        Call("market_list"),
        Call("market_prices"),
    ),
    "reads": (
        Call("account_positions", {"addr": "{addr}"}),
        Call("account_history", {"addr": "{addr}"}),
        Call("account_balances", {"addr": "{addr}"}),
        Call("intent_local", {"addr": "{addr}"}),
        Call("intent_status", {"addr": "{addr}", "key": "{intent}"}),
        Call("intent_status", {"addr": "{addr}", "key": None}),
    ),
    "validation": (
        *(
            Call(tool, {"unexpected": [1]}, "invalid")
            for tool in ("mcp_info", "market_list", "market_prices", *ACCOUNT_READS)
        ),
        *(Call(tool, {"addr": False}, "invalid") for tool in ACCOUNT_READS),
        Call("intent_status", {"addr": "{addr}", "key": {}}, "invalid"),
    ),
    "refusals": (
        *(Call(tool, {"addr": None}, "SESSION_UNAVAILABLE") for tool in ACCOUNT_READS),
        *(
            Call(tool, {"addr": False, "unexpected": 1}, "SIGNED_OUT")
            for tool in TRADE_TOOLS
        ),
    ),
    "keeper-errors": (
        Call("market_list", {}, "KEEPER_ERROR"),
        Call("market_prices", {}, "KEEPER_ERROR"),
        Call("account_positions", {"addr": "{addr}"}, "KEEPER_ERROR"),
        Call("account_history", {"addr": "{addr}"}, "KEEPER_ERROR"),
        Call("intent_status", {"addr": "{addr}", "key": "{intent}"}, "KEEPER_ERROR"),
    ),
    "stream-error": (Call("account_balances", {"addr": "{addr}"}, "STREAM_ERROR"),),
    "session": (
        Call("mcp_info"),
        Call("account_positions"),
        Call("trade_open", OPEN_WITH_BAD_AMOUNT, "invalid"),
        Call("trade_open", OPEN_WITH_BAD_AMOUNT, "SESSION_EXPIRED", session="expired"),
        Call("mcp_info"),
        Call("account_positions"),
        Call(
            "trade_open",
            OPEN_WITH_BAD_AMOUNT,
            "SESSION_UNAVAILABLE",
            session="other-endpoint",
        ),
        Call("mcp_info"),
        Call("account_positions", {}, "SESSION_UNAVAILABLE"),
        Call("account_positions", {"addr": "{addr}"}),
    ),
}
