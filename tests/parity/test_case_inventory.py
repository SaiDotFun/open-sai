import os
import tomllib
from pathlib import Path

import pytest
from case_inventory import require_manifest_case_ids
from scenarios import loopback, seed_home


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


@pytest.mark.parametrize("name", ["auth-use", "auth-logout"])
@pytest.mark.parametrize("umask", [0o002, 0o077], ids=["group-write", "private"])
def test_auth_mutation_seed_matches_manifest_lock_mode(
    tmp_path: Path, name: str, umask: int
) -> None:
    manifest = Path(__file__).with_name("cases.toml")
    case = next(
        case
        for case in tomllib.loads(manifest.read_text())["case"]
        if case["name"] == name
    )
    home = tmp_path / "home"
    with loopback("normal") as endpoints:
        previous = os.umask(umask)
        try:
            seed_home(home, endpoints, case["seed"])
        finally:
            os.umask(previous)
    lock = home / "sai" / "trader" / "sessions.lock"
    assert lock.stat().st_mode & 0o777 == case["state"]["trader/sessions.lock#@mode"]
