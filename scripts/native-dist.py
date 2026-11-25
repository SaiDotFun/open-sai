"""Package, stage, and verify native Sai release artifacts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from itertools import product
from pathlib import Path
from typing import Any

from check_release import check_tag, native_versions, pyproject_version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shim.src.sai_cli.sai_cli import (
    TARGET_CPU,
    TARGET_OS,
    archive_name,
    native_target,
)


def make_archive(binary: Path, destination: Path) -> None:
    with (
        destination.open("wb") as output,
        gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed,
        tarfile.open(
            fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT
        ) as archive,
        binary.open("rb") as executable,
    ):
        member = tarfile.TarInfo("sai")
        member.mode = 0o755
        member.size = binary.stat().st_size
        archive.addfile(member, executable)


def archive_names(release: str) -> set[str]:
    targets = product(set(TARGET_CPU.values()), TARGET_OS.values())
    return {archive_name(release, f"{cpu}-{os}") for cpu, os in targets}


def package_names(version: str) -> set[str]:
    return {f"sai_cli-{version}-py3-none-any.whl", f"sai_cli-{version}.tar.gz"}


def digests(directory: Path, names: set[str]) -> dict[str, str]:
    hashes = {}
    for name in names:
        with (directory / name).open("rb") as file:
            hashes[name] = hashlib.file_digest(file, "sha256").hexdigest()
    return hashes


def manifest_text(hashes: dict[str, str]) -> str:
    return "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))


def check_files(directory: Path, expected: set[str], label: str) -> None:
    found = {path.name for path in directory.iterdir()}
    if found != expected:
        raise ValueError(
            f"{label} has the wrong files: missing {sorted(expected - found)}, "
            f"unexpected {sorted(found - expected)}"
        )


def stage_release(
    native: Path,
    python: Path,
    output: Path,
    release: str,
    shim: str,
    python_version: str,
) -> None:
    archives = archive_names(release)
    native_names = archives | package_names(shim)
    check_files(native, native_names | {"SHA256SUMS"}, "native bundle")
    native_hashes = digests(native, native_names)
    if (native / "SHA256SUMS").read_bytes() != manifest_text(native_hashes).encode(
        "ascii"
    ):
        raise ValueError("native bundle does not match its SHA256SUMS")

    # Wheel and sdist builders normalize the PEP 440 spelling of release candidates.
    python_names = package_names(python_version.replace("-rc.", "rc"))
    check_files(python, python_names, "full Python distribution")
    hashes = digests(python, python_names)
    hashes.update({name: native_hashes[name] for name in archives})
    output.mkdir(parents=True)
    for source, names in ((native, archives), (python, python_names)):
        for name in names:
            shutil.copyfile(source / name, output / name)
    (output / "SHA256SUMS").write_text(manifest_text(hashes), encoding="ascii")


def check_attestation(
    directory: Path, tag_object: str, verified: dict[str, Any]
) -> None:
    subjects = verified["verificationResult"]["statement"]["subject"]
    tags = [subject["digest"]["sha1"] for subject in subjects if "name" not in subject]
    if tags != [tag_object]:
        raise ValueError("release attestation does not match the built tag object")
    assets = [subject for subject in subjects if "name" in subject]
    hashes = {subject["name"]: subject["digest"]["sha256"] for subject in assets}
    names = {path.name for path in directory.iterdir() if path.is_file()}
    if len(hashes) != len(assets) or hashes != digests(directory, names):
        raise ValueError("release attestation does not match the staged release assets")
    print(f"Verified the immutable tag and all {len(hashes)} release assets")


def build_rehearsal_wheel(output: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="sai-rehearsal-wheel-") as scratch:
        source = Path(scratch) / "shim"
        shutil.copytree(
            ROOT / "shim",
            source,
            ignore=shutil.ignore_patterns("build", "dist", "*.egg-info", "__pycache__"),
        )
        shutil.copyfile(
            ROOT / "tests/parity/native_release_source.py",
            source / "src/sai_cli/_release_source.py",
        )
        subprocess.run(
            ["uv", "build", "--wheel", source, "--out-dir", output.resolve()],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser("archive")
    archive.add_argument("binary", type=Path)
    archive.add_argument("directory", type=Path)
    archive.add_argument("--target", required=True)
    manifest = commands.add_parser("manifest")
    manifest.add_argument("directory", type=Path)
    rehearsal = commands.add_parser("rehearsal-wheel")
    rehearsal.add_argument("directory", type=Path)
    tag_check = commands.add_parser("check-tag")
    tag_check.add_argument("tag")
    stage = commands.add_parser("stage-release")
    stage.add_argument("native", type=Path)
    stage.add_argument("python", type=Path)
    stage.add_argument("output", type=Path)
    stage.add_argument("--tag", help="require matching versions for publication")
    attestation = commands.add_parser("check-attestation")
    attestation.add_argument("directory", type=Path)
    attestation.add_argument("tag_object")
    args = parser.parse_args()

    try:
        run(args)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc


def run(args: argparse.Namespace) -> None:
    if args.command == "check-attestation":
        check_attestation(args.directory, args.tag_object, json.load(sys.stdin))
        return
    release, shim = native_versions()
    if args.command == "archive":
        if args.target != native_target():
            raise ValueError("the archive must be built on a runner for its target")
        args.directory.mkdir(parents=True, exist_ok=True)
        make_archive(args.binary, args.directory / archive_name(release, args.target))
    elif args.command == "manifest":
        names = archive_names(release) | package_names(shim)
        sums = args.directory / "SHA256SUMS"
        check_files(
            args.directory,
            names | ({sums.name} if sums.exists() else set()),
            "native bundle",
        )
        manifest = manifest_text(digests(args.directory, names))
        sums.write_text(manifest, encoding="ascii")
    elif args.command == "rehearsal-wheel":
        build_rehearsal_wheel(args.directory)
    elif args.command == "check-tag":
        check_tag(args.tag, release, pyproject_version())
    elif args.command == "stage-release":
        python_version = pyproject_version()
        if args.tag:
            check_tag(args.tag, release, python_version)
        stage_release(
            args.native, args.python, args.output, release, shim, python_version
        )


if __name__ == "__main__":
    main()
