"""Assemble native Sai archives and a checksum manifest for CI artifacts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shim.src.sai_cli.sai_cli import (
    TARGET_CPU,
    TARGET_OS,
    archive_name,
    native_target,
    release_version,
)


def versions() -> tuple[str, str]:
    cargo = tomllib.loads((ROOT / "Cargo.toml").read_text())["workspace"]["package"]
    wheel = tomllib.loads((ROOT / "shim" / "pyproject.toml").read_text())["project"]
    if cargo["version"] != release_version(wheel["version"]):
        raise SystemExit("Cargo workspace and shim versions must name the same release")
    return cargo["version"], wheel["version"]


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


def write_manifest(directory: Path, release: str, wheel_version: str) -> None:
    targets = product(set(TARGET_CPU.values()), TARGET_OS.values())
    names = {archive_name(release, f"{cpu}-{os}") for cpu, os in targets}
    names.update(
        {f"sai_cli-{wheel_version}-py3-none-any.whl", f"sai_cli-{wheel_version}.tar.gz"}
    )
    lines = []
    for name in sorted(names):
        with (directory / name).open("rb") as file:
            lines.append(f"{hashlib.file_digest(file, 'sha256').hexdigest()}  {name}\n")
    (directory / "SHA256SUMS").write_text("".join(lines), encoding="ascii")


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
    args = parser.parse_args()
    release, wheel = versions()
    if args.command == "archive":
        if args.target != native_target():
            parser.error("the archive must be built on a runner for its target")
        args.directory.mkdir(parents=True, exist_ok=True)
        make_archive(args.binary, args.directory / archive_name(release, args.target))
    elif args.command == "manifest":
        write_manifest(args.directory, release, wheel)
    else:
        build_rehearsal_wheel(args.directory)


if __name__ == "__main__":
    main()
