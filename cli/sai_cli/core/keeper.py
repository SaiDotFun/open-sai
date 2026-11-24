"""Keeper GraphQL transport and request construction."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import log
from .errors import Code, SaiError

MAINNET_HTTP = "https://sai-keeper.nibiru.fi/query"
MAINNET_WS = "wss://sai-keeper.nibiru.fi/query"

MARKETS = """query { perp { borrowings { marketId price isOpen visible minLeverage maxLeverage minPositionSizeUSD openFeePct closeFeePct baseToken { id symbol name } quoteToken { symbol } collateralToken { id symbol } } } }"""
MARKET = """query($market:Int!, $collateral:Int!) { perp { borrowing(marketId:$market,collateralId:$collateral) { marketId isOpen minLeverage maxLeverage collateralToken { id symbol } } } }"""
PRICES = """query { oracle { tokenPricesUsd(limit: 100) { priceUsd token { id symbol name } lastUpdatedBlock { block block_ts } } } }"""
POSITIONS = """query($trader:String!) { perp { trades(where:{trader:$trader,isOpen:true}, limit:100) { id trader isOpen isLong tradeType collateralAmount leverage openPrice tp sl state { liquidationPrice pnlCollateral pnlPct positionValue } perpBorrowing { marketId baseToken { symbol } collateralToken { symbol } } } } }"""
POSITION = """query($trader:String!, $id:Int!) { perp { trade(trader:$trader,id:$id) { id trader isOpen tradeType collateralAmount leverage perpBorrowing { marketId collateralToken { id symbol } } } } }"""
ROUTE = """query { oneClickTrading { enabled chainId contractAddress } }"""
HISTORY = """query($trader:String!) { perp { tradeHistory(where:{trader:$trader}, limit:50, order_desc:true) { id intentKey tradeChangeType txHash evmTxHash block { block block_ts } trade { id trader } } } }"""
INTENTS = """query($user:String!, $key:String) { userIntents(where:{user:$user, intentKey:$key}, limit:50) { intentKey kind lifecycleStep execActor evmAddress nibiAddress txHash execBlock execBlockTs errorCode errorMsg } }"""
CHALLENGE = """mutation($address:String!) { createUserIntentAuthChallenge(evmAddress:$address) { nonce evmAddress expiresAt chainId typedData } }"""
SESSION = """mutation($signature:String!, $challenge:String!) { createUserIntentSession(signature:$signature, challenge:$challenge) { token evmAddress expiresAt } }"""
SUBMIT = """mutation($input:RecordUserIntentInput!) { recordUserIntent(input:$input) { intentKey kind lifecycleStep execActor evmAddress txHash errorCode errorMsg } }"""

INTENT_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")
GENERATED_INTENT_KEY = re.compile(r"[0-9]{8}T[0-9]{9}Z_[0-9A-HJKMNP-TV-Z]{16}")
REQUEST_HASH = re.compile(r"(?:0[xX])?[0-9a-fA-F]{64}")
# gqlgen's Time scalar emits RFC3339Nano for block and execution timestamps.
BLOCK_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
INTENT_KINDS = frozenset(
    {
        "OPEN_TRADE",
        "CLOSE_POSITION",
        "CANCEL_PENDING_TRADE",
        "UPDATE_TRADE",
        "BRIDGE_USD",
        "DEPOSIT_USD",
        "WITHDRAW_USD",
        "UPDATE_OPEN_ORDER",
        "DECREASE_POSITION_SIZE",
        "INCREASE_POSITION_SIZE",
    }
)
INTENT_STEPS = frozenset(
    {
        "jwt_pass",
        "jwt_failed",
        "intent_start",
        "sign_reject",
        "sign_failed",
        "exec_req_pass",
        "exec_req_fail",
        "pending_exec",
        "exec_pass",
        "exec_fail",
        "exec_finality_observed",
    }
)
TRADE_CHANGES = frozenset(
    {
        "position_opened",
        "limit_order_created",
        "stop_order_created",
        "order_triggered",
        "position_closed_tp",
        "position_closed_sl",
        "position_liquidated",
        "position_closed_user",
        "order_closed_user",
        "tp_updated",
        "sl_updated",
        "tpsl_updated",
        "trade_size_increase",
        "trade_size_decrease",
        "leverage_updated",
        "after_hours_risk_trigger",
    }
)
INTENT_ACTORS = frozenset({"server", "client", "user"})
INTENT_ERROR_CODES = frozenset(
    {
        "SLIPPAGE",
        "simulation_failed",
        "admission_expired",
        "broadcast_rejected",
        "chain_execution_failed",
        "market_order_expired",
        "market_order_fill_failed",
        "market_order_trading_paused",
        "market_order_market_closed",
        "market_order_trade_not_open",
        "market_order_outcome_not_indexed",
        "intent_already_used",
        "below_minimum",
        "insufficient_idle",
    }
)
GRAPHQL_ERROR_CODES = frozenset(
    {
        "GRAPHQL_PARSE_FAILED",
        "GRAPHQL_VALIDATION_FAILED",
        "INTENT_NOT_ACCEPTED",
        "BAD_USER_INPUT",
        "FORBIDDEN",
        "INTERNAL_SERVER_ERROR",
        "UNAUTHENTICATED",
    }
)


def request_hash(value: object) -> tuple[str | None, bool | None]:
    """Return a validated request transaction hash and an invalid-value flag."""
    if value is None:
        return None, None
    if isinstance(value, str) and REQUEST_HASH.fullmatch(value):
        return value.lower(), None
    return None, True


def known_text(value: object, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def intent_error_code(row: Mapping[str, Any]) -> str | None:
    code = row.get("errorCode")
    if (
        code
        or row.get("errorMsg")
        or row.get("lifecycleStep")
        in ("jwt_failed", "sign_reject", "sign_failed", "exec_req_fail", "exec_fail")
    ):
        return known_text(code, INTENT_ERROR_CODES) or Code.KEEPER_ERROR
    return None


def _block_time(value: object) -> str | None:
    if isinstance(value, str) and BLOCK_TIMESTAMP.fullmatch(value):
        try:
            datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            return value
    return None


def _row_id(value: object) -> str | int | None:
    if type(value) is int and 0 <= value < 2**63:
        return value
    if isinstance(value, str) and re.fullmatch(
        r"[0-9]{1,24}(?:[-_:][0-9]{1,24}){0,3}", value
    ):
        return value
    return None


def safe_intent_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """Select scalars whose meaning and format are known to Sai."""
    record: dict[str, Any] = {}
    key, address = row.get("intentKey"), row.get("evmAddress")
    if isinstance(key, str) and GENERATED_INTENT_KEY.fullmatch(key):
        record["intentKey"] = key
    if isinstance(address, str) and is_address(address):
        record["evmAddress"] = address
        nibi = row.get("nibiAddress")
        if (
            isinstance(nibi, str)
            and nibi.startswith("nibi1")
            and canonical_account(nibi) == address.lower()
        ):
            record["nibiAddress"] = nibi
    for field, allowed in (
        ("kind", INTENT_KINDS),
        ("lifecycleStep", INTENT_STEPS),
        ("execActor", INTENT_ACTORS),
    ):
        if value := known_text(row.get(field), allowed):
            record[field] = value
    block = row.get("execBlock")
    if type(block) is int and 0 <= block < 2**63:
        record["execBlock"] = block
    timestamp = row.get("execBlockTs")
    if valid := _block_time(timestamp):
        record["execBlockTs"] = valid
    elif timestamp is not None:
        record["execBlockTsInvalid"] = True
    tx_hash, invalid = request_hash(row.get("txHash"))
    if tx_hash:
        record["txHash"] = tx_hash
    if invalid:
        record["txHashInvalid"] = True
    if code := intent_error_code(row):
        record["errorCode"] = code
        record["errorMsg"] = log.untrusted("Keeper reported an intent error.")
    return log.clean(record)


def safe_intent_history(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return the validated identifiers in a matched trade-history row."""
    history: dict[str, Any] = {}
    if isinstance(key := row.get("intentKey"), str) and GENERATED_INTENT_KEY.fullmatch(
        key
    ):
        history["intentKey"] = key
    if (identifier := _row_id(row.get("id"))) is not None:
        history["id"] = identifier
    if change := known_text(row.get("tradeChangeType"), TRADE_CHANGES):
        history["tradeChangeType"] = change
    for field in ("txHash", "evmTxHash"):
        value, invalid = request_hash(row.get(field))
        if value:
            history[field] = value
        if invalid:
            history[field + "Invalid"] = True
    if isinstance(trade := row.get("trade"), dict):
        history["trade"] = {}
        if (trade_id := _row_id(trade.get("id"))) is not None:
            history["trade"]["id"] = trade_id
        trader = trade.get("trader")
        if canonical_account(trader) is not None:
            history["trade"]["trader"] = trader
    if isinstance(block := row.get("block"), dict):
        history["block"] = {}
        if (height := _row_id(block.get("block"))) is not None:
            history["block"]["block"] = height
        timestamp = block.get("block_ts")
        if valid := _block_time(timestamp):
            history["block"]["block_ts"] = valid
        elif timestamp is not None:
            history["block"]["block_tsInvalid"] = True
    return log.clean(history)


def safe_history_response(data: Mapping[str, Any]) -> dict[str, Any]:
    """Project the account-history response while keeping its GraphQL envelope."""
    perp = data.get("perp")
    if not isinstance(perp, dict):
        return {"perp": None} if perp is None and "perp" in data else {}
    rows = perp.get("tradeHistory")
    if isinstance(rows, list):
        return {
            "perp": {
                "tradeHistory": [
                    safe_intent_history(row) for row in rows if isinstance(row, dict)
                ]
            }
        }
    return {
        "perp": {"tradeHistory": None}
        if rows is None and "tradeHistory" in perp
        else {}
    }


# Names for the log; a query not listed here is logged as `custom`.
OPERATIONS = {
    MARKETS: "markets",
    MARKET: "market",
    PRICES: "prices",
    POSITIONS: "positions",
    POSITION: "position",
    ROUTE: "route",
    HISTORY: "history",
    INTENTS: "intents",
    CHALLENGE: "challenge",
    SESSION: "session",
    SUBMIT: "submit",
}


class KeeperError(SaiError):
    """Keeper rejected a query or transport failed."""

    code = Code.KEEPER_ERROR


class UdUnavailable(KeeperError):
    """ud is installed but could not say whether it has a wallet."""


class SigningFailed(KeeperError):
    """The signing command did not run, exited with an error, or timed out."""


class AdmissionRejected(KeeperError):
    """Keeper confirmed that admission was rejected."""

    code = Code.ADMISSION_REJECTED


class OutcomeUnknown(KeeperError):
    """The server may have accepted the request.

    INTENT_KEY names the intent to reconcile, when one was reserved.
    """

    code = Code.OUTCOME_UNKNOWN

    def __init__(
        self, message: str, intent_key: str | None = None, *, hint: str | None = None
    ) -> None:
        super().__init__(message, intent_key=intent_key, hint=hint)


class NotSent(KeeperError):
    """The connection failed before the request reached Keeper."""

    code = Code.NOT_SENT


# Keeper expires queued intents two minutes after admission. On an empty
# Keeper read the CLI releases an unknown key after that window plus a
# grace period. A later admission can still restore the block.
ADMISSION_EXPIRY_SECONDS = 120
ABSENT_AFTER_SECONDS = ADMISSION_EXPIRY_SECONDS + 180


LOOPBACK = ("localhost", "127.0.0.1", "::1")


def check_url(url: str, label: str, secure: str = "https") -> None:
    """Refuse URL unless it uses SECURE (`https` or `wss`), or the plain
    scheme on loopback, and carries no credentials.

    LABEL names the endpoint in the error, such as `Keeper` or `LCD`.
    """
    plain = secure.removesuffix("s")
    parts = urlsplit(url)
    if parts.scheme != secure and not (
        parts.scheme == plain and parts.hostname in LOOPBACK
    ):
        raise ValueError(
            f"{label} endpoint must use {secure.upper()}, except on loopback"
        )
    if parts.username or parts.password:
        raise ValueError(f"Credentials are not allowed in the {label} URL")


def request(
    endpoint: str,
    query: str,
    variables: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 20,
    *,
    trace_intent: bool = True,
) -> dict[str, Any]:
    """Send one GraphQL request; TIMEOUT applies to each phase in seconds.

    Logs one `keeper.request` event with the operation, the endpoint's
    origin, the outcome, and the duration. For an intent lookup, it also
    records the key unless an audit call disables `trace_intent`.
    Intent and history reads log only known GraphQL codes and an error count.
    """
    started = time.monotonic()
    outcome: dict[str, Any] = {"status": "error", "code": Code.INTERNAL_ERROR}
    try:
        data = _request(endpoint, query, variables, token, timeout, outcome)
        outcome["status"] = "ok"
        del outcome["code"]
        return data
    except NotSent as exc:
        outcome.update(status="not_sent", code=exc.code)
        raise
    except AdmissionRejected as exc:
        outcome.update(status="rejected", code=exc.code)
        raise
    except KeeperError as exc:
        outcome.update(
            error=str(exc), code=Code.OUTCOME_UNKNOWN if query == SUBMIT else exc.code
        )
        raise
    except KeyboardInterrupt:
        if query == SUBMIT:
            outcome["code"] = Code.OUTCOME_UNKNOWN
        raise
    finally:
        _log_request(endpoint, query, variables, token, started, outcome, trace_intent)


def _log_request(
    endpoint: str,
    query: str,
    variables: dict[str, Any] | None,
    token: str | None,
    started: float,
    outcome: dict[str, Any],
    trace_intent: bool,
) -> None:
    """Log one request. Never raises, so it cannot replace the request's
    own error, and parses nothing: the log writer reduces the endpoint to
    its origin later."""
    try:
        account = next(
            (
                value
                for name in ("trader", "user", "address")
                if is_address(value := (variables or {}).get(name))
            ),
            None,
        )
        intent_key = (
            (variables or {}).get("key") if query == INTENTS and trace_intent else None
        )
        log.event(
            "keeper.request",
            "info" if outcome.get("status") == "ok" else "warn",
            operation=OPERATIONS.get(query, "custom"),
            endpoint=log.endpoint(endpoint),
            endpointId=log.endpoint_identity(endpoint),
            addr=account,
            intentKey=intent_key
            if isinstance(intent_key, str) and INTENT_KEY.fullmatch(intent_key)
            else None,
            authenticated=bool(token),
            durationMs=round((time.monotonic() - started) * 1000),
            status=outcome["status"],
            code=outcome.get("code"),
            error=outcome.get("error") if trace_intent else None,
            httpStatus=outcome.get("httpStatus"),
            keeperErrors=outcome.get("keeperErrors")
            if trace_intent or query in (INTENTS, HISTORY)
            else None,
            keeperErrorCount=outcome.get("keeperErrorCount"),
        )
    except Exception:  # noqa: BLE001, S110 - logging must not change the outcome
        pass


def _graphql_errors(errors: Any, *, codes_only: bool) -> list[dict[str, Any]]:
    """Keep at most three GraphQL errors for the log.

    Keeper's message can echo input, such as the signature sent to the
    session mutation, so it is marked untrusted: the log writer cuts it
    short and removes long hex strings and tokens.
    """
    found: list[dict[str, Any]] = []
    for item in errors[:3] if isinstance(errors, list) else []:
        if not isinstance(item, dict):
            continue
        extensions = item.get("extensions")
        code = extensions.get("code") if isinstance(extensions, dict) else None
        if codes_only:
            found.append(
                {"code": known_text(code, GRAPHQL_ERROR_CODES) or Code.KEEPER_ERROR}
            )
            continue
        found.append(
            {
                "code": log.untrusted(str(code)) if code is not None else None,
                "message": log.untrusted(str(item.get("message", ""))),
            }
        )
    return found


def _request(
    endpoint: str,
    query: str,
    variables: dict[str, Any] | None,
    token: str | None,
    timeout: float,
    outcome: dict[str, Any],
) -> dict[str, Any]:
    """Send the request and record what Keeper answered in OUTCOME."""
    try:
        check_url(endpoint, "Keeper")
    except ValueError as exc:
        raise KeeperError(str(exc)) from exc
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = httpx.post(
            endpoint,
            json={"query": query, "variables": variables or {}},
            headers=headers,
            timeout=timeout,
        )
        outcome["httpStatus"] = response.status_code
        response.raise_for_status()
        result = response.json()
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise NotSent("Could not connect to Keeper; the request was not sent") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise KeeperError("Keeper request failed or returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise KeeperError("Keeper returned an invalid response")
    if result.get("errors"):
        errors = result["errors"]
        codes_only = query in (INTENTS, HISTORY)
        outcome["keeperErrors"] = _graphql_errors(errors, codes_only=codes_only)
        if codes_only:
            outcome["keeperErrorCount"] = len(errors) if isinstance(errors, list) else 1
        if (
            query == SUBMIT
            and not result.get("data")
            and isinstance(errors, list)
            and errors
            and all(
                isinstance(item, dict)
                and isinstance(item.get("extensions"), dict)
                and item["extensions"].get("code") == "INTENT_NOT_ACCEPTED"
                for item in errors
            )
        ):
            raise AdmissionRejected(
                "Keeper rejected admission; check inputs and session"
            )
        raise KeeperError("Keeper rejected the request; check inputs and session")
    data = result.get("data")
    if not isinstance(data, dict):
        raise KeeperError("Keeper returned no data")
    return data


def _challenge_hex(value: object) -> str:
    """Normalize a 32-byte challenge; Keeper sends the nonce without 0x."""
    if not isinstance(value, str):
        raise TypeError("Challenge must be a string")
    digits = value.lower().removeprefix("0x")
    if len(digits) != 64 or any(c not in "0123456789abcdef" for c in digits):
        raise ValueError("Challenge must be 32 bytes of hex")
    return digits


def check_session_typed_data(typed_data: dict[str, Any], addr: str, nonce: str) -> None:
    """Refuse typed data that is not Keeper's session challenge for ADDR.

    The signer must never receive a payload the endpoint substituted, such as
    a token permit or an order.
    """
    try:
        domain = typed_data["domain"]
        message = typed_data["message"]
        valid = (
            typed_data["primaryType"] == "UserTradingSession"
            and domain["name"] == "Sai"
            and domain["version"] == "1"
            and isinstance(message["account"], str)
            and message["account"].lower() == addr.lower()
            and _challenge_hex(message["challenge"]) == _challenge_hex(nonce)
        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise KeeperError("Challenge is not a Sai trading session request") from exc
    if not valid:
        raise KeeperError("Challenge is not a Sai trading session request")


def fetch_challenge(endpoint: str, addr: str) -> tuple[dict[str, Any], str]:
    """Return checked typed data to sign and its challenge nonce."""
    challenge = request(endpoint, CHALLENGE, {"address": addr})[
        "createUserIntentAuthChallenge"
    ]
    if challenge["evmAddress"].lower() != addr.lower():
        raise KeeperError("Challenge addr does not match the requested account")
    typed_data = challenge["typedData"]
    check_session_typed_data(typed_data, addr, challenge["nonce"])
    declared = {field["name"] for field in typed_data["types"]["EIP712Domain"]}
    domain = typed_data["domain"]
    typed_data["domain"] = {
        key: value
        for key, value in domain.items()
        if key in declared or value not in (None, "")
    }
    if any(typed_data["domain"].get(key) in (None, "") for key in declared):
        raise KeeperError("Challenge has an empty declared signing domain field")
    return typed_data, challenge["nonce"]


def signed_session(
    endpoint: str,
    addr: str,
    sign_command: list[str],
    *,
    interactive: bool = False,
) -> dict[str, str]:
    """Sign Keeper's challenge with SIGN_COMMAND plus the typed-data file path.

    With INTERACTIVE, the signer keeps the user's terminal for stdin and
    stderr, so it can prompt for a keystore password. Only stdout, the
    signature, is captured. The signature and JWT stay in memory.
    """
    typed_data, nonce = fetch_challenge(endpoint, addr)
    with tempfile.TemporaryDirectory(prefix="sai-auth-") as temp:
        path = Path(temp) / "challenge.json"
        path.write_text(json.dumps(typed_data), encoding="utf-8")
        path.chmod(0o600)
        try:
            proc = subprocess.run(
                [*sign_command, str(path)],
                stdout=subprocess.PIPE,
                stderr=None if interactive else subprocess.PIPE,
                text=True,
                check=True,
                timeout=300 if interactive else 120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SigningFailed("Wallet signing failed") from exc
    signature = proc.stdout.strip()
    if not signature.startswith("0x") or len(signature) != 132:
        raise KeeperError("Wallet signer returned an invalid signature")
    session = request(endpoint, SESSION, {"signature": signature, "challenge": nonce})[
        "createUserIntentSession"
    ]
    if session["evmAddress"].lower() != addr.lower():
        raise KeeperError("Session addr does not match the requested account")
    return session


def wallet_session(endpoint: str, addr: str, wallet: str) -> dict[str, str]:
    """Sign the challenge with a ud wallet; ud supplies the keystore password."""
    return signed_session(
        endpoint,
        addr,
        [
            "ud",
            "evm",
            "wallet",
            "run",
            wallet,
            "--",
            "wallet",
            "sign",
            "--data",
            "--from-file",
        ],
    )


def is_address(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value[2:])


_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GENERATORS = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def canonical_account(value: object) -> str | None:
    """Return the lowercase EVM form of a 20-byte EVM or Nibiru account."""
    if not isinstance(value, str) or not value.isascii():
        return None
    value = value.lower()
    evm = "0x" + value.removeprefix("0x")
    if is_address(evm):
        return evm
    # A Nibiru account has 32 payload symbols and six checksum symbols.
    if not value.startswith("nibi1") or len(value) != 43:
        return None
    try:
        symbols = [_BECH32_CHARSET.index(char) for char in value[5:]]
    except ValueError:
        return None

    # Bech32 checksum (BIP 173), using the Nibiru account prefix.
    prefix = b"nibi"
    expanded = [char >> 5 for char in prefix] + [0] + [char & 31 for char in prefix]
    checksum = 1
    for symbol in (*expanded, *symbols):
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ symbol
        for bit, generator in enumerate(_BECH32_GENERATORS):
            if top & (1 << bit):
                checksum ^= generator
    if checksum != 1:
        return None

    payload = 0
    for symbol in symbols[:-6]:
        payload = (payload << 5) | symbol
    return "0x" + payload.to_bytes(20, "big").hex()


@dataclass(frozen=True)
class UdWallet:
    """Public details from `ud evm wallet show`.

    `cast_state` is ud's Cast keystore state for the name: `matching`,
    `missing`, or `name-conflict`, or None when ud printed none. `ud evm
    wallet run` signs only when it is `matching`.
    """

    addr: str
    cast_state: str | None

    @property
    def can_sign(self) -> bool:
        return self.cast_state == "matching"


def parse_wallet(output: str) -> UdWallet | None:
    """Read the first JSON object in OUTPUT that has an `address`.

    `ud evm wallet show` prints a JSON object, but tolerate log lines or other
    text around it.
    """
    decoder = json.JSONDecoder()
    start = output.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(output, start)
        except ValueError:
            value = None
        if isinstance(value, dict) and is_address(value.get("address")):
            cast = value.get("cast")
            state = cast.get("state") if isinstance(cast, dict) else None
            return UdWallet(value["address"], state if isinstance(state, str) else None)
        start = output.find("{", start + 1)
    return None


def ud_says_unknown(wallet: str, stderr: str) -> bool:
    """Whether `ud evm wallet show` reported exactly "no wallet by that name".

    The whole final line must be ud's missing-wallet message for this name.
    Any other text, even one that mentions an unknown wallet, is a failed
    lookup, so a broken ud never looks like a confirmed absence.
    """
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return False
    pattern = rf"(Error: )?wallet {re.escape(wallet)} is not in pass entry \S+"
    return re.fullmatch(pattern, lines[-1]) is not None


def ud_wallet(wallet: str) -> UdWallet | None:
    """Read a ud wallet's public details, or None when there is none.

    None means ud is not installed or says it has no wallet by that name.
    Any other failure raises `UdUnavailable`, because the caller then cannot
    tell whether ud holds another account under the name.

    `ud evm wallet show` prints public data only. Its stdin is closed so it
    can never prompt. Its stderr is read only to recognize an unknown name,
    and only the addr and Cast state leave this function.
    """
    if shutil.which("ud") is None:
        return None
    try:
        proc = subprocess.run(
            ["ud", "evm", "wallet", "show", wallet],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UdUnavailable(f"`ud evm wallet show {wallet}` did not run") from exc
    if proc.returncode != 0:
        if ud_says_unknown(wallet, proc.stderr):
            return None
        raise UdUnavailable(f"`ud evm wallet show {wallet}` failed")
    found = parse_wallet(proc.stdout)
    if found is None:
        raise UdUnavailable(f"`ud evm wallet show {wallet}` printed no addr")
    return found


def cast_address(account: str) -> str:
    """Read a Foundry keystore addr; cast may prompt for its password."""
    try:
        proc = subprocess.run(
            ["cast", "wallet", "address", "--account", account],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeeperError(f"cast could not read keystore account {account}") from exc
    addr = proc.stdout.strip()
    if not addr.startswith("0x") or len(addr) != 42:
        raise KeeperError("cast returned an invalid addr")
    return addr


def cast_session(endpoint: str, addr: str, account: str) -> dict[str, str]:
    """Sign the challenge with a Foundry keystore through `cast wallet sign`."""
    return signed_session(
        endpoint,
        addr,
        ["cast", "wallet", "sign", "--account", account, "--data", "--from-file"],
        interactive=True,
    )
