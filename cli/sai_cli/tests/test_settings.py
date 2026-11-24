"""Settings: precedence, the WebSocket rule, the config file, and its edits."""

from __future__ import annotations

import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from sai_cli.core import keeper, margin, settings
from sai_cli.core.settings import SettingsError, resolve

FLAG_URL = "https://flag.example/query"
ENV_URL = "https://env.example/query"
FILE_URL = "https://file.example/query"

# For each URL key: the environment variable, a flag value, an environment
# value, a file value, and the default.
URL_CASES = {
    "keeper.endpoint": (
        "SAI_ENDPOINT",
        FLAG_URL,
        ENV_URL,
        FILE_URL,
        keeper.MAINNET_HTTP,
    ),
    "lcd.endpoint": (
        "SAI_LCD_ENDPOINT",
        "https://lcd-flag.example",
        "https://lcd-env.example",
        "https://lcd-file.example",
        margin.MAINNET_LCD,
    ),
}


@pytest.fixture(autouse=True)
def scrubbed_env(tmp_path, monkeypatch):
    for spec in settings.SETTINGS:
        monkeypatch.delenv(spec.env, raising=False)
    monkeypatch.setenv("SAI_HOME", str(tmp_path))


def sourced(result: settings.Settings, key: str) -> tuple[Any, str]:
    return result.value(key), result.sources[key]


@pytest.mark.parametrize("key", sorted(URL_CASES))
def test_precedence_for_each_url_key(key):
    env_name, flag, env, file, default = URL_CASES[key]
    assert sourced(resolve({}, {}, {}), key) == (default, "default")
    assert sourced(resolve({}, {}, {key: file}), key) == (file, "file")
    assert sourced(resolve({}, {env_name: env}, {key: file}), key) == (env, "env")
    both = resolve({key: flag}, {env_name: env}, {key: file})
    assert sourced(both, key) == (flag, "flag")
    # An empty variable or a None flag counts as unset.
    assert sourced(resolve({key: None}, {env_name: " "}, {key: file}), key) == (
        file,
        "file",
    )


def test_precedence_for_update_auto():
    key, env = "update.auto", "SAI_UPDATE_AUTO"
    assert sourced(resolve({}, {}, {}), key) == (True, "default")
    assert sourced(resolve({}, {}, {key: False}), key) == (False, "file")
    assert sourced(resolve({}, {env: "yes"}, {key: False}), key) == (True, "env")
    assert sourced(resolve({key: "off"}, {env: "yes"}, {}), key) == (False, "flag")


@pytest.mark.parametrize(
    ("flags", "env", "file", "expected"),
    [
        # Unset everywhere: derived from the mainnet default.
        ({}, {}, {}, (keeper.MAINNET_WS, "derived")),
        # Same layer as keeper.endpoint, or higher: used.
        ({}, {}, {"keeper.ws_endpoint": "wss://w.file/q"}, ("wss://w.file/q", "file")),
        (
            {},
            {},
            {"keeper.endpoint": FILE_URL, "keeper.ws_endpoint": "wss://w.file/q"},
            ("wss://w.file/q", "file"),
        ),
        (
            {},
            {"SAI_WS_ENDPOINT": "wss://w.env/q"},
            {"keeper.endpoint": FILE_URL},
            ("wss://w.env/q", "env"),
        ),
        (
            {"keeper.endpoint": FLAG_URL, "keeper.ws_endpoint": "wss://w.flag/q"},
            {},
            {},
            ("wss://w.flag/q", "flag"),
        ),
        # Lower layer than keeper.endpoint: ignored and derived.
        (
            {},
            {"SAI_ENDPOINT": ENV_URL},
            {"keeper.ws_endpoint": "wss://w.file/q"},
            ("wss://env.example/query", "derived"),
        ),
        (
            {"keeper.endpoint": "http://127.0.0.1:9/q"},
            {"SAI_WS_ENDPOINT": "wss://w.env/q"},
            {},
            ("ws://127.0.0.1:9/q", "derived"),
        ),
    ],
)
def test_websocket_rule(flags, env, file, expected):
    assert sourced(resolve(flags, env, file), "keeper.ws_endpoint") == expected


def test_bad_values_name_their_origin():
    with pytest.raises(SettingsError, match="SAI_ENDPOINT: Invalid value"):
        resolve({}, {"SAI_ENDPOINT": "wss://x/q"}, {})
    with pytest.raises(SettingsError, match="--lcd-endpoint: Invalid value"):
        resolve({"lcd.endpoint": "lcd.example"}, {}, {})
    with pytest.raises(SettingsError, match="SAI_UPDATE_AUTO.*true or false"):
        resolve({}, {"SAI_UPDATE_AUTO": "maybe"}, {})


def test_unknown_key_lists_valid_keys():
    with pytest.raises(SettingsError, match="Valid keys: keeper.endpoint, "):
        settings.setting("keeper.url")


# The file


def test_missing_file_sets_nothing(tmp_path):
    assert settings.read_file(tmp_path / "config.toml") == ({}, [])


def test_malformed_file_error_names_the_path(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[keeper\nendpoint = 1\n")
    with pytest.raises(SettingsError, match=f"Cannot parse {path}"):
        settings.read_file(path)
    path.write_text('[update]\nauto = "yes"\n')
    with pytest.raises(SettingsError, match=f"{path}: Invalid value for update"):
        settings.read_file(path)


def test_unknown_keys_warn_and_are_ignored(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'theme = "dark"\n[keeper]\nendpoint = "https://k.example/q"\n'
        "retries = 3\n[network]\nname = 'testnet'\n"
    )
    values, warnings = settings.read_file(path)
    assert values == {"keeper.endpoint": "https://k.example/q"}
    assert warnings == [
        f"{path}: ignoring unknown setting 'theme'",
        f"{path}: ignoring unknown setting 'keeper.retries'",
        f"{path}: ignoring unknown setting 'network.name'",
    ]
    seen: list[str] = []
    loaded = settings.load(warn=seen.append)
    assert loaded.endpoint == "https://k.example/q"
    assert seen == warnings


def test_template_lists_every_key_unset_and_explained():
    text = settings.template()
    assert tomllib.loads(text) == {"keeper": {}, "lcd": {}, "update": {}, "logs": {}}
    for spec in settings.SETTINGS:
        assert f"# {spec.name} = " in text
        assert spec.env in text
    assert all(len(line) <= 80 for line in text.splitlines())


def test_set_and_unset_round_trip_from_no_file():
    text, value = settings.set_value(None, "keeper.endpoint", " https://k.example/q ")
    assert value == "https://k.example/q"
    assert tomllib.loads(text)["keeper"] == {"endpoint": "https://k.example/q"}
    # The key takes the place of its commented-out line.
    assert '# endpoint = "https://sai-keeper' not in text
    assert "Environment: SAI_ENDPOINT." in text
    text, value = settings.set_value(text, "update.auto", "false")
    assert value is False
    assert tomllib.loads(text)["update"] == {"auto": False}
    text, removed = settings.unset_value(text, "keeper.endpoint")
    assert removed
    text, removed = settings.unset_value(text, "update.auto")
    assert removed
    assert text == settings.template()
    assert settings.unset_value(text, "update.auto") == (text, False)


def test_set_and_unset_keep_the_persons_comments():
    original = (
        "# my notes at the top\n"
        "[keeper]\n"
        "# staging Keeper, ask Ana before changing\n"
        'endpoint = "https://old.example/q"  # pinned\n'
        "# trailing note\n"
        "\n"
        "[update]\n"
        "auto = true\n"
    )
    text, _ = settings.set_value(original, "keeper.endpoint", "https://new.example/q")
    assert text == original.replace("old.example", "new.example")
    text, _ = settings.set_value(text, "lcd.endpoint", "https://lcd.example")
    assert tomllib.loads(text)["lcd"] == {"endpoint": "https://lcd.example"}
    text, _ = settings.unset_value(text, "keeper.endpoint")
    for kept in ("# my notes", "# staging Keeper", "# trailing note"):
        assert kept in text
    # The key's inline note stays on its commented-out line.
    assert '# endpoint = "https://sai-keeper.nibiru.fi/query"  # pinned\n' in text
    assert "endpoint" not in tomllib.loads(text)["keeper"]
    # Setting it again takes the note back.
    text, _ = settings.set_value(text, "keeper.endpoint", "https://new.example/q")
    assert 'endpoint = "https://new.example/q"  # pinned\n' in text
    assert "# endpoint =" not in text


NON_STANDARD = {
    "inline": (
        "# notes\n"
        'keeper = { endpoint = "https://old.example/q" }  # staging\n'
        "[update]\nauto = false\n"
    ),
    "dotted": (
        'keeper.endpoint = "https://old.example/q"  # staging\n'
        'keeper.ws_endpoint = "wss://old.example/q"  # rotates weekly\n'
        "[update]\nauto = false\n"
    ),
    "subtable-only": "[update]\nauto = false\n[keeper.sub]  # mine\nx = 1\n",
}


@pytest.mark.parametrize("form", sorted(NON_STANDARD))
def test_non_standard_sections_are_read_but_not_edited(form):
    original = NON_STANDARD[form]
    values, _ = settings.parse_file(original, "x")
    assert values["update.auto"] is False
    for edit in (
        lambda: settings.set_value(
            original, "keeper.endpoint", "https://n.example", "x"
        ),
        lambda: settings.unset_value(original, "keeper.endpoint", "x"),
        lambda: settings.unset_value(original, "keeper.ws_endpoint", "x"),
    ):
        with pytest.raises(
            settings.UneditableSection,
            match=r"keeper in x is an inline table, dotted keys, or only subtables;"
            r" rewrite it as a \[keeper\] table",
        ):
            edit()
    # Other sections stay editable.
    text, _ = settings.set_value(original, "update.auto", "true", "x")
    assert "# staging" in text or form == "subtable-only"


def test_known_section_that_is_not_a_table_is_an_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('keeper = "https://staging.example/query"\n')
    with pytest.raises(SettingsError, match=f"{path}: 'keeper' must be a table"):
        settings.read_file(path)
    with pytest.raises(SettingsError, match="'keeper' must be a table"):
        settings.load()
    for edit in (
        lambda t: settings.set_value(t, "keeper.endpoint", "https://k.example"),
        lambda t: settings.unset_value(t, "keeper.endpoint"),
    ):
        with pytest.raises(SettingsError, match="'keeper' must be a table"):
            edit(path.read_text())
    # An unknown section that is not a table is still only a warning.
    path.write_text('theme = "dark"\n')
    assert settings.read_file(path) == (
        {},
        [f"{path}: ignoring unknown setting 'theme'"],
    )


def test_set_refuses_bad_values_and_unknown_keys():
    with pytest.raises(SettingsError, match="expected a URL starting with wss"):
        settings.set_value(None, "keeper.ws_endpoint", "https://k.example/q")
    with pytest.raises(SettingsError, match="Unknown setting"):
        settings.set_value(None, "keeper.timeout", "3")
    with pytest.raises(SettingsError, match="Cannot parse x.toml"):
        settings.set_value("[keeper", "update.auto", "true", "x.toml")


def test_write_file_is_private_and_atomic(tmp_path):
    path = tmp_path / "home" / "config.toml"
    settings.write_file(path, "a = 1\n")
    settings.write_file(path, "a = 2\n")
    assert path.read_text() == "a = 2\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]


def test_concurrent_edits_keep_every_change(tmp_path):
    """Each edit holds the lock across read, edit, and replace, so parallel
    `sai config set` processes never drop one another's change."""
    path = tmp_path / "config.toml"
    code = (
        "import sys; from pathlib import Path; from sai_cli.core import settings;"
        " key, value = sys.argv[1], sys.argv[2];"
        " settings.edit_file(Path(sys.argv[3]),"
        " lambda t: (__import__('time').sleep(0.3),"
        " settings.set_value(t, key, value)[0])[1])"
    )
    jobs = [
        ("keeper.endpoint", "https://k.example/q"),
        ("lcd.endpoint", "https://lcd.example"),
        ("keeper.ws_endpoint", "wss://w.example/q"),
        ("update.auto", "false"),
    ]
    procs = [
        subprocess.Popen([sys.executable, "-c", code, key, value, str(path)])
        for key, value in jobs
    ]
    assert [proc.wait(timeout=60) for proc in procs] == [0] * len(jobs)
    values, _ = settings.read_file(path)
    assert values == {key: settings.parse_value(key, value) for key, value in jobs}
    assert stat.S_IMODE((tmp_path / settings.LOCK_NAME).stat().st_mode) == 0o600


def test_edit_file_creates_the_full_file(tmp_path):
    path = Path(tmp_path) / "config.toml"
    settings.edit_file(
        path, lambda text: settings.set_value(text, "update.auto", "0")[0]
    )
    text = path.read_text()
    assert text.startswith("# Sai CLI and MCP server settings.")
    assert settings.read_file(path) == ({"update.auto": False}, [])


def test_public_view_redacts_urls():
    result = resolve(
        {"keeper.endpoint": "https://u:pw@k.example/q?key=s"},
        {"SAI_LCD_ENDPOINT": "https://lcd.example/?apikey=s"},
        {},
    )
    shown = settings.public(result)
    assert list(shown) == list(settings.KEYS)
    assert shown["keeper.endpoint"] == {
        "value": "https://k.example",
        "source": "flag",
        "env": "SAI_ENDPOINT",
        "default": "https://sai-keeper.nibiru.fi",
    }
    assert shown["keeper.ws_endpoint"]["value"] == "wss://k.example"
    assert shown["keeper.ws_endpoint"]["default"] is None
    assert shown["lcd.endpoint"]["value"] == "https://lcd.example/"
    assert shown["update.auto"] == {
        "value": True,
        "source": "default",
        "env": "SAI_UPDATE_AUTO",
        "default": True,
    }
