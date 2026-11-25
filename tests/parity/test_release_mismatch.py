from pathlib import Path

import pytest
from compare import Normalize
from release_mismatch import difference_signature, differing_paths


@pytest.mark.parametrize(
    ("reference", "known", "mutant"),
    [
        (
            {"lcd": "release-fixture"},
            {"lcd": "known-fixture"},
            {"lcd": "mutant-fixture"},
        ),
        (b"release-fixture", b"known-fixture", b"mutant-fixture"),
        ({}, {"extra": "known-fixture"}, {"extra": "mutant-fixture"}),
        ([], ["known-fixture"], ["mutant-fixture"]),
        (None, {"extra": "known-fixture"}, {"extra": "mutant-fixture"}),
    ],
)
def test_signature_changes_when_a_value_changes_at_the_same_path(
    reference: object, known: object, mutant: object
) -> None:
    approved = differing_paths(reference, known, "parity.stdout")
    changed = differing_paths(reference, mutant, "parity.stdout")
    assert difference_signature(approved) != difference_signature(changed)
    assert "fixture" not in " ".join(changed)


def test_fixture_port_changes_preserve_only_the_known_fingerprint(
    tmp_path: Path,
) -> None:
    def signature(port: int, suffix: str) -> str:
        authority = f"127.0.0.1:{port}"
        normalize = Normalize(tmp_path, fixture_values={authority: "<fixture-http>"})
        origin = f"http://{authority}"
        return difference_signature(
            differing_paths(
                normalize.text(origin + "/query"),
                normalize.text(origin + suffix),
                "parity",
            )
        )

    assert signature(40001, "") == signature(40002, "")
    assert signature(40001, "") != signature(40001, "/wrong")
    assert signature(40001, "") != signature(40001, "0")
