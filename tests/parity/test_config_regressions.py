"""Compare config edits and Unix input handling against the Python CLI."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from release_mismatch import assert_release_equal


def invoke(
    cli: tuple[str, ...],
    home: Path,
    *args: str,
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    env = {
        "PATH": os.defpath,
        "HOME": str(home.parent),
        "SAI_HOME": str(home),
        "SAI_LOG": "0",
        "PYTHONUTF8": "1",
    }
    if overrides is not None:
        env.update(overrides)
    return subprocess.run(
        [*cli, "--json", "config", *args],
        env=env,
        cwd=home.parent,
        capture_output=True,
        check=False,
        timeout=10,
    )


def reply(result: subprocess.CompletedProcess[bytes], home: Path) -> dict[str, object]:
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    data = json.loads(result.stdout)
    assert os.fsencode(data.pop("path")) == os.fsencode(home / "config.toml")
    return data


@pytest.mark.parametrize("verb", ["set", "unset"])
@pytest.mark.parametrize("following_table", [False, True])
def test_explicit_table_with_dotted_assignment(
    cli_pair: tuple[tuple[str, ...], tuple[str, ...]],
    tmp_path: Path,
    verb: str,
    following_table: bool,
) -> None:
    follower = b'# lcd note\n[lcd]\nendpoint = "https://lcd.example"\n'
    suffix = follower if following_table else b""
    initial = b"[keeper]\nendpoint.a = 1\n" + suffix
    replies = []
    configs = []
    for index, cli in enumerate(cli_pair):
        home = tmp_path / str(index)
        home.mkdir(mode=0o700)
        path = home / "config.toml"
        path.write_bytes(initial)
        args = [verb, "keeper.endpoint"]
        if verb == "set":
            args.append("https://keeper.example")
        replies.append(reply(invoke(cli, home, *args), home))
        content = path.read_bytes()
        configs.append(content)
        if following_table:
            assert follower.splitlines()[0] in content
            assert content.endswith(follower.split(b"\n", 1)[1])
    assert replies[0] == replies[1]
    assert configs[0] == configs[1]


@pytest.mark.skipif(os.name != "posix", reason="requires Unix filesystem bytes")
def test_non_utf8_home_json(
    cli_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path
) -> None:
    replies = []
    for index, cli in enumerate(cli_pair):
        root = tmp_path / str(index)
        root.mkdir(mode=0o700)
        home = root / os.fsdecode(b'home-\xff-\xe2\x82-\xc3\xa9-"')
        outputs = []
        for args in (
            ("set", "update.auto", "false"),
            ("list",),
            ("unset", "update.auto"),
        ):
            result = invoke(cli, home, *args)
            data = reply(result, home)
            if args == ("list",):
                # The pinned Python reference predates origin-only endpoint output.
                settings = data["settings"]
                assert isinstance(settings, dict)
                data = {"settings": {"update.auto": settings["update.auto"]}}
            outputs.append(data)
            assert rb"home-\udcff-\udce2\udc82-\u00e9-\"/config.toml" in result.stdout
        assert b"auto = false" not in (home / "config.toml").read_bytes()
        replies.append(outputs)
    assert replies[0] == replies[1]


@pytest.mark.skipif(os.name != "posix", reason="requires Unix environment bytes")
@pytest.mark.parametrize("setting", ["SAI_ENDPOINT", "SAI_LOG"])
def test_undecodable_environment(
    cli_pair: tuple[tuple[str, ...], tuple[str, ...]], tmp_path: Path, setting: str
) -> None:
    observed = []
    for index, cli in enumerate(cli_pair):
        home = tmp_path / str(index)
        home.mkdir(mode=0o700)
        (home / "config.toml").write_text(
            '[keeper]\nendpoint = "https://file.example"\n[logs]\nenabled = true\n',
            encoding="utf-8",
        )
        key = "keeper.endpoint" if setting == "SAI_ENDPOINT" else "logs.enabled"
        result = invoke(
            cli, home, "get", key, overrides={setting: os.fsdecode(b"\xff")}
        )
        observed.append((result.returncode, result.stdout, result.stderr))
        assert not (home / "logs").exists()
    if setting == "SAI_ENDPOINT":
        assert observed[0][0] == 2
        assert observed[0][2] == observed[1][2] == b""
        assert observed[0][1] == observed[1][1]
        error = json.loads(observed[0][1])["error"]
        assert error["code"] == "INVALID_REQUEST"
        assert error["message"].startswith(
            "SAI_ENDPOINT: Invalid value for keeper.endpoint"
        )
        assert_release_equal({"exit": observed[0][0]}, {"exit": observed[1][0]})
    else:
        assert observed[0][:2] == observed[1][:2]
        assert observed[0][2].startswith(b"SAI_LOG: invalid value for logs.enabled;")
        assert observed[1][2].startswith(
            b"SAI_LOG: Invalid value for logs.enabled: expected true, false,"
        )
        assert observed[0][0] == 0
        assert json.loads(observed[0][1]) == {
            "key": "logs.enabled",
            "value": False,
            "source": "invalid",
        }
