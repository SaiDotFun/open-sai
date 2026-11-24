"""Guard against selecting the Python reference as the Rust candidate."""

import shutil
import sys
from pathlib import Path

import pytest


def test_python_launcher_is_rejected(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    launcher = shutil.which("sai", path=str(Path(sys.executable).parent))
    assert launcher is not None
    monkeypatch.setenv("SAI_PARITY_BIN", launcher)
    with pytest.raises(pytest.fail.Exception, match="compiled Rust sai binary"):
        request.getfixturevalue("cli_pair")
