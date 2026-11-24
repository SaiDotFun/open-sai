"""Build a full Python bridge wheel at an explicitly chosen release version."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRE_BRIDGE = (0, 1, 2)
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    match = VERSION.fullmatch(args.version)
    if match is None or tuple(map(int, match.groups())) <= PRE_BRIDGE:
        parser.error("the bridge requires a distinct stable version newer than 0.1.2")
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
