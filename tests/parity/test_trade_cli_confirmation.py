"""Compare CLI confirmations with Python main at 7c7b00d and a loopback Keeper."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

import pytest
from sai_cli.core.confirmation import preview_digest
from sai_cli.core.settings import load as load_settings
from sai_cli.mcp_server.tools import partial_close as mcp_partial_close
from scenarios import child_env, loopback, seed_home
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
    ("command", "scenario", "wrong_balance_at"),
    [
        (MARGIN, "position-execution-ready", "pinned"),
        (LEVERAGE, "quote-close", "snapshot"),
        (LEVERAGE, "quote-close", "pinned"),
    ],
    ids=["margin-pinned", "leverage-snapshot", "leverage-pinned"],
)
def test_confirmed_trade_handles_balance_for_another_collateral(
    pair: tuple[CLI, CLI],
    tmp_path: Path,
    command: CLI,
    scenario: str,
    wrong_balance_at: Literal["snapshot", "pinned"],
) -> None:
    python, _ = pair
    with loopback(scenario) as endpoints:
        home = tmp_path / "preview"
        seed_home(home, endpoints, "session")
        code, preview, stderr = run(
            python, home, endpoints, (*command, "--dry-run"), python=True
        )
        assert (code, stderr) == (0, "")
        assert not endpoints.errors

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
                # A wrong-token balance leaves the standalone estimate unavailable.
                code, _, stderr = result
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
