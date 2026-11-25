import tomllib
from pathlib import Path

import pytest
from case_inventory import require_manifest_case_ids


def test_required_passing_case_cannot_disappear() -> None:
    directory = Path(__file__).parent
    cases = tomllib.loads((directory / "cases.toml").read_text())["case"]
    inventory = directory / "release-case-ids.txt"
    names = [case["name"] for case in cases]
    require_manifest_case_ids(names, inventory)
    names.remove("config-invalid-endpoint")
    with pytest.raises(
        ValueError, match="Missing required release case IDs: config-invalid-endpoint"
    ):
        require_manifest_case_ids(names, inventory)
