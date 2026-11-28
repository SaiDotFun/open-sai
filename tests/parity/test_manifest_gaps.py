import os
import subprocess
import sys
from pathlib import Path

import pytest
from manifest_gaps import ManifestGaps
from release_mismatch import difference_signature


def registry(tmp_path: Path, difference: str) -> ManifestGaps:
    path = tmp_path / "manifest-xfails.toml"
    path.write_text(
        '[gap."case:log-fields"]\n'
        'owner = "#126"\n'
        f'signature = "{difference_signature([difference])}"\n',
        encoding="utf-8",
    )
    return ManifestGaps(path)


def test_only_the_recorded_difference_passes(tmp_path: Path) -> None:
    difference = "parity.files.logs/cli.jsonl[0].data.commandId: missing"
    gaps = registry(tmp_path, difference)
    assert gaps.result("case:log-fields", [difference])[0] == "XFAIL"
    assert gaps.finish() == 0

    gaps = registry(tmp_path, difference)
    assert (
        gaps.result("case:log-fields", [difference, "parity.output.outcome"])[0]
        == "DIFF"
    )
    assert gaps.finish() == 1

    gaps = registry(tmp_path, difference)
    assert gaps.result("case:log-fields", [])[0] == "XPASS"
    assert gaps.finish() == 1

    gaps = registry(tmp_path, difference)
    assert gaps.result("mcp:market_list", [])[0] == "PASS"
    assert gaps.finish() == 1


def test_configured_exception_cannot_mask_a_network_violation(tmp_path: Path) -> None:
    difference = "candidate attempted a non-loopback network connection"
    gaps = registry(tmp_path, difference)
    assert gaps.result("case:log-fields", [difference])[0] == "DIFF"
    assert gaps.finish() == 1


CURRENT_TRADE_UX_CASES = (
    "account-balances",
    "account-balances-derived-endpoint",
    "account-balances-no-subprotocol",
    "account-balances-redirect",
    "account-balances-text",
    "bare-help-trade-cancel",
    "market-list-text",
    "trade-cancel-alias",
    "trade-open-ambiguous-market-symbol",
    "trade-open-unknown-market-symbol",
)


CURRENT_GAPS = ManifestGaps(Path(__file__).with_name("python-main-gaps.toml"))


@pytest.mark.parametrize(
    ("case_name", "status"),
    [(case, "PASS") for case in CURRENT_TRADE_UX_CASES]
    + [(case.removeprefix("case:"), "XFAIL") for case in CURRENT_GAPS.gaps],
)
def test_current_trade_ux_manifest_matches_native(
    cli_pair: tuple[tuple[str, ...], tuple[str, ...]],
    case_name: str,
    status: str,
) -> None:
    directory = Path(__file__).parent
    result = subprocess.run(
        [
            sys.executable,
            str(directory / "parity.py"),
            "--reference",
            str(Path(sys.executable).with_name("sai")),
            "--case",
            case_name,
        ],
        cwd=directory.parents[1],
        env={**os.environ, "SAI_PARITY_BIN": cli_pair[1][0]},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"({case_name}) | {status} |" in result.stdout
