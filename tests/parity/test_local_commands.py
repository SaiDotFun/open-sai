"""C9b parity against Python ea09f0d417a53a71079bb8c5e426005fa29051a4."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "sai"


@pytest.fixture
def local_pair(
    cli_pair: tuple[tuple[str, ...], tuple[str, ...]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    # CI's C4 reference predates these commands. Its current checkout
    # retains the Python C9b implementation and installs it alongside pytest.
    reference = os.environ.get("SAI_C9B_PARITY_PYTHON") or shutil.which(
        "sai", path=str(Path(sys.executable).parent)
    )
    assert reference is not None
    return (reference,), cli_pair[1]


def case_env(root: Path, *, logging: str) -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": str(root),
        "SAI_HOME": str(root / "sai-home"),
        "CI": "1",
        "NO_COLOR": "1",
        "PYTHONUTF8": "1",
        "SAI_LOG": logging,
    }


def invoke(
    cli: tuple[str, ...],
    root: Path,
    *args: str,
    stdin: bytes = b"",
    logging: str = "0",
) -> subprocess.CompletedProcess[bytes]:
    root.mkdir(mode=0o700, exist_ok=True)
    return subprocess.run(
        [*cli, *args],
        cwd=root,
        env=case_env(root, logging=logging),
        input=stdin,
        capture_output=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize(
    "args,page",
    [
        (("--skill",), "SKILL.md"),
        (("skill", "show"), "SKILL.md"),
        (("--json", "skill", "show", "--page", "SKILL.md"), "SKILL.md"),
        (("--json", "skill", "show", "--page", "contracts"), "references/contracts.md"),
        (("skill", "show", "--page", "examples/prompts.md"), "examples/prompts.md"),
    ],
)
def test_skill_show(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    args: tuple[str, ...],
    page: str,
) -> None:
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        result = invoke(cli, root, *args, logging="1")
        assert result.returncode == 0 and result.stderr == b""
        assert result.stdout == (SKILL / page).read_bytes()
        assert not (root / "sai-home").exists()


def test_skill_source(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    reports = []
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        result = invoke(cli, root, "--json", "skill", "source")
        assert result.returncode == 0 and result.stderr == b""
        report = json.loads(result.stdout)
        assert Path(report.pop("path")).exists()
        assert report.pop("cliVersion")
        assert Path(report["install"].pop("defaultDir")) == root / ".agents/skills/sai"
        reports.append(report)
    assert reports[0] == reports[1]


@pytest.mark.parametrize("mode", ["default", "modified", "symlink", "unrelated"])
def test_skill_install(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path, mode: str
) -> None:
    package = {
        path.relative_to(SKILL): path.read_bytes()
        for path in SKILL.rglob("*")
        if path.is_file()
    }
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        root.mkdir()
        target = root / ".agents/skills/sai" if mode == "default" else root / "skill"
        options = [] if mode == "default" else ["--dir", str(target)]
        if mode == "modified":
            shutil.copytree(SKILL, target)
            (target / "cli.md").write_bytes(b"edited")
        elif mode == "symlink":
            target.symlink_to("external", target_is_directory=True)
        elif mode == "unrelated":
            target.mkdir()
            (target / "SKILL.md").write_bytes(b"---\nname: another\n")
        if mode in {"modified", "symlink"}:
            refused = invoke(cli, root, "--json", "skill", "install", *options)
            assert refused.returncode == 1 and refused.stderr == b""
            assert "--force" in json.loads(refused.stdout)["error"]["message"]
        force = [] if mode == "default" else ["--force"]
        result = invoke(cli, root, "--json", "skill", "install", *options, *force)
        if mode == "unrelated":
            assert result.returncode == 1
            assert (target / "SKILL.md").read_bytes() == b"---\nname: another\n"
            continue
        assert result.returncode == 0 and result.stderr == b""
        report = json.loads(result.stdout)
        assert report["action"] == ("installed" if mode == "default" else "replaced")
        assert report["files"] == len(package)
        assert Path(report["dir"]) == target
        assert report["commit"] == json.loads(package[Path(".source.json")])["commit"]
        assert stat.S_IMODE(target.stat().st_mode) == 0o755
        assert not target.is_symlink()
        assert not (root / "external").exists()
        assert {
            path.relative_to(target): path.read_bytes()
            for path in target.rglob("*")
            if path.is_file()
        } == package
        repeated = invoke(cli, root, "--json", "skill", "install", *options)
        assert repeated.returncode == 0 and repeated.stderr == b""
        assert json.loads(repeated.stdout) == {**report, "action": "unchanged"}


def test_command_log_v2(
    local_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    runs = []
    for index, cli in enumerate(local_pair):
        root = tmp_path / str(index)
        cli_json(cli, root, "config", "list")
        logs = root / "sai-home/logs"
        rows = [
            json.loads(line)
            for file in logs.glob("*.jsonl")
            for line in file.read_bytes().splitlines()
        ]
        assert [row["event"] for row in rows] == ["command.start", "command.end"]
        assert all(row["schema"] == 2 for row in rows)
        assert rows[0]["runId"] == rows[1]["runId"]
        assert rows[0]["commandId"] == rows[1]["commandId"]
        assert len(rows[0]["commandId"]) == 32
        for row in rows:
            for volatile in (
                "ts",
                "pid",
                "version",
                "runId",
                "commandId",
                "durationMs",
            ):
                row.pop(volatile, None)
        runs.append(rows)
    assert runs[0] == runs[1]
