"""Compare trade estimates with Python main and the released signed-out preview."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from scenarios import child_env, loopback, seed_home
from test_trade import CANCEL, CLI, CLOSE, OBSERVED_AT, OPEN, rows, run

OPEN_READS = (
    "borrowings oneClickTrading node_info get_market get_group "
    "get_pair_custom_max_leverage get_fees"
)
OWNER_READS = "trade oneClickTrading node_info get_trade_raw borrowing"
POSITION_READS = OWNER_READS + " get_exchange_balance get_trade_raw get_market"
ORACLE_READS = POSITION_READS + " get_oracle_address get_exchange_rate"
FRESH_READS = ORACLE_READS + " market_order_config get_group"
CLOSE_READS = FRESH_READS + " get_liquidation_price is_after_hours_market"
STALE_READS = FRESH_READS + " block"


@pytest.fixture
def estimate_pair(cli_pair: tuple[CLI, CLI]) -> tuple[CLI, CLI]:
    reference = os.environ.get("SAI_PARITY_ESTIMATES_PYTHON")
    if not reference or not Path(reference).is_file():
        pytest.fail("Set SAI_PARITY_ESTIMATES_PYTHON to the Python v0.1.2 launcher")
    return (reference,), cli_pair[1]


@pytest.mark.parametrize(
    ("args", "seed", "scenario", "wire", "status_field", "status"),
    [
        (OPEN, "empty", "quote-market", OPEN_READS, "fees", "estimate"),
        (OPEN, "empty", "quote-usdc-oracle-outage", OPEN_READS, "fees", "estimate"),
        (OPEN, "empty", "quote-usdc-oracle-off-peg", OPEN_READS, "fees", "estimate"),
        (OPEN[:-2], "empty", "quote-market", OPEN_READS, "worstCaseFill", "bound"),
        (
            (*OPEN, "--type", "limit"),
            "empty",
            "quote-trigger-fee",
            OPEN_READS,
            "fees",
            "estimate",
        ),
        (
            (*OPEN, "--type", "stop", "--side", "short"),
            "empty",
            "quote-trigger-fee",
            OPEN_READS,
            "worstCaseFill",
            "bound",
        ),
        (
            (*OPEN, "--type", "limit"),
            "empty",
            "quote-close-only",
            "borrowings oneClickTrading node_info get_market",
            "worstCaseFill",
            "unavailable",
        ),
        (
            OPEN,
            "empty",
            "quote-pair-cap",
            OPEN_READS,
            "liquidationPrice",
            "unavailable",
        ),
        (OPEN, "empty", "quote-minimum", OPEN_READS, "fees", "unavailable"),
        (OPEN, "empty", "quote-absent-cap", OPEN_READS, "fees", "estimate"),
        (
            OPEN,
            "empty",
            "quote-fee-read-error",
            OPEN_READS,
            "fees",
            "unavailable",
        ),
        (
            OPEN,
            "empty",
            "keeper-error",
            "borrowings oneClickTrading",
            "worstCaseFill",
            "estimate",
        ),
        (
            OPEN[:-2],
            "empty",
            "keeper-error",
            "borrowings oneClickTrading",
            "worstCaseFill",
            "unavailable",
        ),
        (OPEN[:-2], "empty", "quote-null-price", OPEN_READS, "fees", "estimate"),
        (OPEN, "empty", "quote-null-price", OPEN_READS, "liquidationPrice", "estimate"),
        (CLOSE, "empty", "quote-close", "", "worstCaseFill", "unbounded"),
        (
            CLOSE,
            "session",
            "quote-close",
            CLOSE_READS + " get_fees",
            "execution",
            "voluntary_close",
        ),
        (
            CLOSE,
            "session",
            "quote-close-fee-cap",
            CLOSE_READS + " get_fees",
            "fees",
            "estimate",
        ),
        (
            CLOSE,
            "session",
            "quote-fee-read-error",
            CLOSE_READS + " get_fees",
            "fees",
            "unavailable",
        ),
        (
            CLOSE,
            "session",
            "quote-liquidation",
            FRESH_READS + " get_liquidation_price",
            "execution",
            "would_liquidate",
        ),
        (
            CLOSE,
            "session",
            "quote-height-mismatch",
            POSITION_READS,
            "execution",
            "unavailable",
        ),
        (
            CLOSE,
            "session",
            "quote-after-hours",
            CLOSE_READS + " get_market get_after_hours_leverage get_fees",
            "fees",
            "estimate",
        ),
        (
            CLOSE,
            "session",
            "quote-stale",
            STALE_READS,
            "execution",
            "would_be_rejected",
        ),
        (
            CLOSE,
            "session",
            "quote-queued",
            FRESH_READS,
            "execution",
            "unavailable",
        ),
        (
            CANCEL,
            "session",
            "pending-order",
            "oneClickTrading node_info get_trade_raw",
            "fees",
            "unavailable",
        ),
        (
            (*CLOSE, "--cancel-order"),
            "session",
            "pending-order",
            "oneClickTrading node_info get_trade_raw",
            "worstCaseFill",
            "unavailable",
        ),
    ],
    ids=[
        "market-explicit-price",
        "market-usdc-oracle-outage",
        "market-usdc-oracle-off-peg",
        "market-keeper-price",
        "limit-trigger-fee",
        "stop-short",
        "limit-close-only",
        "market-pair-cap",
        "market-minimum",
        "market-absent-custom-cap",
        "market-fee-read-error",
        "market-reference-fallback",
        "market-keeper-error",
        "market-null-price",
        "market-null-price-with-reference",
        "close-signed-out",
        "close-voluntary",
        "close-fee-cap",
        "close-fee-read-error",
        "close-liquidation",
        "close-height-mismatch",
        "close-after-hours",
        "close-stale",
        "close-queued",
        "cancel",
        "close-cancel-order",
    ],
)
def test_dry_run_estimates(
    estimate_pair: tuple[CLI, CLI],
    tmp_path: Path,
    args: CLI,
    seed: str,
    scenario: str,
    wire: str,
    status_field: str,
    status: str,
) -> None:
    if args != CLOSE or seed != "empty":
        estimate_pair = (str(Path(sys.executable).with_name("sai")),), estimate_pair[1]
    outputs = []
    for index, cli in enumerate(estimate_pair):
        cli_args = (*CLOSE, "--cancel-order") if index == 0 and args == CANCEL else args
        with loopback(scenario) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, seed)
            completed = subprocess.run(
                [*cli, "--json", *cli_args, "--dry-run"],
                cwd=home,
                env={**child_env(home, endpoints), "SAI_UPDATE_AUTO": "0"},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
                check=False,
            )
            assert (completed.returncode, completed.stderr) == (0, b"")
            preview = json.loads(completed.stdout)
            assert preview["estimates"][status_field]["status"] == status
            if scenario.startswith("quote-usdc-oracle-"):
                assert preview["estimates"]["fees"]["collateralPriceUsd"] == {
                    "price": "1",
                    "source": "Fixed USDC price",
                    "observedAt": None,
                }
            assert [request.label for request in endpoints.wire] == wire.split()
            assert not endpoints.errors
            assert not rows(home)
            outputs.append(
                json.loads(OBSERVED_AT.sub(b'"observedAt": "<time>"', completed.stdout))
            )
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    ("scenario", "message", "wire"),
    [
        (
            "quote-foreign-keeper-owner",
            "Keeper position belongs to a different account",
            "trade",
        ),
        (
            "quote-wrong-position-index",
            "Keeper returned a different position index",
            "trade",
        ),
        (
            "quote-foreign-contract-owner",
            "Contract position belongs to a different account",
            "trade oneClickTrading node_info get_trade_raw",
        ),
    ],
)
def test_close_quote_checks_the_position_owner_and_index(
    cli_pair: tuple[CLI, CLI], tmp_path: Path, scenario: str, message: str, wire: str
) -> None:
    # The workspace Python includes the merged PR 148, which checks Perp
    # ownership before querying the borrowing.
    reference = str(Path(sys.executable).with_name("sai"))
    for index, cli in enumerate(((reference,), cli_pair[1])):
        with loopback(scenario) as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            code, output, stderr = run(
                cli, home, endpoints, (*CLOSE, "--dry-run"), python=index == 0
            )
            assert (code, output, stderr) == (
                1,
                {
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": message,
                        "retryable": False,
                    }
                },
                "",
            )
            assert [request.label for request in endpoints.wire] == wire.split()
            assert not endpoints.errors
            assert not rows(home)


def test_close_quote_reports_unverified_owner_when_perp_read_fails(
    cli_pair: tuple[CLI, CLI], tmp_path: Path
) -> None:
    reference = str(Path(sys.executable).with_name("sai"))
    outputs = []
    for index, cli in enumerate(((reference,), cli_pair[1])):
        with loopback("quote-owner-read-error") as endpoints:
            home = tmp_path / str(index)
            seed_home(home, endpoints, "session")
            code, output, stderr = run(
                cli, home, endpoints, (*CLOSE, "--dry-run"), python=index == 0
            )
            assert (code, stderr) == (0, "")
            assert output["positionOwnerVerified"] is False
            assert output["estimates"]["fees"]["status"] == "unavailable"
            assert "previewDigest" not in output
            assert [request.label for request in endpoints.wire] == [
                "trade",
                "oneClickTrading",
                "node_info",
                "get_trade_raw",
            ]
            assert not endpoints.errors
            assert not rows(home)
            outputs.append(output)
    assert outputs[0] == outputs[1]
