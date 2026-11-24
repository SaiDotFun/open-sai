"""Loopback Keeper transcripts for cross-process trade tests."""

from __future__ import annotations

import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from compare import INTENT_ID, timestamp
from keeper_wire import WireRequest, same_variables
from scenarios import ADDRESS, PROTOCOL

SESSION = PROTOCOL["session_response"]["data"]["createUserIntentSession"]
REQUEST_HASH = "a" * 64
MARKET_INDEX = re.compile(r"MarketIndex\(([0-9]+)\)")
UNSIGNED = re.compile(r"[0-9]+")
DECIMAL = re.compile(r"(0|[1-9][0-9]*)(?:\.([0-9]{1,18}))?")
UINT64_MAX = 2**64 - 1
UINT128_MAX = 2**128 - 1
ATOMS_PER_USDC = 10**12  # Contract Decimal has 18 places, USDC has 6.
Phase = Literal["absent", "pending", "filled"]


@dataclass
class TradeReplay:
    kind: str = "CLOSE_POSITION"
    entity: str = "7"
    change: str = "position_closed_user"
    hold_admission: bool = False
    lose_reply: bool = False
    fill_on_second_poll: bool = False
    phase: Phase = "absent"
    reserved_key: str | None = None
    arrived: threading.Event = field(default_factory=threading.Event, init=False)
    release: threading.Event = field(default_factory=threading.Event, init=False)
    _intents: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _attempts: Counter[str | None] = field(default_factory=Counter, init=False)
    _wait_polls: Counter[str] = field(default_factory=Counter, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def attempts(self) -> dict[str | None, int]:
        with self._lock:
            return dict(self._attempts)

    def admitted_keys(self) -> set[str]:
        with self._lock:
            return set(self._intents)

    def intent(self, key: str) -> dict[str, Any]:
        with self._lock:
            return self._intents[key]

    def __call__(
        self, request: WireRequest, authorization: str | None
    ) -> dict[str, Any] | None:
        if request.label == "recordUserIntent":
            return self._admit(request.variables, authorization)

        if authorization is not None:
            raise ValueError("Outcome replay requires a public read")
        if request.label in {"tradeHistory", "intentTradeHistory"}:
            history_args: dict[str, Any] = {"trader": ADDRESS}
            with self._lock:
                history = [
                    self._history(key)
                    for key in self._intents
                    if self._phase(key) == "filled"
                ]
            if request.label == "intentTradeHistory":
                history_args.update(limit=25, offset=0)
            else:
                # The C4 Rust CLI still uses the public, single-page query.
                for row in history:
                    del row["marketOrder"]
            if not same_variables(request.variables, history_args):
                raise ValueError("Unexpected trade history variables")
            return {"data": {"perp": {"tradeHistory": history}}}

        key = request.variables.get("key")
        variables: dict[str, Any] = {"user": ADDRESS, "key": key}
        if request.label == "waitUserIntents":
            variables["includeAction"] = True
        if not isinstance(key, str) or not same_variables(request.variables, variables):
            raise ValueError("Outcome read did not use the reserved intent identity")
        with self._lock:
            if key != self.reserved_key and key not in self._attempts:
                raise ValueError(
                    "Outcome read did not use the reserved intent identity"
                )
            if request.label == "waitUserIntents":
                self._wait_polls[key] += 1
            intent = self._intents.get(key)
            phase = self._phase(key)
        rows = []
        if intent is not None and phase != "absent":
            row = self._record(
                key, "pending_exec" if phase == "pending" else "exec_pass"
            )
            if request.label == "waitUserIntents":
                row["body"] = intent["body"]
            rows.append(row)
        return {"data": {"userIntents": rows}}

    def _phase(self, key: str) -> Phase:
        if self.fill_on_second_poll and self._wait_polls[key] >= 2:
            return "filled"
        return self.phase

    def _admit(
        self, variables: dict[str, Any], authorization: str | None
    ) -> dict[str, Any] | None:
        envelope = variables.get("input") if isinstance(variables, dict) else None
        key = envelope.get("intentKey") if isinstance(envelope, dict) else None
        with self._lock:
            self._attempts[key if isinstance(key, str) else None] += 1
        if not isinstance(key, str) or not isinstance(envelope, dict):
            raise TypeError("Malformed intent mutation")
        intent = envelope.get("intent")
        stamp = intent.get("tsIntentInit") if isinstance(intent, dict) else None
        address = envelope.get("evmAddress")
        if (
            not key.isascii()
            or not INTENT_ID.fullmatch(key)
            or authorization != f"Bearer {SESSION['token']}"
            or envelope.get("signedTx") is not None
            or (
                address is not None
                and (
                    not isinstance(address, str)
                    or address.strip().lower() not in ("", ADDRESS.lower())
                )
            )
            or not isinstance(intent, dict)
            or intent.get("intentSchemaVersion") != "v0"
            or intent.get("kind") != self.kind
            or not isinstance(stamp, str)
            or not timestamp(stamp)
            or datetime.fromisoformat(stamp) == datetime.min.replace(tzinfo=UTC)
            or not same_variables(
                envelope.get("lifecycle"),
                {"sequence": 1, "lifecycleStep": "jwt_pass", "tsLifecycleStep": stamp},
            )
        ):
            raise ValueError("Unexpected intent admission report")
        self._validate_payload(intent)
        with self._lock:
            previous = self._intents.setdefault(key, intent)
            if not same_variables(intent, previous):
                raise ValueError("Immutable intent differs for the existing key")
        self.arrived.set()
        if self.hold_admission and not self.release.wait(35):
            raise ValueError("Timed out waiting for the test to release admission")
        if self.lose_reply:
            return None
        return {"data": {"recordUserIntent": self._record(key, "jwt_pass")}}

    def _validate_payload(self, intent: dict[str, Any]) -> None:
        # Apply Keeper's report validation and BuildIntentTradeMsg checks before
        # exposing the admission to readers.
        body, target = intent.get("body"), intent.get("target")
        height, block_time = (
            intent.get("blockIntentInit"),
            intent.get("blockTsIntentInit"),
        )
        extensions = intent.get("extensions")
        if (
            (height is not None and (type(height) is not int or height < 0))
            or (block_time is not None and not timestamp(block_time))
            or (extensions is not None and not isinstance(extensions, dict))
        ):
            raise ValueError("Invalid intent metadata")
        if not isinstance(body, dict):
            raise TypeError("Intent body must be an object")
        if self.kind == "OPEN_TRADE":
            if target is not None:
                raise ValueError("OPEN_TRADE must not set target")
            validate_open(body)
            return
        if (
            not isinstance(target, dict)
            or unsigned_int(target.get("userTradeId"), UINT64_MAX) is None
            or target["userTradeId"] != self.entity
        ):
            raise ValueError("Expected target.userTradeId for the replayed position")
        if self.kind in {"CLOSE_POSITION", "CANCEL_PENDING_TRADE"}:
            if body:
                raise ValueError(f"{self.kind} body must be empty")
        elif self.kind == "UPDATE_TRADE":
            validate_update(body)
        elif self.kind == "DECREASE_POSITION_SIZE":
            validate_partial_close(body)
        else:
            raise ValueError("Unsupported trade action in replay")

    def _record(self, key: str, step: str) -> dict[str, Any]:
        return {
            "intentKey": key,
            "kind": self.kind,
            "lifecycleStep": step,
            "execActor": "server",
            "evmAddress": ADDRESS,
            "nibiAddress": None,
            "txHash": REQUEST_HASH.upper(),
            "execBlock": 122 if step == "exec_pass" else 121,
            "execBlockTs": "2026-11-24T12:00:00Z",
            "errorCode": None,
            "errorMsg": None,
        }

    def _history(self, key: str) -> dict[str, Any]:
        return {
            "id": 7,
            "intentKey": key,
            "txHash": REQUEST_HASH.upper(),
            "tradeChangeType": self.change,
            "block": {"block": 122, "block_ts": "2026-11-24T12:00:00Z"},
            "marketOrder": {
                "orderReqBlock": 121,
                "orderReqTxHash": REQUEST_HASH.upper(),
                "orderExecBlock": 122,
                "sequence": 49,
                "execPrice": "100.25",
                "collateralAmount": "4000000",
            },
            "trade": {"id": 7, "trader": ADDRESS},
        }


def unsigned_int(value: object, maximum: int) -> int | None:
    if not isinstance(value, str) or not UNSIGNED.fullmatch(value):
        return None
    digits = value.lstrip("0") or "0"
    if len(digits) > len(str(maximum)):
        return None
    number = int(digits)
    return number if number <= maximum else None


def decimal_atomics(value: object, field: str, *, positive: bool = False) -> int:
    match = DECIMAL.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"Invalid {field} decimal")
    whole, fraction = match.groups()
    number = unsigned_int(whole + (fraction or "").ljust(18, "0"), UINT128_MAX)
    if number is None or (positive and number == 0):
        raise ValueError(f"Invalid {field} decimal")
    return number


def validate_open(body: dict[str, Any]) -> None:
    market = body.get("market")
    index = market.get("marketIndex") if isinstance(market, dict) else None
    match = MARKET_INDEX.fullmatch(index) if isinstance(index, str) else None
    if match is None or unsigned_int(match[1], UINT64_MAX) is None:
        raise ValueError("Invalid market.marketIndex")
    if body.get("orderType") not in ("market", "limit", "stop") or body.get(
        "side"
    ) not in ("long", "short"):
        raise ValueError("Invalid open order type or side")
    amount = decimal_atomics(body.get("amountUsd"), "amountUsd", positive=True)
    if amount % ATOMS_PER_USDC:
        raise ValueError("amountUsd exceeds six-decimal collateral precision")
    for name in ("intentPrice", "leverage"):
        decimal_atomics(body.get(name), name, positive=True)
    for name in ("takeProfit", "stopLoss"):
        if body.get(name) is not None:
            decimal_atomics(body[name], name)
    slippage = decimal_atomics(body.get("maxSlippagePct"), "maxSlippagePct")
    if slippage > 100 * 10**18 or slippage % 100:
        raise ValueError(
            "Slippage must be at most 100% and exact as a contract fraction"
        )


def validate_update(body: dict[str, Any]) -> None:
    changes_trigger = False
    for name in ("takeProfit", "stopLoss"):
        patch = body.get(name)
        if not isinstance(patch, dict):
            raise TypeError(f"{name} patch must be an object")
        operation = patch.get("operation")
        if operation not in ("keep", "clear", "set"):
            raise ValueError(f"Invalid {name} patch")
        value = patch.get("value")
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Invalid {name} patch value")
        if operation == "set":
            decimal_atomics(value, name, positive=True)
        changes_trigger |= operation != "keep"
    leverage = body.get("requestedLeverage")
    if leverage is not None:
        if changes_trigger:
            raise ValueError("TP/SL and leverage changes require separate intents")
        decimal_atomics(leverage, "requestedLeverage", positive=True)
    elif not changes_trigger:
        raise ValueError("UPDATE_TRADE keeps both triggers and leverage")


def validate_partial_close(body: dict[str, Any]) -> None:
    collateral = body.get("collateral")
    if not isinstance(collateral, dict):
        raise TypeError("Partial close requires a collateral object")
    usdc = collateral.get("amountUsdc")
    units = unsigned_int(usdc, UINT128_MAX)
    if units is None or usdc != str(units):
        raise ValueError("Invalid collateral.amountUsdc")
    amount = decimal_atomics(collateral.get("amountUsd"), "collateral.amountUsd")
    if amount != units * ATOMS_PER_USDC:
        raise ValueError("collateral.amountUsdc differs from collateral.amountUsd")
    decimal_atomics(body.get("leverageDelta"), "leverageDelta")
