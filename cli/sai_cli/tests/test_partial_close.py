"""Size partial closes from contract state."""

from __future__ import annotations

import json

from click.testing import CliRunner
from sai_cli.core import storage
from sai_cli.sai_cli import main
from sai_cli.tests import test_mcp_position_digest as fixture

position_service = fixture.position_service


def test_partial_close_refuses_index_lag(
    position_service: tuple[fixture.Position, str],
) -> None:
    position, base_url = position_service
    # The contract shows $1.50 while the index still shows $2.
    position.collateral = 1_500_000
    position.indexed_collaterals.extend([2_000_000] * 3)
    fixture.save_session(f"{base_url}/query")
    args = fixture.position_cli_args(
        base_url, ["partial-close", "7", "--percent", "50"]
    )

    result = CliRunner().invoke(main, args)

    assert result.exit_code != 0
    assert "Retry after Keeper synchronizes" in result.output
    assert position.submitted == []
    assert not (storage.home() / "intents.sqlite3").exists()


def test_partial_close_sizes_from_contract_state(
    position_service: tuple[fixture.Position, str],
) -> None:
    position, base_url = position_service
    position.collateral = 3_000_000
    position.preview_inputs = True
    fixture.save_session(f"{base_url}/query")
    args = fixture.position_cli_args(
        base_url, ["partial-close", "7", "--percent", "50", "--dry-run"]
    )

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["intent"]["body"]["collateral"]["amountUsdc"] == "1500000"
    assert payload["previewDigest"]
    assert position.market_price_query_heights == ["120"]
    assert position.price_query_heights == []
    assert position.submitted == []
    assert not (storage.home() / "intents.sqlite3").exists()
