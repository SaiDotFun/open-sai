"""Compare CLI confirmations with Python main at 7c7b00d and a loopback Keeper."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest
from python_main_usdc import without_partial_close_usd
from sai_cli.core.confirmation import preview_digest
from sai_cli.core.settings import load as load_settings
from sai_cli.mcp_server.tools import partial_close as mcp_partial_close
from scenarios import ADDRESS, child_env, loopback, seed_home
from test_mcp_stdio import server
from test_mcp_trade import result as mcp_result
from test_trade import CANCEL, CLI, CLOSE, OPEN, rows, run
from trade_replay import TradeReplay

LIMIT = (*OPEN, "--type", "limit")
TPSL = ("trade", "tpsl", "7", "--take-profit", "110")
LEVERAGE = ("trade", "leverage", "7", "--leverage", "1.818181818181818182")
CANCEL_ORDER = (*CLOSE, "--cancel-order")
MARGIN = ("trade", "margin", "7", "--direction", "add", "--amount", "1")
PARTIAL = ("trade", "partial-close", "7", "--percent", "50")


@pytest.fixture
def pair(cli_pair: tuple[CLI, CLI]) -> tuple[CLI, CLI]:
    return (str(Path(sys.executable).with_name("sai")),), cli_pair[1]


def test_unfunded_capped_margin_text_refusal_matches_python(
    pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    args = (*MARGIN, "--max-collateral-usd", "10.5", "--dry-run")
    diagnostics = []
    for index, cli in enumerate(pair):
        with loopback(
            "position-execution-unfunded", leverage_target="1.904761904761904762"
        ) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            result = subprocess.run(
                [*cli, *args],
                cwd=home,
                env=child_env(home, endpoints),
                capture_output=True,
                check=False,
                timeout=15,
            )
            assert result.returncode == 4, (result.stdout, result.stderr)
            assert result.stdout == b""
            assert b"additional margin" in result.stderr
            diagnostics.append(result.stderr)
            assert all(row.label != "recordUserIntent" for row in endpoints.wire)
            assert not rows(home)
            assert not endpoints.errors
    assert diagnostics[0] == diagnostics[1]


@pytest.mark.parametrize(
    ("collateral", "percent", "amount_usdc", "amount_usd"),
    [
        # The fixture's $20 notional leaves $0.20 against Keeper's $1 minimum.
        pytest.param(10_000_000, "99", "9900000", "9.9", id="below-minimum"),
        # The old Decimal calculation rendered the base-unit amount as 1.0…E+26.
        pytest.param(10**28, "1", str(10**26), str(10**20), id="scientific-reduction"),
    ],
)
def test_confirmed_partial_close_matches_preview_and_ledger(
    pair: tuple[CLI, CLI],
    tmp_path: Path,
    collateral: int,
    percent: str,
    amount_usdc: str,
    amount_usd: str,
) -> None:
    args = (*PARTIAL[:-1], percent)
    kind = "DECREASE_POSITION_SIZE"
    expected = {
        "kind": kind,
        "target": {"userTradeId": "7"},
        "body": {
            "collateral": {"amountUsdc": amount_usdc, "amountUsd": amount_usd},
            "leverageDelta": "0",
        },
    }
    digests = []
    for index, cli in enumerate(pair):
        home = tmp_path / str(index)
        replay = TradeReplay(kind)
        with loopback(
            "position-default", position_collateral=collateral, trade_replay=replay
        ) as endpoints:
            seed_home(home, endpoints, "session")
            code, preview, stderr = run(
                cli, home, endpoints, (*args, "--dry-run"), python=index == 0
            )
            assert (code, stderr) == (0, ""), preview
            assert preview["intent"] == expected
            digest = preview["previewDigest"]
            assert digest == preview_digest(preview)
            assert not replay.attempts()
            assert not rows(home)

            code, result, stderr = run(
                cli,
                home,
                endpoints,
                (*args, "--preview-digest", digest),
                python=index == 0,
            )
            assert (code, stderr) == (0, ""), (result, endpoints.errors)
            key = result["intentKey"]
            assert result["status"] == "accepted"
            assert replay.attempts() == {key: 1}
            sent = replay.intent(key)
            assert {field: sent[field] for field in expected} == expected
            [(row_key, *ledger, created_at)] = rows(home)
            assert row_key == key
            assert ledger == [kind, "accepted", "7", ADDRESS.lower(), None]
            assert isinstance(created_at, float)
            assert not endpoints.errors
            digests.append(digest)
    assert digests[0] == digests[1]


@pytest.mark.parametrize(
    ("command", "scenario", "kind", "execution_code"),
    [
        (OPEN, "quote-close-only", "OPEN_TRADE", "MarketClosed"),
        (LIMIT, "quote-close-only", "OPEN_TRADE", "MarketClosed"),
        (CLOSE, "quote-stale", "CLOSE_POSITION", "OraclePriceStale"),
        (LEVERAGE, "quote-close-only", "UPDATE_TRADE", "MarketClosed"),
        (LEVERAGE, "quote-stale", "UPDATE_TRADE", "OraclePriceStale"),
    ],
    ids=[
        "market-open",
        "limit-open",
        "full-close",
        "leverage-closed",
        "leverage-stale",
    ],
)
def test_unconfirmed_trade_checks_execution_before_reserving(
    pair: tuple[CLI, CLI],
    tmp_path: Path,
    command: CLI,
    scenario: str,
    kind: str,
    execution_code: str,
) -> None:
    for index, cli in enumerate(pair):
        home = tmp_path / str(index)
        replay = TradeReplay(kind)
        with loopback(scenario, trade_replay=replay) as endpoints:
            seed_home(home, endpoints, "session")
            code, result, _ = run(cli, home, endpoints, command, python=index == 0)
            assert not endpoints.errors
            assert not replay.attempts()
            assert not rows(home)
            assert code == 4
            error = result["error"]
            assert error["code"] == "PREVIEW_MISMATCH"
            assert (
                error["details"]["preview"]["estimates"]["execution"]["code"]
                == execution_code
            )


@pytest.mark.parametrize("surface", ["plain-cli", "confirmed-cli", "mcp"])
@pytest.mark.parametrize("market_oracle", ["unavailable", "stale"])
@pytest.mark.parametrize("queued", [False, True], ids=["immediate", "queued"])
@pytest.mark.parametrize(
    ("command", "action", "kind", "options"),
    [
        (CLOSE, "close", "CLOSE_POSITION", {}),
        (PARTIAL, "partial_close", "DECREASE_POSITION_SIZE", {"percent": 50}),
        (LEVERAGE, "leverage", "UPDATE_TRADE", {"leverage": LEVERAGE[-1]}),
        (MARGIN, "margin", "UPDATE_TRADE", {"direction": "add", "amount": "1"}),
    ],
    ids=["close", "partial-close", "leverage", "margin"],
)
def test_market_oracle_failure_refuses_before_reservation(
    pair: tuple[CLI, CLI],
    tmp_path: Path,
    surface: str,
    market_oracle: Literal["unavailable", "stale"],
    queued: bool,
    command: CLI,
    action: str,
    kind: str,
    options: dict[str, Any],
) -> None:
    # Perp reads no price at request when it queues a trade, so both runtimes
    # accept one on a stale oracle, and a leverage or margin change without
    # any oracle read. An unreadable oracle blocks only a confirmation; Perp
    # checks the price at execution. Otherwise Python main reads the combined
    # Perp price query instead of the market oracle (README "Fixed USDC
    # Price"), so only the native CLI runs.
    accepted = (
        queued and (market_oracle == "stale" or action in {"leverage", "margin"})
    ) or (surface == "plain-cli" and market_oracle == "unavailable")
    # Python main's partial-close sizing needs the combined price query.
    python = accepted and not (
        action == "partial_close" and market_oracle == "unavailable"
    )
    for index, cli in enumerate(pair) if python else [(1, pair[1])]:
        home = tmp_path / f"submit-{index}"
        submit = command
        args = {"trade_id": 7, **options}
        if surface != "plain-cli":
            # Obtain confirmation while the market still has a healthy rate.
            with loopback("quote-close") as endpoints:
                preview_home = tmp_path / f"preview-{index}"
                if surface == "mcp":
                    with server(
                        Path(cli[0]), preview_home, endpoints, seed="session"
                    ) as stdio:
                        preview = mcp_result(stdio.call(2, f"preview_{action}", **args))
                else:
                    seed_home(preview_home, endpoints, "session")
                    code, preview, _ = run(
                        cli,
                        preview_home,
                        endpoints,
                        (*command, "--dry-run"),
                        python=index == 0,
                    )
                    assert code == 0, preview
                digest = preview["previewDigest"]
                assert not endpoints.errors
            args["preview_digest"] = digest
            submit = (*command, "--preview-digest", digest)

        replay = TradeReplay(kind)
        with loopback(
            "quote-close",
            trade_replay=replay,
            market_oracle=market_oracle,
            queued=queued,
        ) as endpoints:
            if accepted:
                if surface == "mcp":
                    with server(Path(cli[0]), home, endpoints, seed="session") as stdio:
                        reply = mcp_result(stdio.call(2, f"trade_{action}", **args))
                else:
                    seed_home(home, endpoints, "session")
                    code, reply, stderr = run(
                        cli, home, endpoints, submit, python=index == 0
                    )
                    assert (code, stderr) == (0, ""), reply
                assert reply["status"] == "accepted", reply
                assert replay.attempts() == {reply["intentKey"]: 1}
                assert not endpoints.errors
                continue
            if surface == "mcp":
                with server(Path(cli[0]), home, endpoints, seed="session") as stdio:
                    reply = mcp_result(stdio.call(2, f"trade_{action}", **args))
                assert reply["code"] == "PREVIEW_MISMATCH"
                assert reply["quoteStatus"] == "unavailable"
                replacement = reply["preview"]
            else:
                seed_home(home, endpoints, "session")
                code, reply, stderr = run(
                    cli, home, endpoints, submit, python=index == 0
                )
                assert (code, reply["error"]["code"], stderr) == (
                    4,
                    "PREVIEW_MISMATCH",
                    "",
                )
                replacement = reply["error"]["details"]["preview"]
            if market_oracle == "stale":
                assert not queued
                expected = "OraclePriceStale"
            else:
                expected = (
                    "OraclePublicationTimeUnknown"
                    if action in {"close", "partial_close"}
                    else "OraclePriceUnavailable"
                )
            assert replacement["estimates"]["execution"]["code"] == expected
            assert "previewDigest" not in replacement
            labels = [request.label for request in endpoints.wire]
            assert "get_exchange_rate" in labels
            assert "get_collateral_price" not in labels
            assert "recordUserIntent" not in labels
            assert not replay.attempts()
            assert not (home / "sai/trader/intents.sqlite3").exists()
            assert not endpoints.errors


@pytest.mark.parametrize(
    ("preview_args", "submit_args", "scenario", "kind", "argument"),
    [
        (
            CANCEL,
            (*CANCEL[:2], "0_7"),
            "pending-order",
            "CANCEL_PENDING_TRADE",
            "TRADE_ID",
        ),
        (
            PARTIAL,
            (*PARTIAL[:-1], "5_0"),
            "position-execution-ready",
            "DECREASE_POSITION_SIZE",
            "--percent",
        ),
    ],
    ids=["order-id", "percent"],
)
def test_confirmed_trade_rejects_underscores_before_reserving(
    pair: tuple[CLI, CLI],
    tmp_path: Path,
    preview_args: CLI,
    submit_args: CLI,
    scenario: str,
    kind: str,
    argument: str,
) -> None:
    with loopback(scenario) as endpoints:
        home = tmp_path / "preview"
        seed_home(home, endpoints, "session")
        code, preview, stderr = run(
            pair[0], home, endpoints, (*preview_args, "--dry-run"), python=True
        )
        assert (code, stderr) == (0, ""), preview
        assert not endpoints.errors
        digest = preview["previewDigest"]

    results = []
    for index, cli in enumerate(pair):
        replay = TradeReplay(kind)
        with loopback(scenario, trade_replay=replay) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            outcome = run(
                cli,
                home,
                endpoints,
                (*submit_args, "--preview-digest", digest),
                python=index == 0,
            )
            assert not replay.attempts()
            assert not rows(home)
            assert not endpoints.wire
            assert not endpoints.errors
            code, result, stderr = outcome
            assert (code, result["error"]["code"], stderr) == (2, "INVALID_REQUEST", "")
            assert f"'{argument}'" in result["error"]["message"]
            results.append(outcome)
    assert results[0] == results[1]


def test_partial_close_with_unreadable_leverage_cannot_be_confirmed(
    pair: tuple[CLI, CLI], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = "position-malformed-after-hours-cap"
    with loopback(scenario) as endpoints, monkeypatch.context() as env:
        home = tmp_path / "python-preview"
        seed_home(home, endpoints, "session")
        variables = child_env(home, endpoints)
        for key in ("SAI_HOME", "NO_PROXY", "no_proxy"):
            env.setenv(key, variables[key])
        expected = mcp_partial_close(
            load_settings(env=variables), dry_run=True, trade_id=7, percent=50
        )
        assert expected["quote"]["effectiveLeverage"] is None
        assert "previewDigest" not in expected
        assert not endpoints.errors

    for args in (PARTIAL, (*PARTIAL, "--preview-digest", preview_digest(expected))):
        for index, cli in enumerate(pair):
            replay = TradeReplay("DECREASE_POSITION_SIZE")
            with loopback(scenario, trade_replay=replay) as endpoints:
                home = tmp_path / f"submit-{len(args)}-{index}"
                seed_home(home, endpoints, "session")
                code, result, stderr = run(
                    cli, home, endpoints, args, python=index == 0
                )
                assert (code, result["error"]["code"], stderr) == (
                    4,
                    "PREVIEW_MISMATCH",
                    "",
                )
                assert "Cannot verify effective leverage" in result["error"]["message"]
                assert not replay.attempts()
                assert not rows(home)
                assert not endpoints.errors

    for index, cli in enumerate(pair):
        with loopback(scenario) as endpoints:
            home = tmp_path / f"preview-{index}"
            seed_home(home, endpoints, "session")
            code, preview, stderr = run(
                cli, home, endpoints, (*PARTIAL, "--dry-run"), python=index == 0
            )
            assert (code, stderr) == (0, "")
            assert preview["quote"] == expected["quote"]
            assert "previewDigest" not in preview
            assert not endpoints.errors
            assert not rows(home)


@pytest.mark.parametrize(
    ("command", "scenario", "kind", "execution_code"),
    [
        (MARGIN, "position-execution-close-only", "UPDATE_TRADE", "MarketClosed"),
        (
            MARGIN,
            "position-execution-unfunded",
            "UPDATE_TRADE",
            "InsufficientDepositBalance",
        ),
        (
            MARGIN,
            "position-execution-pinned-unfunded",
            "UPDATE_TRADE",
            "InsufficientDepositBalance",
        ),
        (
            MARGIN,
            "position-execution-quote-liquidates",
            "UPDATE_TRADE",
            "LiquidationReached",
        ),
        (
            PARTIAL,
            "position-execution-no-oracle-time",
            "DECREASE_POSITION_SIZE",
            "OraclePublicationTimeUnknown",
        ),
        (
            PARTIAL,
            "position-execution-stale",
            "DECREASE_POSITION_SIZE",
            "OraclePriceStale",
        ),
        (LEVERAGE, "quote-close-only", "UPDATE_TRADE", "MarketClosed"),
        (LEVERAGE, "quote-stale", "UPDATE_TRADE", "OraclePriceStale"),
    ],
    ids=[
        "margin-close-only",
        "margin-unfunded",
        "margin-pinned-balance-unfunded",
        "margin-liquidates-with-borrowing",
        "partial-no-oracle-time",
        "partial-stale",
        "leverage-close-only",
        "leverage-stale-before-liquidation",
    ],
)
def test_position_execution_refuses_before_reserving(
    pair: tuple[CLI, CLI],
    tmp_path: Path,
    command: CLI,
    scenario: str,
    kind: str,
    execution_code: str,
) -> None:
    documents = []
    for index, cli in enumerate(pair):
        ready = "quote-close" if command == LEVERAGE else "position-execution-ready"
        with loopback(ready) as endpoints:
            home = tmp_path / f"preview-{index}"
            seed_home(home, endpoints, "session")
            code, preview, stderr = run(
                cli, home, endpoints, (*command, "--dry-run"), python=index == 0
            )
            assert (code, stderr) == (0, ""), preview
            digest = preview["previewDigest"]
            assert digest == preview_digest(preview)
            assert not endpoints.errors
        replay = TradeReplay(kind)
        with loopback(scenario, trade_replay=replay) as endpoints:
            home = tmp_path / f"submit-{index}"
            seed_home(home, endpoints, "session")
            code, document, stderr = run(
                cli,
                home,
                endpoints,
                (*command, "--preview-digest", digest),
                python=index == 0,
            )
            assert not endpoints.errors
            assert not replay.attempts()
            assert not (home / "sai/trader/intents.sqlite3").exists()
            error = document["error"]
            assert (code, error["code"], stderr) == (4, "PREVIEW_MISMATCH", "")
            assert (
                error["details"]["preview"]["estimates"]["execution"]["code"]
                == execution_code
            )
            assert "previewDigest" not in error["details"]["preview"]
            if index:
                details = error["details"]
                details["preview"] = without_partial_close_usd(details["preview"])
            documents.append(document)
        with loopback(scenario) as endpoints:
            home = tmp_path / f"blocked-preview-{index}"
            seed_home(home, endpoints, "session")
            code, blocked, stderr = run(
                cli, home, endpoints, (*command, "--dry-run"), python=index == 0
            )
            assert (code, stderr) == (0, ""), blocked
            assert not endpoints.errors
            assert "previewDigest" not in blocked
            assert blocked["estimates"]["execution"]["code"] == execution_code
    assert documents[0] == documents[1]


@pytest.mark.parametrize(
    ("command", "scenario", "wrong_balance_at", "preview_unavailable"),
    [
        (MARGIN, "position-execution-ready", "pinned", False),
        (LEVERAGE, "quote-close", "snapshot", False),
        (LEVERAGE, "quote-close", "pinned", False),
        (LEVERAGE, "quote-close", "snapshot", True),
        (LEVERAGE, "quote-close", "pinned", True),
    ],
    ids=[
        "margin-pinned",
        "leverage-snapshot",
        "leverage-pinned",
        "leverage-snapshot-advisory",
        "leverage-pinned-advisory",
    ],
)
def test_confirmed_trade_handles_balance_for_another_collateral(
    pair: tuple[CLI, CLI],
    tmp_path: Path,
    command: CLI,
    scenario: str,
    wrong_balance_at: Literal["snapshot", "pinned"],
    preview_unavailable: bool,
) -> None:
    python, _ = pair
    with loopback(
        scenario, wrong_balance_at=wrong_balance_at if preview_unavailable else None
    ) as endpoints:
        home = tmp_path / "preview"
        seed_home(home, endpoints, "session")
        code, preview, stderr = run(
            python, home, endpoints, (*command, "--dry-run"), python=True
        )
        assert (code, stderr) == (0, "")
        assert not endpoints.errors
        if command == LEVERAGE:
            collateral = preview["collateralBaseUnits"]
            assert collateral["before"] == "10000000"
            assert ("after" in collateral) is not preview_unavailable

    for index, cli in enumerate(pair):
        replay = TradeReplay("UPDATE_TRADE")
        with loopback(
            scenario, trade_replay=replay, wrong_balance_at=wrong_balance_at
        ) as endpoints:
            home = tmp_path / f"submit-{index}"
            seed_home(home, endpoints, "session")
            result = run(
                cli,
                home,
                endpoints,
                (*command, "--preview-digest", preview["previewDigest"]),
                python=index == 0,
            )
            assert not endpoints.errors
            if command == LEVERAGE:
                code, output, stderr = result
                if not preview_unavailable:
                    assert (code, output["error"]["code"], stderr) == (
                        4,
                        "PREVIEW_MISMATCH",
                        "",
                    )
                    assert not replay.attempts()
                    assert not rows(home)
                    continue
                # A preview with only the starting collateral can still be confirmed.
                assert (code, stderr) == (0, "")
                (key,) = replay.admitted_keys()
                assert replay.attempts() == {key: 1}
                assert replay.intent(key)["body"] == preview["intent"]["body"]
                assert [row[:6] for row in rows(home)] == [
                    (key, "UPDATE_TRADE", "accepted", "7", preview["addr"], None)
                ]
                continue
            assert not replay.attempts()
            assert not rows(home)
            assert result == (
                4,
                {
                    "error": {
                        "code": "PREVIEW_MISMATCH",
                        "message": "Position changed. Review the margin change again.",
                        "retryable": True,
                    }
                },
                "",
            )


@pytest.mark.parametrize(
    ("args", "scenario", "kind"),
    [
        (LIMIT, "quote-trigger-fee", "OPEN_TRADE"),
        (CLOSE, "quote-queued", "CLOSE_POSITION"),
        (CANCEL_ORDER, "pending-order", "CANCEL_PENDING_TRADE"),
        (CANCEL, "pending-order", "CANCEL_PENDING_TRADE"),
        (TPSL, "quote-close", "UPDATE_TRADE"),
        (LEVERAGE, "quote-close", "UPDATE_TRADE"),
    ],
    ids=["open", "close", "cancel-order", "cancel-alias", "tpsl", "leverage"],
)
def test_confirmed_trade_rejects_changed_digest_before_reserving(
    pair: tuple[CLI, CLI],
    tmp_path: Path,
    args: CLI,
    scenario: str,
    kind: str,
) -> None:
    expected = {
        "code": "PREVIEW_MISMATCH",
        "message": "The trade no longer matches the confirmed preview. Run with --dry-run to review a new preview before submitting.",
        "retryable": True,
    }
    for index, cli in enumerate(pair):
        replay = TradeReplay(kind, "" if kind == "OPEN_TRADE" else "7")
        # Each command has its own transcript for block-pinned LCD reads.
        with loopback(scenario) as endpoints:
            preview_home = tmp_path / f"preview-{index}"
            seed_home(preview_home, endpoints, "session")
            code, preview, stderr = run(
                cli,
                preview_home,
                endpoints,
                (*args, "--dry-run"),
                python=index == 0,
            )
            assert (code, stderr) == (0, ""), preview
            assert not endpoints.errors
        digest = preview["previewDigest"]
        assert digest == preview_digest(preview)
        for field, old in (("chainId", "old-chain"), ("contractAddress", "nibi1old")):
            with loopback(scenario, trade_replay=replay) as endpoints:
                home = tmp_path / f"{index}-{field}"
                seed_home(home, endpoints, "session")
                stale = {
                    **preview,
                    "positionRoute": {**preview["positionRoute"], field: old},
                }
                assert run(
                    cli,
                    home,
                    endpoints,
                    (*args, "--preview-digest", preview_digest(stale)),
                    python=index == 0,
                ) == (4, {"error": expected}, "")
                assert not rows(home)
                assert not replay.attempts()
                assert not endpoints.errors
        with loopback(scenario, trade_replay=replay) as endpoints:
            # Seed the same account at the new fixture endpoint.
            retry = tmp_path / f"retry-{index}"
            seed_home(retry, endpoints, "session")
            code, accepted, stderr = run(
                cli,
                retry,
                endpoints,
                (*args, "--preview-digest", digest),
                python=index == 0,
            )
            assert (code, accepted.get("status"), stderr) == (0, "accepted", ""), (
                accepted
            )
            key = accepted["intentKey"]
            assert replay.attempts() == {key: 1}
            assert [entry[:4] for entry in rows(retry)] == [
                (key, kind, "accepted", "" if kind == "OPEN_TRADE" else "7")
            ]
            assert not endpoints.errors


@pytest.mark.parametrize(
    ("price", "exit_code", "messages"),
    [
        (
            (),
            1,
            ("Pass the confirmed market preview's referencePrice via --price",) * 2,
        ),
        (
            ("--price", "0"),
            1,
            ("Reference price must be a positive finite decimal",) * 2,
        ),
        (
            ("--price", "1e2"),
            2,
            (
                "Invalid value for '--price': '1e2' is not a valid decimal.",
                "Invalid value for '--price': expected a decimal.",
            ),
        ),
    ],
    ids=["missing", "invalid", "malformed"],
)
def test_confirmed_market_requires_reference_before_keeper_reads(
    pair: tuple[CLI, CLI],
    tmp_path: Path,
    price: CLI,
    exit_code: int,
    messages: tuple[str, str],
) -> None:
    for index, cli in enumerate(pair):
        with loopback("quote-market") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            args = (*OPEN[:-2], *price, "--preview-digest", "stale")
            assert run(cli, home, endpoints, args, python=index == 0) == (
                exit_code,
                {
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": messages[index],
                        "retryable": False,
                    }
                },
                "",
            )
            assert not endpoints.wire
            assert not (home / "sai/trader/intents.sqlite3").exists()


@pytest.mark.parametrize(
    "flags", [("--preview-digest", "stale"), ("--dry-run",)], ids=["submit", "preview"]
)
def test_confirmed_market_rejects_zero_slippage_before_keeper_reads(
    pair: tuple[CLI, CLI], tmp_path: Path, flags: CLI
) -> None:
    message = (
        "Slippage (--slippage) must be greater than 0 and at most 100 percent, "
        "with at most 18 decimal places and only zeros after the 16th place"
    )
    for index, cli in enumerate(pair):
        with loopback("quote-market") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            args = (*OPEN, "--slippage", "0", *flags)
            assert run(cli, home, endpoints, args, python=index == 0) == (
                2,
                {
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": message,
                        "retryable": False,
                    }
                },
                "",
            )
            assert not endpoints.wire
            assert not (home / "sai/trader/intents.sqlite3").exists()


@pytest.mark.parametrize(
    ("market_price", "expected", "slippage"),
    [
        (99, None, "1"),
        (101, None, "1.000000000000000000"),
        (98.99999, "1.0000", "1"),
        (101.25, "1.2500", "1"),
    ],
    ids=["lower-bound", "upper-bound", "below-bound", "above-bound"],
)
def test_confirmed_market_checks_refreshed_price(
    pair: tuple[CLI, CLI],
    tmp_path: Path,
    market_price: float,
    expected: str | None,
    slippage: str,
) -> None:
    for index, cli in enumerate(pair):
        with loopback("quote-market") as endpoints:
            home = tmp_path / f"preview-{index}"
            seed_home(home, endpoints, "session")
            code, preview, stderr = run(
                cli,
                home,
                endpoints,
                (*OPEN, "--slippage", slippage, "--dry-run"),
                python=index == 0,
            )
            assert (code, stderr, preview["referencePrice"]) == (0, "", "100")
            digest = preview["previewDigest"]
            assert not endpoints.errors
        replay = TradeReplay("OPEN_TRADE", "")
        with loopback(
            "quote-market", market_price=market_price, trade_replay=replay
        ) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            code, result, stderr = run(
                cli,
                home,
                endpoints,
                (
                    *OPEN[:-2],
                    "--slippage",
                    slippage,
                    "--price",
                    "100.0000",
                    "--preview-digest",
                    digest,
                ),
                python=index == 0,
            )
            assert stderr == ""
            assert not endpoints.errors
            if expected is None:
                assert (code, result["status"]) == (0, "accepted")
                assert float(result["referencePrice"]) == market_price
                key = result["intentKey"]
                assert replay.attempts() == {key: 1}
                assert (
                    replay.intent(key)["body"]["intentPrice"]
                    == result["referencePrice"]
                )
            else:
                assert code == 4
                assert result == {
                    "error": {
                        "code": "PRICE_MOVED",
                        "message": f"The market price moved {expected}% from the confirmed reference, more than the {slippage}% slippage. Run with --dry-run to review a new preview before submitting.",
                        "retryable": True,
                    }
                }
                assert not replay.attempts()
                assert not (home / "sai/trader/intents.sqlite3").exists()


def test_confirmed_market_rechecks_route_after_price_refresh(
    pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    for index, cli in enumerate(pair):
        with loopback("quote-market") as endpoints:
            home = tmp_path / f"preview-{index}"
            seed_home(home, endpoints, "session")
            code, preview, stderr = run(
                cli, home, endpoints, (*OPEN, "--dry-run"), python=index == 0
            )
            assert (code, stderr) == (0, "")
            assert not endpoints.errors
        with loopback("quote-route-changed", market_price=100) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            assert run(
                cli,
                home,
                endpoints,
                (*OPEN, "--preview-digest", preview["previewDigest"]),
                python=index == 0,
            ) == (
                4,
                {
                    "error": {
                        "code": "PREVIEW_MISMATCH",
                        "message": "The trading route changed while checking the open. Request a new preview before submitting.",
                        "retryable": True,
                    }
                },
                "",
            )
            assert [entry.label for entry in endpoints.wire].count(
                "oneClickTrading"
            ) == 2
            assert not (home / "sai/trader/intents.sqlite3").exists()
            assert not endpoints.errors


@pytest.mark.parametrize("scenario", ["quote-stale", "quote-no-publication"])
def test_verified_close_refuses_unconfirmable_oracle(
    pair: tuple[CLI, CLI], tmp_path: Path, scenario: str
) -> None:
    refusals: list[dict[str, Any]] = []
    for index, cli in enumerate(pair):
        with loopback(scenario) as endpoints:
            home = tmp_path / f"preview-{index}"
            seed_home(home, endpoints, "session")
            code, preview, stderr = run(
                cli, home, endpoints, (*CLOSE, "--dry-run"), python=index == 0
            )
            assert (code, stderr) == (0, "")
            assert "previewDigest" not in preview
            assert not endpoints.errors
        replay = TradeReplay()
        with loopback(scenario, trade_replay=replay) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            code, result, stderr = run(
                cli,
                home,
                endpoints,
                (*CLOSE, "--preview-digest", preview_digest(preview)),
                python=index == 0,
            )
            assert (code, result["error"]["code"], stderr) == (
                4,
                "PREVIEW_MISMATCH",
                "",
            )
            assert "previewDigest" not in result["error"]["details"]["preview"]
            assert not replay.attempts()
            assert not rows(home)
            assert not endpoints.errors
            refusals.append(result)
    assert refusals[0] == refusals[1]


@pytest.mark.parametrize(
    ("scenario", "accepted"),
    [
        ("quote-close-height-failed", False),
        ("quote-close-collateral-changed", True),
    ],
)
def test_confirmed_close_rechecks_position_height(
    pair: tuple[CLI, CLI], tmp_path: Path, scenario: str, accepted: bool
) -> None:
    python, _ = pair
    with loopback("quote-queued") as endpoints:
        home = tmp_path / "preview"
        seed_home(home, endpoints, "session")
        code, preview, stderr = run(
            python, home, endpoints, (*CLOSE, "--dry-run"), python=True
        )
        assert (code, stderr) == (0, "")
        assert not endpoints.errors
        assert not rows(home)

    for index, cli in enumerate(pair):
        replay = TradeReplay()
        with loopback(scenario, trade_replay=replay) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            code, result, stderr = run(
                cli,
                home,
                endpoints,
                (*CLOSE, "--preview-digest", preview["previewDigest"]),
                python=index == 0,
            )
            assert not endpoints.errors
            assert [entry.label for entry in endpoints.wire].count("get_trade_raw") == 2
            if accepted:
                assert (code, stderr) == (0, "")
                (key,) = replay.admitted_keys()
                assert replay.attempts() == {key: 1}
                assert replay.intent(key)["kind"] == "CLOSE_POSITION"
                assert [row[:6] for row in rows(home)] == [
                    (key, "CLOSE_POSITION", "accepted", "7", preview["addr"], None)
                ]
            else:
                assert (code, stderr) == (4, "")
                error = result["error"]
                assert error["code"] == "PREVIEW_MISMATCH"
                assert error["details"]["preview"]["estimates"]["execution"][
                    "code"
                ] == ("OraclePublicationTimeUnknown")
                assert not replay.attempts()
                assert not rows(home)


@pytest.mark.parametrize(
    ("scenario", "reopened"),
    [("quote-cache-reopened", True), ("quote-cache-closed", False)],
)
@pytest.mark.parametrize("implementation", ["python", "rust"])
def test_open_submission_uses_fresh_market_state(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    scenario: str,
    reopened: bool,
    implementation: str,
) -> None:
    python = implementation == "python"
    cli = (
        (str(Path(sys.executable).with_name("sai")),)
        if python
        else request.getfixturevalue("cli_pair")[1]
    )
    replay = TradeReplay("OPEN_TRADE", "")
    home = tmp_path / "home"
    with loopback(scenario, trade_replay=replay, market_price=100) as endpoints:
        seed_home(home, endpoints, "session")
        code, preview, stderr = run(
            cli, home, endpoints, (*OPEN, "--dry-run"), python=python
        )
        assert (code, stderr) == (0, "")
        assert ("previewDigest" in preview) == (not reopened)
        assert not replay.attempts()
        assert not rows(home)

        code, result, stderr = run(cli, home, endpoints, OPEN[:-2], python=python)
        assert not endpoints.errors
        assert [entry.label for entry in endpoints.wire].count("get_market") == 2
        if reopened:
            assert (code, stderr) == (0, "")
            (key,) = replay.admitted_keys()
            assert replay.attempts() == {key: 1}
            assert replay.intent(key)["kind"] == "OPEN_TRADE"
            assert replay.intent(key)["body"] == preview["intent"]["body"]
            assert [row[:6] for row in rows(home)] == [
                (key, "OPEN_TRADE", "accepted", "", preview["addr"], None)
            ]
        else:
            assert (code, stderr) == (4, "")
            error = result["error"]
            assert error["code"] == "PREVIEW_MISMATCH"
            assert error["details"]["preview"]["estimates"]["execution"]["code"] == (
                "MarketClosed"
            )
            assert not replay.attempts()
            assert not rows(home)
