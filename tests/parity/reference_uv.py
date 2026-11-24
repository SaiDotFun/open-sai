"""Resolve uv before replacing HOME with the parity fixture."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from network import restricted


def resolve_uv() -> str:
    path_uv = shutil.which("uv")
    candidates = [os.getenv("UV"), path_uv]
    # mise's uv shim resolves to mise itself. Ask mise for its installed
    # executable while its original HOME and configuration are accessible.
    if path_uv and Path(path_uv).resolve().name == "mise":
        try:
            installed = subprocess.run(
                restricted([str(Path(path_uv).resolve()), "which", "uv"]),
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if installed.returncode == 0:
                candidates.append(installed.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass

    with tempfile.TemporaryDirectory(prefix="sai-parity-uv-") as home:
        for candidate in dict.fromkeys(candidates):
            if not candidate or not Path(candidate).is_absolute():
                continue
            try:
                executable = Path(candidate).resolve()
                with executable.open("rb") as file:
                    if file.read(4) != b"\x7fELF":
                        continue
                result = subprocess.run(
                    restricted([str(executable), "--version"]),
                    env={"HOME": home, "PATH": os.defpath, "UV_NO_CONFIG": "1"},
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                if result.returncode == 0 and re.match(
                    r"uv \d+\.\d+\.\d+", result.stdout
                ):
                    return str(executable)
            except (OSError, subprocess.TimeoutExpired, UnicodeError):
                continue
    raise ValueError(
        "Could not resolve a uv executable that runs with an isolated HOME. "
        "Run the driver with uv run or install uv directly on PATH."
    )
