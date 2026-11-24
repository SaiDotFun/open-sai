"""Names retired when the MCP server moved under `sai mcp serve`."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
# Built from parts so this file does not match its own search.
RETIRED = ("sai" + "-mcp", "SAI_MCP_" + "EXPOSED_TOOLS")


def tracked_files() -> list[Path]:
    """Return files git tracks or would track, so build output is skipped."""
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.splitlines()
    return [ROOT / name for name in listed]


def test_retired_mcp_names_do_not_reappear():
    here = Path(__file__).resolve()
    found = []
    for path in tracked_files():
        if path.resolve() == here or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(ROOT).as_posix()
        # Each adapter recognizes the retired name in existing registrations.
        if relative == "cli/sai_cli/mcp_install.py":
            text = text.replace(f'RETIRED_NAME = "{RETIRED[0]}"', "", 1)
        elif relative == "sai/src/mcp_registration.rs":
            text = text.replace(f'const RETIRED_NAME: &str = "{RETIRED[0]}";', "", 1)
        for name in RETIRED:
            if name in text:
                found.append(f"{relative}: {name}")
    assert found == []
