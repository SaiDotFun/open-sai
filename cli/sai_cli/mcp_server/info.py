"""What runs, from where: the version and provenance part of `mcp_info`.

Agents call `mcp_info` instead of reading MCP client configs, which can hold
secrets. The report carries names, paths, and a commit, never secret values.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any

from ..core import update

PACKAGE = "sai-cli"
# cli/sai_cli/mcp_server/info.py -> repository root
SOURCE_ROOT = Path(__file__).resolve().parents[3]


def version(name: str = PACKAGE) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


# The version this process loaded. A reinstall changes the installed version on
# disk, but a running server keeps executing the code it imported.
RUNNING_VERSION = version()


def versions() -> dict[str, Any]:
    """Report the running, installed, and latest known versions.

    The installed version is read fresh from package metadata. The latest
    version comes from the cache that `sai update --check` writes; this makes
    no network request.
    """
    installed = version()
    known = "unknown" not in (installed, RUNNING_VERSION)
    return {
        "version": RUNNING_VERSION,
        "installedVersion": installed,
        "restartNeeded": known and installed != RUNNING_VERSION,
        "latestVersion": update.latest_version(),
    }


def is_checkout(root: Path) -> bool:
    """True when ROOT's pyproject.toml declares this package."""
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return data.get("project", {}).get("name") == PACKAGE


def git_state(root: Path) -> dict[str, Any] | None:
    """Return the checkout's commit and dirty flag, or None."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout.strip()

    try:
        commit = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--", "."))
    except (OSError, subprocess.SubprocessError):
        return None
    return {"commit": commit, "dirty": dirty}


def provenance(root: Path = SOURCE_ROOT) -> dict[str, Any]:
    """Report a source checkout with its git state, or an installed package."""
    if is_checkout(root):
        return {"kind": "source-checkout", "path": str(root), "git": git_state(root)}
    return {"kind": "installed", "path": str(Path(sys.prefix).resolve())}
