"""Check that a release tag names the version in pyproject.toml.

The release workflow runs this before it builds anything, so a tag pushed
without its version bump fails instead of publishing a mislabeled release.

Release tags carry the component name, `sai-cli/vX.Y.Z`, so other components
in this repository can release under their own prefixes.

With `--built COMMIT --remote URL` it also checks that the tag on the remote
still points at COMMIT, the commit the workflow built, so a tag moved during
the build cannot get the earlier build's files.

Usage: python scripts/check_release.py sai-cli/vX.Y.Z [--built COMMIT --remote URL]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
TAG_PREFIX = "sai-cli/v"
MODE_TRAILER = "Sai-Release-Mode"
PYTHON_ONLY_TRAILER = f"{MODE_TRAILER}: python-only"
# sai-cli/vMAJOR.MINOR.PATCH, or a pre-release such as sai-cli/v0.2.0-rc.1.
# Numbers have no leading zero: packaging turns 0.01.0 into 0.1.0, so the
# wheel of such a tag would not be where the tag says.
NUMBER = r"(0|[1-9]\d*)"
TAG = re.compile(
    rf"^{re.escape(TAG_PREFIX)}{NUMBER}\.{NUMBER}\.{NUMBER}(-rc\.{NUMBER})?$"
)


def pyproject_version(root: Path = ROOT) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def tag_for(version: str) -> str:
    """The release tag of package version VERSION."""
    return f"{TAG_PREFIX}{version}"


def tag_error(tag: str, version: str) -> str | None:
    """Return why TAG cannot release VERSION, or None when it can."""
    if not TAG.match(tag):
        return f"tag {tag!r} is not {TAG_PREFIX}X.Y.Z or {TAG_PREFIX}X.Y.Z-rc.N"
    if tag != tag_for(version):
        return f"tag {tag} does not match pyproject.toml version {version}"
    return None


def native_versions(root: Path = ROOT) -> tuple[str, str]:
    cargo = tomllib.loads((root / "Cargo.toml").read_text())["workspace"]["package"]
    shim = tomllib.loads((root / "shim" / "pyproject.toml").read_text())["project"]
    cargo_version, shim_version = cargo["version"], shim["version"]
    if not TAG.fullmatch(
        tag_for(cargo_version)
    ) or shim_version != cargo_version.replace("-rc.", "rc"):
        raise ValueError("Cargo workspace and shim versions must name the same release")
    return cargo_version, shim_version


def check_tag(tag: str, native_version: str, python_version: str) -> None:
    """Require a release tag to match the full Python package and native distribution."""
    if error := tag_error(tag, python_version):
        raise ValueError(error)
    if tag != tag_for(native_version):
        raise ValueError(
            f"tag {tag} does not match the Cargo and shim version {native_version}"
        )


def tag_mode(tag: str, root: Path = ROOT) -> Literal["native", "python-only"]:
    """Only an annotated tag can opt in to Python-only publication."""
    ref = f"refs/tags/{tag}"

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=True,
            text=True,
            timeout=60,
        ).stdout.strip()

    if git("cat-file", "-t", ref) != "tag":
        return "native"
    trailer = git(
        "for-each-ref", f"--format=%(trailers:key={MODE_TRAILER},unfold)", ref
    )
    if not trailer:
        return "native"
    if trailer == PYTHON_ONLY_TRAILER:
        return "python-only"
    raise ValueError(f"invalid {MODE_TRAILER} trailer on {tag}")


def tag_refs(listing: str) -> dict[str, str]:
    return dict(
        reversed(line.split("\t", 1)) for line in listing.splitlines() if "\t" in line
    )


def remote_tag_commit(listing: str, tag: str) -> str | None:
    """Return the commit TAG points at in `git ls-remote` output LISTING.

    An annotated tag lists the tag object and, as `refs/tags/TAG^{}`, the
    commit it points at; a lightweight tag lists the commit only.
    """
    refs = tag_refs(listing)
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def remote_tag_error(
    tag: str, built: str, listing: str, tag_object: str | None = None
) -> str | None:
    """Return why the remote TAG is not the commit BUILT, or None."""
    commit = remote_tag_commit(listing, tag)
    if commit is None:
        return f"tag {tag} is not on the remote"
    if commit != built:
        return (
            f"tag {tag} on the remote points at {commit[:12]}, not the built"
            f" commit {built[:12]}"
        )
    if (
        tag_object is not None
        and tag_refs(listing).get(f"refs/tags/{tag}") != tag_object
    ):
        return f"tag {tag} on the remote does not match the built tag object"
    return None


def ls_remote(remote: str, tag: str) -> str:
    refs = [f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"]
    return subprocess.run(
        ["git", "ls-remote", "--tags", remote, *refs],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("tag", help="sai-cli/vX.Y.Z")
    parser.add_argument("--built", metavar="COMMIT", help="the commit that was built")
    parser.add_argument(
        "--remote", metavar="URL", help="the remote to read the tag from"
    )
    parser.add_argument(
        "--tag-object", help="also require this exact remote tag object"
    )
    parser.add_argument(
        "--print-mode", action="store_true", help="print the local tag mode"
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    if bool(args.built) != bool(args.remote):
        print("error: --built and --remote go together", file=sys.stderr)
        return 2
    if args.tag_object and not args.built:
        print("error: --tag-object requires --built and --remote", file=sys.stderr)
        return 2
    tag = args.tag
    error = tag_error(tag, pyproject_version())
    if error is None and args.built:
        try:
            error = remote_tag_error(
                tag, args.built, ls_remote(args.remote, tag), args.tag_object
            )
        except (subprocess.SubprocessError, OSError) as exc:
            error = f"could not read {tag} from the remote: {type(exc).__name__}"
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.print_mode:
        try:
            print(tag_mode(tag, ROOT))
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    elif args.built:
        print(f"{tag} matches pyproject.toml and the remote tag is {args.built[:12]}")
    else:
        print(f"{tag} matches pyproject.toml")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
