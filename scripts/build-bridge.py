"""Rehearse a full Python bridge wheel at a proposed release version."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from check_release import ROOT, pyproject_version

VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    match = VERSION.fullmatch(args.version)
    current_version = pyproject_version()
    current = VERSION.fullmatch(current_version)
    if current is None:
        parser.error("the checkout must name a stable Python version in pyproject.toml")
    if match is None or tuple(map(int, match.groups())) <= tuple(
        map(int, current.groups())
    ):
        parser.error(
            f"the bridge requires a distinct stable version newer than {current_version}"
        )
    with tempfile.TemporaryDirectory(prefix="sai-bridge-source-") as scratch:
        source = Path(scratch)
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        project, count = re.subn(
            r'^version = "[^"\r\n]+"$',
            f'version = "{args.version}"',
            project,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise SystemExit("could not set the bridge package version")
        (source / "pyproject.toml").write_text(project, encoding="utf-8")
        for name in ("setup.py", "README.md", "LICENSE"):
            shutil.copyfile(ROOT / name, source / name)
        shutil.copytree(
            ROOT / "cli",
            source / "cli",
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "skills"),
        )
        shutil.copytree(ROOT / "skills" / "sai", source / "cli" / "sai_cli" / "skills")
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                str(source),
                "--out-dir",
                str(args.out_dir.resolve()),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
