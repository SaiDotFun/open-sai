import pytest
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
