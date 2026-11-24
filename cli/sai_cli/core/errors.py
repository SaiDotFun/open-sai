"""Failure codes shared by the CLI, MCP tools, and logs."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any


class Code(StrEnum):
    SIGNED_OUT = "SIGNED_OUT"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_UNAVAILABLE = "SESSION_UNAVAILABLE"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    ADMISSION_REJECTED = "ADMISSION_REJECTED"
    NOT_SENT = "NOT_SENT"
    PREVIEW_MISMATCH = "PREVIEW_MISMATCH"
    PRICE_MOVED = "PRICE_MOVED"
    RECENT_OPEN_PENDING = "RECENT_OPEN_PENDING"
    INVALID_REQUEST = "INVALID_REQUEST"
    KEEPER_ERROR = "KEEPER_ERROR"
    INCOMPLETE_DATA = "INCOMPLETE_DATA"
    STREAM_ERROR = "STREAM_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    UNSUPPORTED = 3
    REJECTED = 4
    UNKNOWN = 5
    SIGN_IN = 6


EXIT_BY_CODE = {
    Code.SIGNED_OUT: ExitCode.SIGN_IN,
    Code.SESSION_EXPIRED: ExitCode.SIGN_IN,
    Code.OUTCOME_UNKNOWN: ExitCode.UNKNOWN,
    Code.ADMISSION_REJECTED: ExitCode.REJECTED,
    Code.NOT_SENT: ExitCode.REJECTED,
    Code.PREVIEW_MISMATCH: ExitCode.REJECTED,
    Code.PRICE_MOVED: ExitCode.REJECTED,
}
SIGN_IN = "sai auth login NAME"
HINT_BY_CODE = {Code.SIGNED_OUT: SIGN_IN, Code.SESSION_EXPIRED: SIGN_IN}


class SaiError(Exception):
    """A failure with a stable code and optional recovery instructions."""

    code: Code = Code.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        code: Code | None = None,
        *,
        hint: str | None = None,
        intent_key: str | None = None,
        exit_code: ExitCode | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.hint = hint if hint is not None else HINT_BY_CODE.get(self.code)
        self.intent_key = intent_key
        self.details = details
        self.exit_code = (
            exit_code
            if exit_code is not None
            else EXIT_BY_CODE.get(self.code, ExitCode.ERROR)
        )

    @property
    def retryable(self) -> bool:
        return self.exit_code == ExitCode.REJECTED

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.intent_key is not None:
            value["intentKey"] = self.intent_key
        if self.hint is not None:
            value["hint"] = self.hint
        if self.details is not None:
            value["details"] = self.details
        return value


class SessionError(SaiError, ValueError):
    """A local session is missing, expired, or unusable."""

    code = Code.SESSION_UNAVAILABLE


class McpStdioError(SaiError):
    """An MCP server failure after the transport took ownership of stdout."""


def describe(exc: Exception) -> SaiError:
    """Classify an exception without exposing unexpected exception details."""
    if isinstance(exc, SaiError):
        return exc
    if isinstance(exc, ValueError):
        return SaiError(str(exc), Code.INVALID_REQUEST)
    if isinstance(exc, (KeyError, TypeError)):
        return SaiError(
            f"Keeper or contract data was missing or malformed ({type(exc).__name__})",
            Code.INCOMPLETE_DATA,
        )
    return SaiError("Unexpected error in Sai", Code.INTERNAL_ERROR)
