"""Guard against selecting the Python reference as the Rust candidate."""

import shutil
import sys
from pathlib import Path

import pytest


def test_native_ci_requires_the_wait_reference(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("SAI_PARITY_BIN", "/fixture/target/debug/sai")
    monkeypatch.delenv("SAI_PARITY_WAIT_PYTHON", raising=False)
    try:
        with pytest.raises(
            pytest.fail.Exception, match="requires SAI_PARITY_WAIT_PYTHON"
        ):
            request.getfixturevalue("wait_cli_pair")
    except pytest.skip.Exception:
        pytest.fail("native CI skipped wait parity when its reference was missing")


def test_python_launcher_is_rejected(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    launcher = shutil.which("sai", path=str(Path(sys.executable).parent))
    assert launcher is not None
    monkeypatch.setenv("SAI_PARITY_BIN", launcher)
    with pytest.raises(pytest.fail.Exception, match="compiled Rust sai binary"):
        request.getfixturevalue("cli_pair")
