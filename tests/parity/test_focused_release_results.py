import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from release_mismatch import difference_signature, differing_paths


@pytest.mark.parametrize(
    ("test_body", "selection", "status"),
    [
        ('pytest.skip("test probe")', [], pytest.ExitCode.TESTS_FAILED),
        ("pass", ["-k", "other"], pytest.ExitCode.TESTS_FAILED),
        ("assert_release_equal(1, 2)", [], pytest.ExitCode.OK),
    ],
)
def test_registered_comparison_must_report_its_result(
    tmp_path: Path, test_body: str, selection: list[str], status: pytest.ExitCode
) -> None:
    target = tmp_path / "tests" / "parity"
    target.mkdir(parents=True)
    for name in ("conftest.py", "release_mismatch.py"):
        shutil.copyfile(Path(__file__).with_name(name), target / name)
    signature = difference_signature(differing_paths(1, 2, "parity"))
    (target / "release-xfails.toml").write_text(
        f'[pull_request."126"]\n"test_probe.py::test_required" = "{signature}"\n'
    )
    (target / "test_probe.py").write_text(
        "import pytest\nfrom release_mismatch import assert_release_equal\n"
        f"def test_required():\n    {test_body}\n"
        "def test_other():\n    pass\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/parity",
            "-q",
            "--release-xfails",
            *selection,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == status, result.stdout + result.stderr
