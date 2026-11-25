"""Stdio calls for the Python MCP contract at 1c671b231b0fc968a44ec8dd520e67ea28e25b7f."""

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


REJECTED_AUTH_ADDRESS = "https://keeper.invalid/private/query?key=fixture-mcp-addr"

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

# The public v0.1.2 inventory supplies the only readable tool labels in reports.
RELEASE_MCP_TOOL_NAMES = frozenset(
    {
        *ACCOUNT_READS,
        *TRADE_TOOLS,
        *(f"preview_{tool.removeprefix('trade_')}" for tool in TRADE_TOOLS),
        "account_overview",
        "auth_list",
        "auth_status",
        "auth_use",
        "intent_wait",
        "logs_intent",
        "market_list",
        "market_prices",
        "mcp_info",
    }
)


@dataclass(frozen=True)
class Call:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    expected: str = "ok"
    session: str | None = None
    switch_active: str | None = None
    outcome: str | None = None

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
# Remove with the temporary mcp_info exception once trade_open is implemented.
# The amount and digest cannot produce a valid trade.
DEFERRED_TRADE_PROBE = Call(
    "trade_open",
    {
        **OPEN_WITH_BAD_AMOUNT,
        "amount": "invalid-decimal",
        "preview_digest": "invalid-digest",
    },
    "NOT_IMPLEMENTED",
)

CALLS = {
    "initialize": (),
    "info": (Call("mcp_info"),),
    "inventory": (
        Call("mcp_info"),
        Call("market_list"),
        Call("market_prices"),
    ),
    "number-spelling": (Call("market_prices"),),
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
    "auth-list": (
        Call("auth_list"),
        Call("auth_list", {"unexpected": REJECTED_AUTH_ADDRESS}, "invalid"),
        Call("auth_list", session="expired"),
    ),
    "auth-status": (
        Call("auth_status"),
        Call("auth_status", {"addr": False}, "invalid"),
        Call("auth_status", {"addr": [REJECTED_AUTH_ADDRESS]}, "invalid"),
        Call("auth_status", {"address": REJECTED_AUTH_ADDRESS}, "invalid"),
        Call("auth_status", {"addr": "{other_addr}"}, "SESSION_UNAVAILABLE"),
        Call("auth_status", {"addr": REJECTED_AUTH_ADDRESS}, "SESSION_UNAVAILABLE"),
        Call("auth_status", expected="SESSION_EXPIRED", session="expired"),
        Call("auth_status", expected="SESSION_UNAVAILABLE", session="other-endpoint"),
    ),
    "auth-use": (
        Call("auth_use", expected="invalid"),
        Call("auth_use", {"addr": "{addr}", "address": "{other_addr}"}, "invalid"),
        Call("auth_use", {"addr": "not-connected"}, "INVALID_REQUEST"),
        Call("auth_use", {"addr": "{addr}"}),
        Call("auth_status"),
        Call("auth_status", switch_active="{other_addr}"),
        Call("auth_status", {"addr": "{addr}"}),
        Call("auth_list"),
    ),
    "auth-use-invalid-expiry": (
        Call("auth_list", expected="SESSION_UNAVAILABLE"),
        Call("auth_use", {"addr": "{other_addr}"}, "SESSION_UNAVAILABLE"),
        Call("auth_status", expected="SESSION_UNAVAILABLE"),
    ),
    "auth-redaction": (
        Call("auth_list"),
        Call("auth_status", {"addr": "{addr}"}),
        Call("auth_use", {"addr": "{addr}"}),
    ),
    "overview-validation": (
        Call("account_overview", {"addr": False}, "invalid"),
        Call("account_overview", {"address": "{addr}"}, "invalid"),
    ),
    "wait-validation": (
        Call("intent_wait", expected="invalid"),
        Call("intent_wait", {"key": "{intent}", "timeout": []}, "invalid"),
        Call("intent_wait", {"key": "{intent}", "timeout": "later"}, "invalid"),
        Call("intent_wait", {"key": "{intent}", "deadline": 1}, "invalid"),
        Call("intent_wait", {"key": "{intent}", "timeout": 0}, "INVALID_REQUEST"),
        Call("intent_wait", {"key": "{intent}", "addr": "invalid"}, "INVALID_REQUEST"),
        Call("intent_wait", {"key": "{intent}"}, "SESSION_UNAVAILABLE"),
    ),
    "wait-final": (Call("intent_wait", {"key": "{intent}"}, outcome="filled"),),
    "wait-pending": (
        Call(
            "intent_wait",
            {"key": "{intent}", "addr": "{addr}", "timeout": "7e-1"},
            "OUTCOME_UNKNOWN",
            outcome="unknown",
        ),
    ),
    "logs-validation": (
        Call("logs_intent", expected="invalid"),
        Call("logs_intent", {"key": "{intent}", "offline": "later"}, "invalid"),
        Call("logs_intent", {"key": "{intent}", "before": []}, "invalid"),
        Call("logs_intent", {"key": "{intent}", "token": True}, "invalid"),
    ),
    "refusals": (
        Call("auth_status", {"addr": False}, "invalid"),
        Call("auth_status", {"addr": [REJECTED_AUTH_ADDRESS]}, "invalid"),
        Call("auth_status", {"address": REJECTED_AUTH_ADDRESS}, "invalid"),
        Call("auth_status", expected="SIGNED_OUT"),
        Call("auth_list"),
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
