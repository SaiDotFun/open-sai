"""scripts/check_release.py and scripts/release.py against real Git repos."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_release
import release


def test_tag_error():
    assert check_release.tag_for("0.1.0") == "sai-cli/v0.1.0"
    assert check_release.tag_error("sai-cli/v0.1.0", "0.1.0") is None
    assert check_release.tag_error("sai-cli/v0.2.0-rc.1", "0.2.0-rc.1") is None
    assert "does not match" in str(check_release.tag_error("sai-cli/v0.2.0", "0.1.0"))
    bad = (
        "v0.1.0",
        "0.1.0",
        "sai-cli/0.1.0",
        "sai-cli/v0.1",
        "sai-cli/v0.1.0-beta",
        "sai-cli/v0.1.0rc1",
        "sai-rs/v0.1.0",
        "x/sai-cli/v0.1.0",
        "sai-cli/v0.01.0",
        "sai-cli/v00.1.0",
        "sai-cli/v0.1.00",
        "sai-cli/v0.1.0-rc.01",
    )
    for tag in bad:
        assert "is not" in str(check_release.tag_error(tag, "0.1.0")), tag


def test_check_release_matches_this_pyproject():
    version = check_release.pyproject_version()
    assert check_release.main([f"sai-cli/v{version}"]) == 0
    assert check_release.main(["sai-cli/v0.0.0-rc.0"]) == 1
    assert check_release.main([f"v{version}"]) == 1
    assert check_release.main([]) == 2


def test_a_leading_zero_cannot_release_even_when_pyproject_matches():
    assert "is not" in str(check_release.tag_error("sai-cli/v0.01.0", "0.01.0"))
    assert check_release.tag_error("sai-cli/v10.20.30", "10.20.30") is None
    assert check_release.tag_error("sai-cli/v0.2.0-rc.10", "0.2.0-rc.10") is None


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """A clone of a bare origin whose main declares version 1.2.3."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    git(work, "config", "commit.gpgsign", "false")
    git(work, "config", "tag.gpgsign", "false")
    (work / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    (work / "Cargo.toml").write_text('[workspace.package]\nversion = "1.2.3"\n')
    (work / "shim").mkdir()
    (work / "shim/pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "main")
    return work


def run(
    repo: Path, version: str, go: bool = False, *, python_only: bool = False
) -> int:
    return release.release(
        version, go, release.git_runner(repo), repo, python_only=python_only
    )


def test_dry_run_checks_and_changes_nothing(repo, capsys):
    assert run(repo, "1.2.3") == 0
    out = capsys.readouterr().out
    assert "git tag --annotate sai-cli/v1.2.3" in out
    assert "Sai-Release-Mode:" not in out
    assert "git push origin refs/tags/sai-cli/v1.2.3" in out
    assert "Dry run" in out
    assert git(repo, "tag", "--list") == ""
    assert git(repo, "ls-remote", "--tags", "origin") == ""


def test_python_only_dry_run_accepts_a_python_bump(repo: Path, capsys):
    config = repo / "pyproject.toml"
    config.write_text(config.read_text().replace("1.2.3", "1.2.4"))
    git(repo, "commit", "-qam", "bump Python only")
    git(repo, "push", "-q", "origin", "main")

    assert run(repo, "1.2.4") == 1
    assert "does not match the Cargo and shim version" in capsys.readouterr().err
    assert run(repo, "1.2.4", python_only=True) == 0
    preview = capsys.readouterr().out
    assert "git tag --annotate sai-cli/v1.2.4" in preview
    assert "Sai-Release-Mode: python-only" in preview
    assert "git push origin refs/tags/sai-cli/v1.2.4" in preview
    assert run(repo, "1.2.5", python_only=True) == 1
    assert "does not match pyproject.toml version 1.2.4" in capsys.readouterr().err
    assert git(repo, "tag", "--list") == ""
    assert git(repo, "ls-remote", "--tags", "origin") == ""


def test_mode_comes_only_from_the_tag_annotation(repo: Path):
    git(
        repo,
        "commit",
        "--allow-empty",
        "-qm",
        "example\n\nSai-Release-Mode: python-only",
    )
    git(repo, "tag", "sai-cli/v1.2.3")
    git(repo, "tag", "-am", "native", "sai-cli/v1.2.4")
    git(repo, *release.commands("sai-cli/v1.2.5", python_only=True)[0])
    git(repo, "tag", "-am", "example\n\nSai-Release-Mode: typo", "sai-cli/v1.2.6")

    assert check_release.tag_mode("sai-cli/v1.2.3", repo) == "native"
    assert check_release.tag_mode("sai-cli/v1.2.4", repo) == "native"
    assert check_release.tag_mode("sai-cli/v1.2.5", repo) == "python-only"
    with pytest.raises(ValueError, match="invalid Sai-Release-Mode"):
        check_release.tag_mode("sai-cli/v1.2.6", repo)


def test_run_tags_and_pushes(repo):
    assert run(repo, "1.2.3", go=True) == 0
    assert git(repo, "cat-file", "-t", "sai-cli/v1.2.3") == "tag"
    assert "refs/tags/sai-cli/v1.2.3" in git(repo, "ls-remote", "--tags", "origin")
    # A second release of the same version is refused.
    assert run(repo, "1.2.3") == 1


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("pyproject.toml", "does not match the Cargo and shim version"),
        ("Cargo.toml", "Cargo workspace and shim versions must name the same release"),
        (
            "shim/pyproject.toml",
            "Cargo workspace and shim versions must name the same release",
        ),
    ],
)
def test_refuses_a_bump_in_only_one_package(
    repo: Path, capsys, path: str, expected: str
):
    config = repo / path
    config.write_text(config.read_text().replace("1.2.3", "1.2.4"))
    git(repo, "add", path)
    git(repo, "commit", "-qm", "bump one package")
    git(repo, "push", "-q", "origin", "main")

    version = "1.2.4" if path == "pyproject.toml" else "1.2.3"
    for go in (False, True):
        assert run(repo, version, go=go) == 1
        assert expected in capsys.readouterr().err
    assert git(repo, "tag", "--list") == ""
    assert git(repo, "ls-remote", "--tags", "origin") == ""


def test_accepts_matching_release_candidate_spellings(repo: Path, capsys):
    for path in ("pyproject.toml", "Cargo.toml", "shim/pyproject.toml"):
        config = repo / path
        version = "1.2.4rc1" if path.startswith("shim/") else "1.2.4-rc.1"
        config.write_text(config.read_text().replace("1.2.3", version))
    git(repo, "commit", "-qam", "bump to release candidate")
    git(repo, "push", "-q", "origin", "main")

    assert run(repo, "1.2.4-rc.1") == 0
    assert "git tag --annotate sai-cli/v1.2.4-rc.1" in capsys.readouterr().out
    assert git(repo, "ls-remote", "--tags", "origin") == ""


def test_refuses_version_mismatch_dirty_tree_and_unpushed_head(repo, capsys):
    assert run(repo, "1.2.4", go=True) == 1
    assert "does not match" in capsys.readouterr().err

    (repo / "stray.txt").write_text("x")
    assert run(repo, "1.2.3", go=True) == 1
    assert "uncommitted" in capsys.readouterr().err

    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "local only")
    assert run(repo, "1.2.3", go=True) == 1
    assert "is not origin/main" in capsys.readouterr().err
    assert git(repo, "tag", "--list") == ""


def test_remote_tag_commit_reads_annotated_and_lightweight_tags():
    annotated = "aaa\trefs/tags/sai-cli/v1.2.3\nbbb\trefs/tags/sai-cli/v1.2.3^{}\n"
    assert check_release.remote_tag_commit(annotated, "sai-cli/v1.2.3") == "bbb"
    light = "ccc\trefs/tags/sai-cli/v1.2.3\n"
    assert check_release.remote_tag_commit(light, "sai-cli/v1.2.3") == "ccc"
    assert check_release.remote_tag_commit(light, "sai-cli/v9.9.9") is None
    assert check_release.remote_tag_commit("", "sai-cli/v1.2.3") is None


def test_remote_tag_object_detects_a_changed_annotation_on_the_same_commit():
    tag = "sai-cli/v1.2.3"
    listing = f"aaa\trefs/tags/{tag}\nbbb\trefs/tags/{tag}^{{}}\n"
    assert check_release.remote_tag_error(tag, "bbb", listing, "aaa") is None
    assert check_release.remote_tag_error(tag, "bbb", listing, "ccc") == (
        f"tag {tag} on the remote does not match the built tag object"
    )


def test_built_commit_must_still_be_the_remote_tag(repo, monkeypatch, capsys):
    """The workflow's last check before publishing, against a real remote."""
    monkeypatch.setattr(check_release, "ROOT", repo)
    monkeypatch.setattr(check_release, "pyproject_version", lambda root=repo: "1.2.3")
    tag = "sai-cli/v1.2.3"
    origin = git(repo, "remote", "get-url", "origin")
    built = git(repo, "rev-parse", "HEAD")
    arguments = [tag, "--built", built, "--remote", origin]

    assert check_release.main(arguments) == 1
    assert "is not on the remote" in capsys.readouterr().err

    assert run(repo, "1.2.3", go=True) == 0
    assert check_release.main(arguments) == 0
    assert "the remote tag is" in capsys.readouterr().out

    # Someone moves the tag to a later commit while the build runs.
    (repo / "later.txt").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "later")
    git(repo, "tag", "--force", "--annotate", tag, "--message", tag)
    git(repo, "push", "-q", "--force", "origin", f"refs/tags/{tag}")
    assert check_release.main(arguments) == 1
    assert "not the built commit" in capsys.readouterr().err

    assert check_release.main([tag, "--built", built]) == 2
