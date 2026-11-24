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
    rust_sai = Path(executable).resolve()
    with rust_sai.open("rb") as binary:
        magic = binary.read(4)
    # Accept ELF and Mach-O executables, including macOS universal binaries.
    native_formats = {
        b"\x7fELF",
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
        b"\xca\xfe\xba\xbf",
    }
    if magic not in native_formats or rust_sai.samefile(sys.executable):
        pytest.fail("SAI_PARITY_BIN must point to the compiled Rust sai binary")
    return (python_sai,), (str(rust_sai),)
