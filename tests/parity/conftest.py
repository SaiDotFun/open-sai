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
    reference = os.environ.get("SAI_PARITY_PYTHON")
    python_sai = (
        shutil.which(reference)
        if reference is not None
        else shutil.which("sai", path=str(Path(sys.executable).parent))
    )
    if python_sai is None:
        pytest.fail(
            "Install the Python reference with uv sync and set SAI_PARITY_PYTHON if needed"
        )
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


@pytest.fixture
def wait_cli_pair(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    reference = os.environ.get("SAI_PARITY_WAIT_PYTHON")
    if reference is None:
        if (
            os.environ.get("GITHUB_ACTIONS") == "true"
            and "SAI_PARITY_BIN" in os.environ
        ):
            pytest.fail("native parity CI requires SAI_PARITY_WAIT_PYTHON")
        pytest.skip("set SAI_PARITY_WAIT_PYTHON to the pinned Python sai launcher")
    monkeypatch.setenv("SAI_PARITY_PYTHON", reference)
    return request.getfixturevalue("cli_pair")
