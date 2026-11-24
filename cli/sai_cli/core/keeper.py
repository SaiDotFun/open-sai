"""Keeper GraphQL transport and request construction."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import log

MAINNET_HTTP = "https://sai-keeper.nibiru.fi/query"
MAINNET_WS = "wss://sai-keeper.nibiru.fi/query"

MARKETS = """query { perp { borrowings { marketId price isOpen visible minLeverage maxLeverage minPositionSizeUSD openFeePct closeFeePct baseToken { id symbol name } quoteToken { symbol } collateralToken { id symbol } } } }"""
MARKET = """query($market:Int!, $collateral:Int!) { perp { borrowing(marketId:$market,collateralId:$collateral) { marketId isOpen minLeverage maxLeverage collateralToken { id symbol } } } }"""
PRICES = """query { oracle { tokenPricesUsd(limit: 100) { priceUsd token { id symbol name } lastUpdatedBlock { block block_ts } } } }"""
POSITIONS = """query($trader:String!) { perp { trades(where:{trader:$trader,isOpen:true}, limit:100) { id trader isOpen isLong tradeType collateralAmount leverage openPrice tp sl state { liquidationPrice pnlCollateral pnlPct positionValue } perpBorrowing { marketId baseToken { symbol } collateralToken { symbol } } } } }"""
POSITION = """query($trader:String!, $id:Int!) { perp { trade(trader:$trader,id:$id) { id trader isOpen tradeType collateralAmount leverage perpBorrowing { marketId collateralToken { id symbol } } } } }"""
ROUTE = """query { oneClickTrading { enabled chainId contractAddress } }"""
HISTORY = """query($trader:String!) { perp { tradeHistory(where:{trader:$trader}, limit:50, order_desc:true) { id intentKey tradeChangeType txHash evmTxHash block { block block_ts } trade { id trader } } } }"""
INTENT_HISTORY = """query($trader:String!, $limit:Int!, $offset:Int!) {
  perp {
    tradeHistory(where:{trader:$trader}, order_by:sequence, order_desc:true,
                 limit:$limit, offset:$offset) {
      id intentKey tradeChangeType txHash evmTxHash block { block block_ts }
      marketOrder { orderReqBlock orderReqTxHash orderExecBlock sequence execPrice collateralAmount }
      openingFeeUsd closingFeeUsd borrowingFeeUsd
      trade {
        id trader tradeType isOpen isLong collateralAmount openCollateralAmount
        leverage openPrice closePrice tp sl openBlock { block } closeBlock { block }
        perpBorrowing { marketId collateralToken { symbol } }
      }
    }
  }
}"""
INTENTS = """query($user:String!, $key:String) { userIntents(where:{user:$user, intentKey:$key}, limit:50) { intentKey kind lifecycleStep execActor evmAddress nibiAddress txHash execBlock execBlockTs errorCode errorMsg } }"""
CHALLENGE = """mutation($address:String!) { createUserIntentAuthChallenge(evmAddress:$address) { nonce evmAddress expiresAt chainId typedData } }"""
SESSION = """mutation($signature:String!, $challenge:String!) { createUserIntentSession(signature:$signature, challenge:$challenge) { token evmAddress expiresAt } }"""
SUBMIT = """mutation($input:RecordUserIntentInput!) { recordUserIntent(input:$input) { intentKey kind lifecycleStep execActor evmAddress txHash errorCode errorMsg } }"""
# Names for the log; a query not listed here is logged as `custom`.
OPERATIONS = {
    MARKETS: "markets",
    MARKET: "market",
    PRICES: "prices",
    POSITIONS: "positions",
    POSITION: "position",
    ROUTE: "route",
    HISTORY: "history",
    INTENT_HISTORY: "history",
    INTENTS: "intents",
    CHALLENGE: "challenge",
    SESSION: "session",
    SUBMIT: "submit",
}


class KeeperError(Exception):
    """Keeper rejected a query or transport failed."""


class UdUnavailable(KeeperError):
    """ud is installed but could not say whether it has a wallet."""


class SigningFailed(KeeperError):
    """The signing command did not run, exited with an error, or timed out."""


class AdmissionRejected(KeeperError):
    """Keeper confirmed that admission was rejected."""

    def __init__(self, message: str, intent_key: str | None = None) -> None:
        super().__init__(message)
        self.intent_key = intent_key


class OutcomeUnknown(KeeperError):
    """The server may have accepted the request.

    INTENT_KEY names the intent to reconcile, when one was reserved.
    """

    def __init__(self, message: str, intent_key: str | None = None) -> None:
        super().__init__(message)
        self.intent_key = intent_key


class NotSent(KeeperError):
    """The connection failed before the request reached Keeper."""


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
) -> dict[str, Any]:
    """Send one GraphQL request; TIMEOUT applies to each phase in seconds.

    Logs one `keeper.request` event with the operation, the endpoint's
    origin, the outcome, and the duration. The variables, the token,
    and the response data are never logged.
    """
    started = time.monotonic()
    outcome: dict[str, Any] = {"status": "error"}
    try:
        data = _request(endpoint, query, variables, token, timeout, outcome)
        outcome["status"] = "ok"
        return data
    except NotSent:
        outcome["status"] = "not_sent"
        raise
    except AdmissionRejected:
        outcome["status"] = "rejected"
        raise
    except KeeperError as exc:
        outcome["error"] = str(exc)
        raise
    finally:
        _log_request(endpoint, query, variables, token, started, outcome)


def _log_request(
    endpoint: str,
    query: str,
    variables: dict[str, Any] | None,
    token: str | None,
    started: float,
    outcome: dict[str, Any],
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
        log.event(
            "keeper.request",
            "info" if outcome.get("status") == "ok" else "warn",
            operation=OPERATIONS.get(query, "custom"),
            endpoint=log.endpoint(endpoint),
            addr=account,
            authenticated=bool(token),
            durationMs=round((time.monotonic() - started) * 1000),
            **outcome,
        )
    except Exception:  # noqa: BLE001, S110 - logging must not change the outcome
        pass


def _graphql_errors(errors: Any) -> list[dict[str, Any]]:
    """The code and message of the first few GraphQL errors, for the log.

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
        outcome["keeperErrors"] = _graphql_errors(errors)
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
