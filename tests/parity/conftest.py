"""Entry points used by subprocess parity tests."""

from __future__ import annotations

import os
import shutil
import sys
import tomllib
from pathlib import Path

import pytest
from release_mismatch import ExpectedReleaseMismatch, ReleaseMismatch

RELEASE_SIGNATURE = pytest.StashKey[str]()
PENDING_RELEASE_RESULTS = pytest.StashKey[set[str]]()


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
        registry = tomllib.load(file)
    reasons: dict[str, tuple[str, str]] = {}
    for table, endpoint in (("pull_request", "pull"), ("issue", "issues")):
        for number, cases in registry.get(table, {}).items():
            url = f"https://github.com/SaiDotFun/open-sai/{endpoint}/{number}"
            for case, signature in cases.items():
                nodeid = f"tests/parity/{case}"
                if nodeid in reasons:
                    raise pytest.UsageError(f"Multiple release owners for {nodeid}")
                reasons[nodeid] = (url, signature)
    config.stash[PENDING_RELEASE_RESULTS] = set(reasons)
    for item in items:
        if entry := reasons.pop(item.nodeid, None):
            url, signature = entry
            item.stash[RELEASE_SIGNATURE] = signature
            item.add_marker(
                pytest.mark.xfail(
                    reason=url,
                    strict=True,
                    raises=ExpectedReleaseMismatch,
                )
            )
    if reasons:
        raise pytest.UsageError(
            "Stale release expected-failure IDs: " + ", ".join(sorted(reasons))
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item):
    try:
        return (yield)
    except ReleaseMismatch as error:
        if error.signature == item.stash.get(RELEASE_SIGNATURE, None):
            item.config.stash[PENDING_RELEASE_RESULTS].remove(item.nodeid)
            raise ExpectedReleaseMismatch(error.differences) from None
        raise


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not (pending := session.config.stash.get(PENDING_RELEASE_RESULTS, None)):
        return
    if terminal := session.config.pluginmanager.get_plugin("terminalreporter"):
        terminal.write_line(
            "Required release comparisons did not report their expected mismatch:"
        )
        for nodeid in sorted(pending):
            terminal.write_line(f"  {nodeid}")
    if exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


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
