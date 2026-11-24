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
REQUEST_HASH = "A" * 64
EXECUTION_HASH = "B" * 64
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
    _counts: Counter[str] = field(default_factory=Counter, init=False)
    _wait_polls: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def intent(self, key: str) -> dict[str, Any]:
        with self._lock:
            return self._intents[key]

    def __call__(
        self, request: WireRequest, authorization: str | None
    ) -> dict[str, Any] | None:
        if request.label == "recordUserIntent":
            return self._admit(request.variables, authorization)

        with self._lock:
            key = next(iter(self._counts), self.reserved_key)
            body = self._intents.get(key, {}).get("body")
            if request.label == "waitUserIntents":
                self._wait_polls += 1
                if self.fill_on_second_poll and self._wait_polls == 2:
                    self.phase = "filled"
            phase = self.phase

        if key is None or authorization is not None:
            raise ValueError("Outcome replay requires a known key and a public read")
        if request.label == "intentTradeHistory":
            if not same_variables(
                request.variables, {"trader": ADDRESS, "limit": 25, "offset": 0}
            ):
                raise ValueError("Unexpected trade history variables")
            return {"data": {"perp": {"tradeHistory": [self._history(key)]}}}

        variables: dict[str, Any] = {"user": ADDRESS, "key": key}
        if request.label == "waitUserIntents":
            variables["includeAction"] = True
        if not same_variables(request.variables, variables):
            raise ValueError("Outcome read did not use the reserved intent identity")
        row = self._record(key, "pending_exec" if phase == "pending" else "exec_pass")
        if request.label == "waitUserIntents":
            row["body"] = body
        return {"data": {"userIntents": [] if phase == "absent" else [row]}}

    def _admit(
        self, variables: dict[str, Any], authorization: str | None
    ) -> dict[str, Any] | None:
        try:
            envelope = variables["input"]
            key = envelope["intentKey"]
            intent = envelope["intent"]
            lifecycle = envelope["lifecycle"]
            stamp = intent["tsIntentInit"]
            if not isinstance(key, str):
                raise TypeError
        except (KeyError, TypeError):
            raise ValueError("Malformed intent mutation") from None
        with self._lock:
            # Count arrivals before validation, including repeated requests or
            # a second key for the same action.
            self._counts[key] += 1
            self._intents[key] = intent
            first = sum(self._counts.values()) == 1
        if (
            not INTENT_ID.fullmatch(key)
            or authorization != f"Bearer {SESSION['token']}"
            or intent.get("intentSchemaVersion") != "v0"
            or intent.get("kind") != self.kind
            or intent.get("target", {}).get("userTradeId", "") != self.entity
            or not timestamp(stamp)
            or not same_variables(
                lifecycle,
                {"sequence": 1, "lifecycleStep": "jwt_pass", "tsLifecycleStep": stamp},
            )
            or not first
        ):
            raise ValueError("Unexpected or duplicate intent mutation")
        self.arrived.set()
        if self.hold_admission and not self.release.wait(35):
            raise ValueError("Timed out waiting for the test to release admission")
        if self.lose_reply:
            return None
        return {"data": {"recordUserIntent": self._record(key, "jwt_pass")}}

    def _record(self, key: str, step: str) -> dict[str, Any]:
        return {
            "intentKey": key,
            "kind": self.kind,
            "lifecycleStep": step,
            "execActor": "server",
            "evmAddress": ADDRESS,
            "nibiAddress": None,
            "txHash": EXECUTION_HASH if step == "exec_pass" else REQUEST_HASH,
            "execBlock": 122 if step == "exec_pass" else 121,
            "execBlockTs": "2026-11-24T12:00:00Z",
            "errorCode": None,
            "errorMsg": None,
        }

    def _history(self, key: str) -> dict[str, Any]:
        return {
            "id": 7,
            "intentKey": key,
            "txHash": EXECUTION_HASH,
            "tradeChangeType": self.change,
            "block": {"block": 122, "block_ts": "2026-11-24T12:00:00Z"},
            "marketOrder": {"orderReqBlock": 121, "orderReqTxHash": REQUEST_HASH},
            "trade": {"id": 7, "trader": ADDRESS},
        }
