"""Entry points used by subprocess parity tests."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest


@pytest.fixture
def cli_pair() -> tuple[tuple[str, ...], tuple[str, ...]]:
    candidate = os.environ.get("SAI_PARITY_BIN")
    if candidate is None:
        pytest.skip("set SAI_PARITY_BIN to the Rust sai executable")
    executable = shutil.which(candidate)
    if executable is None:
        pytest.fail("SAI_PARITY_BIN must name an executable")
    python_sai = shutil.which("sai", path=str(Path(sys.executable).parent))
    if python_sai is None:
        pytest.fail("Install the Python project in the test environment with uv sync")
    return (python_sai,), (str(Path(executable).absolute()),)
