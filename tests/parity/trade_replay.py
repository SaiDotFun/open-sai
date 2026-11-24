"""Loopback Keeper transcripts for cross-process trade tests."""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from compare import INTENT_ID, timestamp
from keeper_wire import WireRequest, same_variables
from scenarios import ADDRESS, PROTOCOL

SESSION = PROTOCOL["session_response"]["data"]["createUserIntentSession"]
REQUEST_HASH = "a" * 64
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
        if (
            not INTENT_ID.fullmatch(key)
            or authorization != f"Bearer {SESSION['token']}"
            or not isinstance(intent, dict)
            or intent.get("intentSchemaVersion") != "v0"
            or intent.get("kind") != self.kind
            or not timestamp(stamp)
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
        # Keeper's userintent.validateIntent, for the action kinds replayed here.
        body, target = intent.get("body"), intent.get("target")
        if not isinstance(body, dict):
            raise TypeError("Intent body must be an object")
        if self.kind == "OPEN_TRADE":
            if target is not None:
                raise ValueError("OPEN_TRADE must not set target")
            if (
                body.get("orderType") not in ("market", "limit", "stop")
                or body.get("side") not in ("long", "short")
                or not all(
                    nonempty_string(body.get(name))
                    for name in (
                        "intentPrice",
                        "maxSlippagePct",
                        "amountUsd",
                        "leverage",
                    )
                )
            ):
                raise ValueError("Invalid open order body")
            return
        if (
            not isinstance(target, dict)
            or not nonempty_string(target.get("userTradeId"))
            or target["userTradeId"] != self.entity
        ):
            raise ValueError("Expected target.userTradeId for the replayed position")
        if self.kind in {"CLOSE_POSITION", "CANCEL_PENDING_TRADE"} and body:
            raise ValueError(f"{self.kind} body must be empty")
        if self.kind == "UPDATE_TRADE":
            for name in ("takeProfit", "stopLoss"):
                patch = body.get(name)
                operation = patch.get("operation") if isinstance(patch, dict) else None
                if operation not in ("keep", "clear") and (
                    operation != "set"
                    or not nonempty_string(
                        patch.get("value") if isinstance(patch, dict) else None
                    )
                ):
                    raise ValueError(f"Invalid {name} patch")
            leverage = body.get("requestedLeverage")
            if leverage is not None and not nonempty_string(leverage):
                raise ValueError("requestedLeverage must be a nonempty string")
        if self.kind == "DECREASE_POSITION_SIZE" and all(
            body.get(name) is None for name in ("collateral", "sizeDelta")
        ):
            raise ValueError("Position size change requires collateral or sizeDelta")

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


def nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
