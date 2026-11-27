"""Exercise the release file and attestation checks with isolated artifacts."""

from __future__ import annotations

import hashlib
import json
import runpy
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from check_release import pyproject_version

TARGETS = (
    "aarch64-apple-darwin",
    "aarch64-unknown-linux-gnu",
    "x86_64-apple-darwin",
    "x86_64-unknown-linux-gnu",
)


def native_dist(
    *args: str | Path, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, ROOT / "scripts/native-dist.py", *args],
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )


def packages(version: str) -> set[str]:
    return {f"sai_cli-{version}-py3-none-any.whl", f"sai_cli-{version}.tar.gz"}


def manifest(directory: Path, names: set[str]) -> str:
    return "".join(
        f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n"
        for name in sorted(names)
    )


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Path, Path, Path, set[str]]:
    cargo = tomllib.loads((ROOT / "Cargo.toml").read_text())["workspace"]["package"]
    shim = tomllib.loads((ROOT / "shim/pyproject.toml").read_text())["project"]
    archives = {f"sai-{cargo['version']}-{target}.tar.gz" for target in TARGETS}
    native = tmp_path / "native CI"
    native.mkdir()
    for name in archives | packages(shim["version"]):
        (native / name).write_text(f"native CI asset {name}")
    (native / "SHA256SUMS").write_text(
        manifest(native, archives | packages(shim["version"]))
    )

    python = tmp_path / "full Python"
    python.mkdir()
    python_names = packages(pyproject_version().replace("-rc.", "rc"))
    for name in python_names:
        (python / name).write_text(f"full Python asset {name}")

    bundle = f"sai-cli-{cargo['version']}-full-python.zip"
    return (
        native,
        python,
        tmp_path / "release",
        archives | packages(shim["version"]) | {bundle},
    )


def assert_staged_candidate(
    native: Path, python: Path, output: Path, names: set[str]
) -> None:
    assert {path.name for path in output.iterdir()} == names | {"SHA256SUMS"}
    (bundle,) = output.glob("sai-cli-*-full-python.zip")
    with zipfile.ZipFile(bundle) as archive:
        assert archive.namelist() == sorted(path.name for path in python.iterdir())
        for path in python.iterdir():
            assert archive.read(path.name) == path.read_bytes()
    for name in names - {bundle.name}:
        assert (output / name).read_bytes() == (native / name).read_bytes()
    assert (output / "SHA256SUMS").read_text() == manifest(output, names)


def test_stages_native_archives_shim_and_full_python_bundle(candidate) -> None:
    native, python, output, names = candidate
    result = native_dist("stage-release", native, python, output)

    assert result.returncode == 0, result.stderr
    assert_staged_candidate(native, python, output, names)


def test_renders_native_release_notes_for_the_staged_packages(candidate) -> None:
    native, python, output, _ = candidate
    result = native_dist("stage-release", native, python, output)
    assert result.returncode == 0, result.stderr
    result = native_dist("release-notes")
    assert result.returncode == 0, result.stderr

    (bundle,) = output.glob("*.zip")
    (shim,) = output.glob("*.whl")
    (full_wheel,) = python.glob("*.whl")
    notes = result.stdout
    assert f"The standard `sai-cli` wheel, `{shim.name}`, is the native shim." in notes
    assert f"`{bundle.name}` and `SHA256SUMS`" in notes
    assert f"sha256sum {bundle.name}" in notes
    assert f"Get-FileHash -Algorithm SHA256 {bundle.name}" in notes
    assert f"python -m zipfile -e {bundle.name} full-python" in notes
    assert f"uv tool install ./full-python/{full_wheel.name}" in notes


def test_stages_python_only_without_cargo_or_shim(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("native-dist.py", "check_release.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.4-rc.1"\n')
    python = tmp_path / "python"
    python.mkdir()
    names = packages("1.2.4rc1")
    for name in names:
        (python / name).write_text(f"full Python {name}")

    output = tmp_path / "release"
    result = subprocess.run(
        [
            sys.executable,
            scripts / "native-dist.py",
            "stage-python-release",
            python,
            output,
            "--tag",
            "sai-cli/v1.2.4-rc.1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert {path.name for path in output.iterdir()} == names | {"SHA256SUMS"}
    assert (output / "SHA256SUMS").read_text() == manifest(output, names)


def test_keeps_both_distributions_when_package_names_collide(candidate) -> None:
    native, python, output, names = candidate
    dist = runpy.run_path(str(ROOT / "scripts/native-dist.py"))
    release, shim = dist["native_versions"]()
    for path in python.iterdir():
        path.unlink()
    for name in packages(shim):
        (python / name).write_bytes(b"full Python")

    dist["check_tag"](f"sai-cli/v{release}", release, release)
    with pytest.raises(ValueError, match="does not match the Cargo and shim version"):
        dist["check_tag"]("sai-cli/v0.0.0", release, "0.0.0")
    dist["stage_release"](native, python, output, release, shim, release)

    assert_staged_candidate(native, python, output, names)


@pytest.mark.parametrize("fault", ["missing", "changed", "extra", "missing-checksum"])
def test_refuses_incomplete_or_changed_native_artifacts(candidate, fault: str) -> None:
    native, python, output, _ = candidate
    archive = next(native.glob("sai-*.tar.gz"))
    if fault == "missing":
        archive.unlink()
    elif fault == "changed":
        archive.write_text("different archive bytes")
    elif fault == "extra":
        (native / "unexpected.tar.gz").touch()
    else:
        (native / "SHA256SUMS").write_text("")
    result = native_dist("stage-release", native, python, output)

    assert result.returncode != 0
    assert "native bundle" in result.stderr
    assert not output.exists()


def test_refuses_a_release_tag_that_differs_from_the_candidate(candidate) -> None:
    native, python, output, _ = candidate
    result = native_dist(
        "stage-release", native, python, output, "--tag", "sai-cli/v0.0.0"
    )

    assert result.returncode != 0
    assert "does not match" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("fault", [None, "missing", "changed", "duplicate", "tag"])
def test_compares_the_verified_release_attestation(
    candidate, fault: str | None
) -> None:
    native, python, output, names = candidate
    result = native_dist("stage-release", native, python, output)
    assert result.returncode == 0, result.stderr

    tag_object = "a" * 40
    subjects = [
        {"uri": "pkg:github/SaiDotFun/open-sai", "digest": {"sha1": tag_object}}
    ]
    subjects += [
        {
            "name": name,
            "digest": {
                "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest()
            },
        }
        for name in sorted(names | {"SHA256SUMS"})
    ]
    if fault == "missing":
        subjects.pop()
    elif fault == "changed":
        subjects[-1]["digest"]["sha256"] = "0" * 64
    elif fault == "duplicate":
        subjects.append(subjects[-1])
    elif fault == "tag":
        subjects[0]["digest"]["sha1"] = "b" * 40

    verified = json.dumps({"verificationResult": {"statement": {"subject": subjects}}})
    result = native_dist("check-attestation", output, tag_object, stdin=verified)

    assert (result.returncode == 0) is (fault is None), result.stderr
