"""Compare C7d dry runs with Python v0.1.2 at 8557a7e13dceab0accc369191182c8e01423c91b."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from scenarios import child_env, loopback, seed_home
from test_trade import CANCEL, CLI, CLOSE, OPEN, rows, run

OBSERVED_AT = re.compile(rb'"observedAt": "\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z"')
OPEN_READS = (
    "borrowings oneClickTrading node_info get_market get_group "
    "get_pair_custom_max_leverage get_fees get_collateral_price"
)
POSITION_READS = (
    "oneClickTrading node_info trade borrowing get_trade_raw get_exchange_balance "
    "get_trade_raw get_perp_prices"
)
CLOSE_READS = POSITION_READS + " get_liquidation_price is_after_hours_market"
STALE_READS = POSITION_READS + " get_market get_group block market_order_config"


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
        (OPEN[:-2], "empty", "quote-market", OPEN_READS, "worstCaseFill", "estimate"),
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
            OPEN_READS.removesuffix(" get_collateral_price"),
            "fees",
            "unavailable",
        ),
        (OPEN, "empty", "keeper-error", "borrowings", "worstCaseFill", "estimate"),
        (
            OPEN[:-2],
            "empty",
            "keeper-error",
            "borrowings",
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
            CLOSE_READS + " get_market get_fees",
            "execution",
            "voluntary_close",
        ),
        (
            CLOSE,
            "session",
            "quote-close-fee-cap",
            CLOSE_READS + " get_market get_fees",
            "fees",
            "estimate",
        ),
        (
            CLOSE,
            "session",
            "quote-fee-read-error",
            CLOSE_READS + " get_market get_fees",
            "fees",
            "unavailable",
        ),
        (
            CLOSE,
            "session",
            "quote-liquidation",
            POSITION_READS + " get_liquidation_price",
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
            CLOSE_READS + " get_market get_after_hours_leverage get_market get_fees",
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
        (CLOSE, "session", "quote-queued", STALE_READS, "execution", "unavailable"),
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
            expected_wire = wire.split()
            if index == 1 and wire.startswith(POSITION_READS):
                # PR 148 validates both owners before reading market inputs.
                expected_wire = [
                    "trade",
                    *expected_wire[:2],
                    "get_trade_raw",
                    "borrowing",
                    *expected_wire[5:],
                ]
            assert [request.label for request in endpoints.wire] == expected_wire
            assert not endpoints.errors
            assert not rows(home)
            outputs.append(OBSERVED_AT.sub(b'"observedAt": "<time>"', completed.stdout))
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
