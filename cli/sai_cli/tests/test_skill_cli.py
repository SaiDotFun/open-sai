"""`sai skill show|source|install` and the packaged skill directory."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tarfile
import zipfile
from importlib import metadata
from pathlib import Path

import pytest
from click.testing import CliRunner
from sai_cli import skill
from sai_cli.sai_cli import main

REPO = Path(__file__).resolve().parents[3]
REPO_SKILL = REPO / "skills" / "sai"
sys.path.insert(0, str(REPO / "scripts"))

import check_dist

PAGES = ("cli", "mcp", "trading-safety", "references/contracts")


# Directories the fixture puts beside the install target: HOME, and
# SAI_HOME, where the commands under test write their log.
OTHER_DIRS = ("home", "sai-home")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A throwaway HOME, so no test touches the real ~/.agents."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SAI_HOME", str(tmp_path / "sai-home"))
    assert Path.home() == home
    return home


def invoke(*args: str):
    return CliRunner().invoke(main, list(args))


def repo_files() -> dict[str, bytes]:
    return check_dist.source_files(REPO_SKILL)


def tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_packaged_directory_is_the_repository_skill():
    files = skill.packaged_files()
    assert files == repo_files()
    for page in PAGES:
        assert f"{page}.md" in files
    assert files["SKILL.md"].startswith(b"---\nname: sai\n")


def test_skill_pages_link_only_to_files_that_ship():
    import re

    files = repo_files()
    for name, data in files.items():
        if not name.endswith(".md"):
            continue
        base = Path(name).parent
        for target in re.findall(r"\]\(([^)#\s]+)", data.decode()):
            if "://" in target:
                continue
            assert (base / target).as_posix() in files, f"{name} links to {target}"


def test_show_prints_skill_md_and_flag_is_an_alias(home):
    shown = invoke("skill", "show")
    assert shown.exit_code == 0
    assert shown.stdout_bytes == (REPO_SKILL / "SKILL.md").read_bytes()
    assert invoke("--skill").stdout_bytes == shown.stdout_bytes
    assert invoke("--json", "skill", "show").stdout_bytes == shown.stdout_bytes
    assert not (home / ".agents").exists()


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("cli", "cli.md"),
        ("mcp.md", "mcp.md"),
        ("trading-safety", "trading-safety.md"),
        ("references/contracts", "references/contracts.md"),
        ("vaults", "references/vaults.md"),
        ("SKILL", "SKILL.md"),
    ],
)
def test_show_page(name, path):
    result = invoke("skill", "show", "--page", name)
    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == (REPO_SKILL / path).read_bytes()


def test_cli_skill_documents_nullable_market_price_timestamp():
    result = invoke("skill", "show", "--page", "cli")
    assert result.exit_code == 0, result.output
    page = " ".join(result.stdout.split())
    assert "MCP tool `market_prices`" in page
    assert "`lastUpdatedBlock.block_ts` field is `string | null`" in page
    assert "A `null` timestamp means the block time is unknown" in page
    assert "`lastUpdatedBlock.block` field still reports the height" in page


def test_show_unknown_page_lists_pages():
    result = invoke("skill", "show", "--page", "nope")
    assert result.exit_code == 1
    assert "does not exist" in result.output
    assert "trading-safety" in result.output


def test_show_ambiguous_page_name():
    files = {"SKILL.md": b"---\n", "a/x.md": b"a", "b/x.md": b"b"}
    assert skill.find_page(files, "a/x") == b"a"
    with pytest.raises(Exception, match="ambiguous"):
        skill.find_page(files, "x")


def test_source_json_names_both_repositories_and_the_synced_commit(home):
    result = invoke("--json", "skill", "source")
    assert result.exit_code == 0, result.output
    info = json.loads(result.stdout)
    record = json.loads((REPO_SKILL / ".source.json").read_text())
    assert info["name"] == "sai"
    assert info["cliVersion"] == metadata.version("sai-cli")
    assert Path(info["path"]).resolve() == REPO_SKILL.resolve()
    assert info["repositories"] == {
        "cli": "https://github.com/SaiDotFun/open-sai",
        "skill": "https://github.com/Unique-Divine/jiyuu",
    }
    assert info["source"]["commit"] == record["commit"]
    assert len(info["source"]["commit"]) == 40
    assert info["source"]["path"] == "ai-skills/sai"
    assert set(PAGES) <= set(info["pages"])
    assert info["install"] == {
        "sai": "sai skill install",
        "npx": "npx skills add SaiDotFun/open-sai --skill sai",
        "defaultDir": str(home / ".agents" / "skills" / "sai"),
    }


def test_source_text(home):
    result = invoke("skill", "source")
    assert result.exit_code == 0
    record = json.loads((REPO_SKILL / ".source.json").read_text())
    for expected in (
        "https://github.com/SaiDotFun/open-sai",
        "https://github.com/Unique-Divine/jiyuu",
        record["commit"],
        "sai skill install",
        "npx skills add SaiDotFun/open-sai --skill sai",
    ):
        assert expected in result.output


def test_source_without_a_record_says_so():
    info = skill.source_info({"SKILL.md": b"---\n"}, "1.2.3")
    assert info["source"]["commit"] is None
    assert "not recorded" in skill.render_source(info)
    assert skill.source_record({".source.json": b"not json"}) == {}


def test_install_defaults_to_agents_skills_under_home(home):
    result = invoke("--json", "skill", "install")
    assert result.exit_code == 0, result.output
    target = home / ".agents" / "skills" / "sai"
    report = json.loads(result.stdout)
    assert report["action"] == "installed"
    assert report["dir"] == str(target)
    assert report["files"] == len(repo_files())
    assert len(report["commit"]) == 40
    assert tree(target) == repo_files()

    again = invoke("skill", "install")
    assert again.exit_code == 0
    assert "already up to date" in again.output
    assert tree(target) == repo_files()


def test_install_into_dir_writes_the_skill_there(home, tmp_path):
    target = tmp_path / "claude" / "skills" / "sai"
    result = invoke("skill", "install", "--dir", str(target))
    assert result.exit_code == 0, result.output
    assert f"Installed the sai skill at {target}" in result.output
    assert tree(target) == repo_files()
    assert not (home / ".agents").exists()
    # Nothing is left beside the target.
    assert [p.name for p in target.parent.iterdir()] == ["sai"]


def test_install_refuses_differing_content_without_force(home, tmp_path):
    target = tmp_path / "sai"
    assert invoke("skill", "install", "--dir", str(target)).exit_code == 0
    (target / "cli.md").write_text("my edit\n")
    (target / "mine.md").write_text("extra\n")

    refused = invoke("skill", "install", "--dir", str(target))
    assert refused.exit_code == 1
    assert "differs from the packaged skill in 2 file(s)" in refused.output
    assert "cli.md" in refused.output and "mine.md" in refused.output
    assert (target / "cli.md").read_text() == "my edit\n"

    forced = invoke("--json", "skill", "install", "--dir", str(target), "--force")
    assert forced.exit_code == 0, forced.output
    assert json.loads(forced.stdout)["action"] == "replaced"
    assert tree(target) == repo_files()


def test_install_refuses_a_symlink_without_force(home, tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "SKILL.md").write_text("---\nname: sai\n---\nmanaged copy\n")
    target = home / ".agents" / "skills" / "sai"
    target.parent.mkdir(parents=True)
    target.symlink_to(managed)

    refused = invoke("skill", "install")
    assert refused.exit_code == 1
    assert "is a symlink" in refused.output
    assert target.is_symlink()

    forced = invoke("skill", "install", "--force")
    assert forced.exit_code == 0, forced.output
    assert "Replaced the sai skill" in forced.output
    assert not target.is_symlink()
    assert tree(target) == repo_files()
    # The link's old target keeps its content.
    assert tree(managed) == {"SKILL.md": b"---\nname: sai\n---\nmanaged copy\n"}


def test_install_never_replaces_a_directory_that_is_not_the_skill(home, tmp_path):
    target = tmp_path / "projects"
    target.mkdir()
    (target / "notes.txt").write_text("keep me\n")
    for extra in ([], ["--force"]):
        result = invoke("skill", "install", "--dir", str(target), *extra)
        assert result.exit_code == 1
        assert "something other than the sai skill" in result.output
    assert tree(target) == {"notes.txt": b"keep me\n"}

    as_file = tmp_path / "file"
    as_file.write_text("x")
    result = invoke("skill", "install", "--dir", str(as_file), "--force")
    assert result.exit_code != 0
    assert as_file.read_text() == "x"


def test_install_fills_an_empty_directory(home, tmp_path):
    target = tmp_path / "empty"
    target.mkdir()
    assert invoke("skill", "install", "--dir", str(target)).exit_code == 0
    assert tree(target) == repo_files()


def snapshot(root: Path) -> dict[str, str]:
    """Every entry under ROOT, links recorded as links."""
    out = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            out[name] = f"link:{path.readlink()}"
        elif path.is_file():
            out[name] = path.read_text()
        else:
            out[name] = "dir"
    return out


def test_install_never_wipes_a_directory_of_links_to_other_skills(home, tmp_path):
    # ~/.agents/skills itself: no regular files, only links to skills.
    other = tmp_path / "repo" / "other-skill"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: other\n---\n")
    skills = home / ".agents" / "skills"
    skills.mkdir(parents=True)
    (skills / "other").symlink_to(other)
    (skills / "dangling").symlink_to(tmp_path / "missing")
    before = snapshot(skills)

    for extra in ([], ["--force"]):
        result = invoke("skill", "install", "--dir", str(skills), *extra)
        assert result.exit_code == 1, result.output
        assert "something other than the sai skill" in result.output
        assert snapshot(skills) == before
    assert snapshot(other) == {"SKILL.md": "---\nname: other\n---\n"}


def test_install_does_not_trust_a_linked_skill_md(home, tmp_path):
    # A SKILL.md that is a link to a sai skill does not make the directory
    # one: replacing it would delete the other entries.
    real = tmp_path / "real"
    assert invoke("skill", "install", "--dir", str(real)).exit_code == 0
    target = tmp_path / "mixed"
    target.mkdir()
    (target / "SKILL.md").symlink_to(real / "SKILL.md")
    (target / "notes.txt").write_text("keep me\n")
    before = snapshot(target)
    result = invoke("skill", "install", "--dir", str(target), "--force")
    assert result.exit_code == 1
    assert "something other than the sai skill" in result.output
    assert snapshot(target) == before
    assert tree(real) == repo_files()


def test_install_refuses_a_skill_directory_that_holds_links(home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "data.md").write_text("mine\n")
    target = tmp_path / "sai"
    assert invoke("skill", "install", "--dir", str(target)).exit_code == 0
    (target / "extra").symlink_to(elsewhere)
    before = snapshot(target)

    for extra in ([], ["--force"]):
        result = invoke("skill", "install", "--dir", str(target), *extra)
        assert result.exit_code == 1, result.output
        assert "contains symlinks" in result.output
        assert snapshot(target) == before
    assert snapshot(elsewhere) == {"data.md": "mine\n"}


def test_install_from_inside_the_destination_changes_nothing(
    home, tmp_path, monkeypatch
):
    target = tmp_path / "sai"
    assert invoke("skill", "install", "--dir", str(target)).exit_code == 0
    (target / "cli.md").write_text("my edit\n")
    before = snapshot(target)

    for cwd in (target, target / "references"):
        monkeypatch.chdir(cwd)
        for where in (".", str(target)):
            if cwd != target and where == ".":
                continue
            result = invoke("skill", "install", "--dir", where, "--force")
            assert result.exit_code == 1, result.output
            assert "current directory is inside" in result.output
            assert snapshot(target) == before
    # No staging directory is left beside the destination.
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    # An up-to-date copy is still reported from inside it.
    (target / "cli.md").write_bytes(repo_files()["cli.md"])
    monkeypatch.chdir(target)
    result = invoke("skill", "install", "--dir", ".")
    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output


def test_install_relative_dir_stages_beside_the_destination(
    home, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert invoke("skill", "install", "--dir", "sai").exit_code == 0
    (tmp_path / "sai" / "cli.md").write_text("my edit\n")
    result = invoke("skill", "install", "--dir", "sai", "--force")
    assert result.exit_code == 0, result.output
    assert tree(tmp_path / "sai") == repo_files()
    assert [p.name for p in tmp_path.iterdir() if p.name not in OTHER_DIRS] == ["sai"]


def test_failed_replacement_keeps_the_previous_copy(home, tmp_path, monkeypatch):
    target = tmp_path / "sai"
    assert invoke("skill", "install", "--dir", str(target)).exit_code == 0
    (target / "cli.md").write_text("my edit\n")
    before = snapshot(target)

    real_replace = os.replace

    def fail_publish(src, dst):
        # Fail moving the new copy into place; let the old one move back.
        if Path(dst) == target and "-new-" in Path(src).name:
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(skill.os, "replace", fail_publish)
    with pytest.raises(OSError, match="disk full"):
        skill.install(skill.packaged_files(), target, force=True)
    monkeypatch.undo()
    assert snapshot(target) == before
    assert [p.name for p in tmp_path.iterdir() if p.name not in OTHER_DIRS] == ["sai"]


def interrupt_publish(monkeypatch, target: Path) -> None:
    """Make the move of the new copy into TARGET raise KeyboardInterrupt."""
    real_replace = os.replace

    def replace(src, dst):
        if Path(dst) == target and "-new-" in Path(src).name:
            raise KeyboardInterrupt
        real_replace(src, dst)

    monkeypatch.setattr(skill.os, "replace", replace)


def test_interrupted_replacement_puts_the_directory_back(home, tmp_path, monkeypatch):
    target = tmp_path / "sai"
    assert invoke("skill", "install", "--dir", str(target)).exit_code == 0
    (target / "cli.md").write_text("my edit\n")
    before = snapshot(target)

    interrupt_publish(monkeypatch, target)
    with pytest.raises(KeyboardInterrupt):
        skill.install(skill.packaged_files(), target, force=True)
    monkeypatch.undo()
    assert snapshot(target) == before
    assert [p.name for p in tmp_path.iterdir() if p.name not in OTHER_DIRS] == ["sai"]


def test_interrupted_replacement_puts_the_symlink_back(home, tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "SKILL.md").write_text("---\nname: sai\n---\nmanaged copy\n")
    target = tmp_path / "sai"
    target.symlink_to(managed)

    for error in (KeyboardInterrupt, OSError):
        real_replace = os.replace

        def replace(src, dst, error=error, real_replace=real_replace):
            if Path(dst) == target and "-new-" in Path(src).name:
                raise error
            real_replace(src, dst)

        monkeypatch.setattr(skill.os, "replace", replace)
        with pytest.raises(error):
            skill.install(skill.packaged_files(), target, force=True)
        monkeypatch.undo()
        assert target.is_symlink()
        assert target.readlink() == managed
        assert snapshot(managed) == {"SKILL.md": "---\nname: sai\n---\nmanaged copy\n"}
        assert sorted(p.name for p in tmp_path.iterdir() if p.name != "home") == [
            "managed",
            "sai",
        ]


# scripts/check_dist.py


def write_wheel(path: Path, files: dict[str, bytes], link: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(f"sai_cli/skills/{name}")
            info.external_attr = (0o120777 if name == link else 0o100644) << 16
            archive.writestr(info, data)
    return path


def write_sdist(path: Path, files: dict[str, bytes], link_dir: bool = False) -> Path:
    import io

    with tarfile.open(path, "w:gz") as archive:
        if link_dir:
            member = tarfile.TarInfo("sai_cli-0.1.0/cli/sai_cli/skills")
            member.type = tarfile.SYMTYPE
            member.linkname = "../../skills/sai"
            archive.addfile(member)
        for name, data in files.items():
            member = tarfile.TarInfo(f"sai_cli-0.1.0/cli/sai_cli/skills/{name}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


def test_check_dist_accepts_archives_with_every_skill_file(tmp_path):
    files = {"SKILL.md": b"---\n", "cli.md": b"c", "references/rest.md": b"r"}
    write_wheel(tmp_path / "a.whl", files)
    write_sdist(tmp_path / "a.tar.gz", files)
    assert check_dist.check(tmp_path, files) == []


def test_check_dist_reports_missing_changed_and_extra_files(tmp_path):
    files = {"SKILL.md": b"---\n", "cli.md": b"c", "references/rest.md": b"r"}
    write_wheel(tmp_path / "a.whl", {"SKILL.md": b"---\n", "cli.md": b"other"})
    write_sdist(tmp_path / "a.tar.gz", {**files, "stray.md": b"s"})
    errors = check_dist.check(tmp_path, files)
    assert len(errors) == 3
    assert any("references/rest.md is missing" in e for e in errors)
    assert any("cli.md differs" in e for e in errors)
    assert any("stray.md is not in skills/sai" in e for e in errors)


def test_check_dist_rejects_symlinks_in_either_archive(tmp_path):
    files = {"SKILL.md": b"---\n"}
    write_wheel(tmp_path / "a.whl", files, link="SKILL.md")
    write_sdist(tmp_path / "a.tar.gz", {}, link_dir=True)
    errors = check_dist.check(tmp_path, files)
    assert len(errors) == 2
    assert any("is a symlink" in e for e in errors)
    assert any("is not a regular file" in e for e in errors)


def test_check_dist_source_walk_includes_hidden_directories(tmp_path):
    (tmp_path / ".templates").mkdir()
    (tmp_path / ".templates" / "t.txt").write_text("t")
    (tmp_path / "references" / ".fixtures" / "nested").mkdir(parents=True)
    (tmp_path / "references" / ".fixtures" / "nested" / "e.txt").write_text("e")
    (tmp_path / ".dot").write_text("d")
    (tmp_path / "SKILL.md").write_text("---\n")
    assert set(check_dist.source_files(tmp_path)) == {
        ".templates/t.txt",
        "references/.fixtures/nested/e.txt",
        ".dot",
        "SKILL.md",
    }


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build")
def test_build_ships_hidden_files_and_directories(tmp_path):
    """Build a copy of the package whose skill has hidden directories."""
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "setup.py"):
        shutil.copy(REPO / name, project / name)
    # copytree follows the skills symlink, so the copy holds real files.
    shutil.copytree(
        REPO / "cli" / "sai_cli",
        project / "cli" / "sai_cli",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    skills = project / "cli" / "sai_cli" / "skills"
    (skills / ".templates").mkdir()
    (skills / ".templates" / "template.txt").write_text("t\n")
    (skills / "references" / ".fixtures" / "nested").mkdir(parents=True)
    (skills / "references" / ".fixtures" / "nested" / "example.txt").write_text("e\n")
    (skills / "references" / "data" / "deep").mkdir(parents=True)
    (skills / "references" / "data" / "deep" / "x.json").write_text("{}\n")
    # Names with glob characters must be packaged as themselves.
    (skills / "references" / "notes [draft].md").write_text("n\n")
    (skills / "references" / "what?.md").write_text("w\n")
    (skills / "references" / "star*.md").write_text("s\n")

    expected = check_dist.source_files(skills)
    assert ".templates/template.txt" in expected
    assert "references/.fixtures/nested/example.txt" in expected
    assert "references/notes [draft].md" in expected
    out = tmp_path / "dist"
    check_dist.build(project, out)
    assert check_dist.check(out, expected) == []
