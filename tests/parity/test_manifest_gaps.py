from pathlib import Path

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
