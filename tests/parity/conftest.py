"""Entry points used by subprocess parity tests."""

from __future__ import annotations

import os
import shutil
import sys
import tomllib
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--release-xfails",
        action="store_true",
        help="Apply the strict expected failures for the v0.1.2 Rust CI comparison",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if not config.getoption("--release-xfails"):
        return
    path = Path(__file__).with_name("release-xfails.toml")
    with path.open("rb") as file:
        owners = tomllib.load(file)["pull_request"]
    reasons = {
        f"tests/parity/{case}": f"https://github.com/SaiDotFun/open-sai/pull/{pr}"
        for pr, cases in owners.items()
        for case in cases
    }
    for item in items:
        if reason := reasons.pop(item.nodeid, None):
            item.add_marker(
                pytest.mark.xfail(reason=reason, strict=True, raises=AssertionError)
            )
    if reasons:
        raise pytest.UsageError(
            "Stale release expected-failure IDs: " + ", ".join(sorted(reasons))
        )


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
