"""Settings shared by the CLI and the MCP server, kept in $SAI_HOME/config.toml.

Each setting resolves from the first layer that sets it: a command-line flag,
then an environment variable, then the config file, then the built-in mainnet
default. `resolve` is pure; `load` reads the environment and the file.

The file lists every key with comments. An unset key stays in the file as a
commented-out line that shows its default. `set_value` and `unset_value` edit
the text with tomlkit, so comments the person adds survive.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import tomllib
from collections.abc import Callable, Collection, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import httpx
import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Comment, Null, Table, Trivia
from websockets.exceptions import InvalidURI
from websockets.uri import parse_uri

from . import keeper, log, margin, paths, stream

FILE_NAME = "config.toml"
LOCK_NAME = ".config.lock"
SOURCES = ("flag", "env", "file", "default", "derived", "invalid")
# Settings that only switch a side feature. A bad value for one of these
# turns that feature off instead of stopping the command.
LENIENT_SECTIONS = frozenset({"logs"})
# Lower rank wins.
_RANK = {"flag": 0, "env": 1, "file": 2, "default": 3}
_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")
_URL_WHITESPACE_OR_CONTROL = re.compile(r"[\s\x00-\x1f\x7f-\x9f]")


class SettingsError(ValueError):
    """A setting, its value, or the config file is invalid."""


class ConfigError(SettingsError):
    """An invalid config file with a diagnostic safe for doctor output."""

    def __init__(self, diagnostic: str, detail: str) -> None:
        super().__init__(detail)
        self.diagnostic = diagnostic


class InvalidSetting(SettingsError):
    """A known setting or section has an invalid value."""


@dataclass(frozen=True)
class Invalid:
    """A value in the config file that failed its check, kept so `resolve`
    can turn the feature off instead of failing. MESSAGE says why."""

    message: str


class UneditableSection(SettingsError):
    """The section is valid TOML that `sai config` will not rewrite."""


@dataclass(frozen=True)
class Setting:
    """One key: where it lives, how to set it, and what it defaults to."""

    key: str
    env: str
    kind: str  # "url", "bool", or "int"
    default: str | bool | int | None
    help: str
    flag: str | None = None
    schemes: tuple[str, ...] = ()

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.key.split(".", 1)[1]


SETTINGS: tuple[Setting, ...] = (
    Setting(
        key="keeper.endpoint",
        env="SAI_ENDPOINT",
        kind="url",
        default=keeper.MAINNET_HTTP,
        flag="--endpoint",
        schemes=("https", "http"),
        help="Sai Keeper GraphQL HTTP endpoint for queries and trading intents.",
    ),
    Setting(
        key="keeper.ws_endpoint",
        env="SAI_WS_ENDPOINT",
        kind="url",
        default=None,
        flag="--ws-endpoint",
        schemes=("wss", "ws"),
        help=(
            "Keeper WebSocket endpoint for balances and live feeds. Unset"
            " derives it from keeper.endpoint (https becomes wss). A value"
            " here is ignored when keeper.endpoint comes from a flag or"
            " environment variable, so the two always point at one Keeper."
        ),
    ),
    Setting(
        key="lcd.endpoint",
        env="SAI_LCD_ENDPOINT",
        kind="url",
        default=margin.MAINNET_LCD,
        flag="--lcd-endpoint",
        schemes=("https", "http"),
        help=(
            "Nibiru REST (LCD) endpoint that margin and partial-close quotes"
            " read exact contract state from."
        ),
    ),
    Setting(
        key="update.auto",
        env="SAI_UPDATE_AUTO",
        kind="bool",
        default=True,
        help=("Install newer sai releases automatically. false means notify only."),
    ),
    Setting(
        key="logs.enabled",
        env="SAI_LOG",
        kind="bool",
        default=True,
        help=(
            "Record commands, MCP tool calls, Keeper requests, and intent"
            " status changes as JSON lines under $SAI_HOME/logs. The session"
            " token is never written. false writes nothing."
        ),
    ),
    Setting(
        key="logs.keep_days",
        env="SAI_LOG_KEEP_DAYS",
        kind="int",
        default=14,
        help=(
            "Days of log files to keep, counting today. Older files are"
            " deleted, at most once a day."
        ),
    ),
)
BY_KEY: Mapping[str, Setting] = MappingProxyType({s.key: s for s in SETTINGS})
KEYS = tuple(BY_KEY)
SECTIONS = frozenset(spec.section for spec in SETTINGS)


@dataclass(frozen=True)
class Settings:
    """Resolved settings. SOURCES maps each key to where its value came from."""

    endpoint: str
    ws_endpoint: str
    lcd_endpoint: str
    update_auto: bool
    sources: Mapping[str, str]
    logs_enabled: bool = True
    logs_keep_days: int = 14

    def value(self, key: str) -> str | bool | int:
        return {
            "keeper.endpoint": self.endpoint,
            "keeper.ws_endpoint": self.ws_endpoint,
            "lcd.endpoint": self.lcd_endpoint,
            "update.auto": self.update_auto,
            "logs.enabled": self.logs_enabled,
            "logs.keep_days": self.logs_keep_days,
        }[setting(key).key]


def setting(key: str) -> Setting:
    """Return the registry entry for KEY, or fail listing the valid keys."""
    try:
        return BY_KEY[key]
    except KeyError:
        raise SettingsError(
            f"Unknown setting {key!r}. Valid keys: {', '.join(KEYS)}"
        ) from None


def parse_value(key: str, raw: str, *, source: str | None = None) -> str | bool | int:
    """Parse RAW, as typed in a flag, variable, or `sai config set`."""
    spec = setting(key)
    text = raw.strip()
    value: str | bool | int = raw
    if spec.kind == "bool":
        if text.lower() in _TRUE:
            value = True
        elif text.lower() in _FALSE:
            value = False
    elif spec.kind == "int" and text.isascii() and text.isdigit():
        try:
            value = int(text)
        except ValueError:
            # Python limits integer string conversion. Reject it like other bad values.
            pass
    return check_value(key, value, source=source)


def check_value(key: str, value: Any, *, source: str | None = None) -> str | bool | int:
    """Return VALUE when it has the right type and form for KEY."""
    spec = setting(key)
    message = f"Invalid value for {key}"
    if source is not None:
        message = f"{source}: {message}"
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise InvalidSetting(f"{message}: expected true or false")
        return value
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise InvalidSetting(f"{message}: expected a whole number, 1 or more")
        return value
    if not isinstance(value, str):
        raise InvalidSetting(f"{message}: expected a URL string")
    if spec.section == "keeper" and "#" in value:
        raise InvalidSetting(f"{message}: remove the URL fragment")
    if _URL_WHITESPACE_OR_CONTROL.search(value):
        raise InvalidSetting(
            f"{message}: URL contains whitespace or control characters"
        )
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        # urlsplit defers port validation until the property is read.
        _ = parts.port
        # Older urlsplit versions ignore text around a bracketed host.
        if "[" in parts.netloc or "]" in parts.netloc:
            host_port = parts.netloc.rpartition("@")[2]
            host, bracket, suffix = host_port.partition("]")
            if (
                not host.startswith("[")
                or not bracket
                or (suffix and not suffix.startswith(":"))
            ):
                hostname = None
        scheme, separator, _ = value.partition("://")
        if (
            separator
            and scheme.lower() == parts.scheme
            and parts.scheme in spec.schemes
            and hostname
            and "\\" not in parts.netloc
        ):
            if parts.scheme in ("ws", "wss"):
                parse_uri(value)
                return value
            if httpx.URL(value).is_absolute_url:
                return value
    except (ValueError, httpx.InvalidURL, InvalidURI):
        pass
    raise InvalidSetting(
        f"{message}: expected a URL starting with {' or '.join(s + '://' for s in spec.schemes)}"
    )


def sai_home(env: Mapping[str, str]) -> Path:
    """The Sai home directory, as core.storage finds it."""
    return paths.sai_home(env)


def config_path(env: Mapping[str, str]) -> Path:
    return sai_home(env) / FILE_NAME


def parse_file(
    text: str, path: Path | str, *, overridden: Collection[str] = ()
) -> tuple[dict[str, Any], list[str]]:
    """Return the known keys set in config TEXT and warnings for unknown ones.

    A file that is not TOML, or a known key with a bad value, is an error
    naming PATH. Unknown keys are only warned about, so an older sai keeps
    working with a file a newer one wrote. A bad `logs.*` value is a warning
    too: it comes back as `Invalid`, and `resolve` turns logging off.
    Skip value validation for keys superseded by flags or environment variables.
    """
    try:
        data = tomllib.loads(text)
    except ValueError as exc:
        raise ConfigError(
            "config.toml is not valid TOML", f"Cannot parse {path}: {exc}"
        ) from None
    values: dict[str, Any] = {}
    warnings: list[str] = []
    for section, table in data.items():
        if not isinstance(table, dict):
            active_keys = [
                spec.key
                for spec in SETTINGS
                if spec.section == section and spec.key not in overridden
            ]
            if section in SECTIONS and not active_keys:
                continue
            if section in LENIENT_SECTIONS:
                # Not worth stopping a trade for: the feature goes off.
                message = f"{section!r} must be a table, such as [{section}]"
                for key in active_keys:
                    values[key] = Invalid(message)
                warnings.append(f"{path}: {message}; logging is off until it is fixed")
                continue
            if section in SECTIONS:
                # A known section that is not a table would otherwise fall back
                # to the default and quietly select another Keeper.
                message = f"{section!r} must be a table, such as [{section}]"
                raise InvalidSetting(f"{path}: {message}")
            warnings.append(f"{path}: ignoring unknown setting {section!r}")
            continue
        for name, value in table.items():
            key = f"{section}.{name}"
            if key not in BY_KEY:
                warnings.append(f"{path}: ignoring unknown setting {key!r}")
                continue
            if key in overridden:
                continue
            try:
                values[key] = check_value(key, value)
            except InvalidSetting as exc:
                if section not in LENIENT_SECTIONS:
                    raise InvalidSetting(f"{path}: {exc}") from None
                values[key] = Invalid(str(exc))
                warnings.append(f"{path}: {exc}; logging is off until it is fixed")
    return values, warnings


def read_file(
    path: Path, *, overridden: Collection[str] = ()
) -> tuple[dict[str, Any], list[str]]:
    """Read the config file at PATH; a missing file sets nothing."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, []
    except UnicodeError:
        message = "config.toml must use UTF-8"
        raise ConfigError(message, f"{path}: {message}") from None
    except OSError as exc:
        raise ConfigError(
            "Could not read config.toml", f"Cannot read {path}: {exc.strerror}"
        ) from None
    return parse_file(text, path, overridden=overridden)


def _override(
    spec: Setting, flags: Mapping[str, str | None], env: Mapping[str, str]
) -> tuple[str, str] | None:
    raw_flag = flags.get(spec.key)
    if raw_flag is not None:
        return raw_flag, "flag"
    raw_env = env.get(spec.env, "")
    if raw_env.strip():
        return raw_env, "env"
    return None


def shadowed_file_keys(
    flag_keys: Iterable[str], env: Mapping[str, str]
) -> frozenset[str]:
    """Config file keys superseded by higher layers."""
    overridden = set(flag_keys)
    overridden.update(spec.key for spec in SETTINGS if env.get(spec.env, "").strip())
    if "keeper.endpoint" in overridden:
        # Even with no explicit WebSocket override, derive it from the HTTP URL.
        overridden.add("keeper.ws_endpoint")
    return frozenset(overridden)


def read_effective_file(
    path: Path, flags: Mapping[str, str | None], env: Mapping[str, str]
) -> tuple[dict[str, Any], list[str]]:
    """Read the config file, skipping values that cannot affect resolution."""
    flag_keys = (key for key, value in flags.items() if value is not None)
    return read_file(path, overridden=shadowed_file_keys(flag_keys, env))


def resolve(
    flags: Mapping[str, str | None],
    env: Mapping[str, str],
    file: Mapping[str, Any],
) -> Settings:
    """Resolve every setting from FLAGS, then ENV, then FILE, then defaults.

    FLAGS maps keys to raw strings; None means not passed. ENV maps variable
    names to raw strings; empty means unset. FILE holds checked values from
    `read_file`. A WebSocket endpoint from a lower layer than keeper.endpoint
    is ignored and derived, so `--endpoint X` never pairs X with the
    configured Keeper's WebSocket.

    A bad `logs.*` value never fails: its source becomes `invalid` and
    logging resolves to off, so a typo in a logging setting cannot stop a
    command or the MCP server from starting. Invalid retention is reported
    even when logging is already off.
    """
    values: dict[str, str | bool | int | None] = {}
    sources: dict[str, str] = {}
    http_key, ws_key = "keeper.endpoint", "keeper.ws_endpoint"
    for spec in SETTINGS:
        override = _override(spec, flags, env)
        source = override[1] if override else "file" if spec.key in file else "default"
        # SETTINGS lists the HTTP endpoint before its dependent WebSocket.
        if spec.key == ws_key and (
            source == "default" or _RANK[source] > _RANK[sources[http_key]]
        ):
            values[ws_key] = stream.derive_ws(str(values[http_key]))
            sources[ws_key] = "derived"
            continue
        try:
            if override is not None:
                origin = (spec.flag or spec.key) if source == "flag" else spec.env
                values[spec.key] = parse_value(spec.key, override[0], source=origin)
            elif source == "file":
                if isinstance(file[spec.key], Invalid):
                    raise InvalidSetting(file[spec.key].message)
                values[spec.key] = check_value(
                    spec.key, file[spec.key], source=FILE_NAME
                )
            else:
                values[spec.key] = spec.default
        except InvalidSetting:
            if spec.section not in LENIENT_SECTIONS:
                raise
            values[spec.key] = spec.default
            source = "invalid"
        sources[spec.key] = source
    logs_valid = "invalid" not in (sources["logs.enabled"], sources["logs.keep_days"])
    return Settings(
        endpoint=str(values[http_key]),
        ws_endpoint=str(values[ws_key]),
        lcd_endpoint=str(values["lcd.endpoint"]),
        update_auto=bool(values["update.auto"]),
        sources=MappingProxyType(sources),
        logs_enabled=bool(values["logs.enabled"]) and logs_valid,
        logs_keep_days=int(values["logs.keep_days"] or 14),
    )


def load(
    flags: Mapping[str, str | None] | None = None,
    env: Mapping[str, str] | None = None,
    warn: Callable[[str], None] | None = None,
) -> Settings:
    """Read the config file named by ENV (default: os.environ) and resolve."""
    values = os.environ if env is None else env
    overrides = flags or {}
    file, warnings = read_effective_file(config_path(values), overrides, values)
    resolved = resolve(overrides, values, file)
    for spec in SETTINGS:
        from_env = values.get(spec.env, "").strip()
        if resolved.sources[spec.key] == "invalid" and from_env:
            warnings.append(
                f"{spec.env}: invalid value for {spec.key}; logging is off"
                " until it is fixed"
            )
    for message in warnings:
        if warn is not None:
            warn(message)
    return resolved


# Redacted output


def public_value(
    spec: Setting, value: str | bool | int | None
) -> str | bool | int | None:
    """Show only the origin for an endpoint setting."""
    if spec.kind == "url" and isinstance(value, str):
        return log.origin(value)
    return value


def public(settings: Settings) -> dict[str, dict[str, Any]]:
    """Every key with its display value, source, variable, and default."""

    return {
        spec.key: {
            "value": public_value(spec, settings.value(spec.key)),
            "source": settings.sources[spec.key],
            "env": spec.env,
            "default": public_value(spec, spec.default),
        }
        for spec in SETTINGS
    }


# Writing the file


def _wrap(text: str, width: int = 78) -> list[str]:
    lines: list[str] = []
    line = "#"
    for word in text.split():
        if len(line) + 1 + len(word) > width and line != "#":
            lines.append(line)
            line = "#"
        line += " " + word
    lines.append(line)
    return lines


def _toml_value(value: str | bool | int) -> str:
    return tomlkit.item(value).as_string()


def _placeholder(spec: Setting) -> str:
    default = spec.default
    if spec.key == "keeper.ws_endpoint":
        default = keeper.MAINNET_WS
    assert default is not None
    return f"# {spec.name} = {_toml_value(default)}"


def template() -> str:
    """The full config file with every key unset and explained."""
    lines = [
        "# Sai CLI and MCP server settings.",
        "#",
        "# `sai config set KEY VALUE` and `sai config unset KEY` edit this file",
        "# and keep its comments. A commented-out key is unset and shows its",
        "# default. Each setting comes from the first of: command-line flag,",
        "# environment variable, this file, built-in mainnet default.",
        "# `sai config list` shows each value and where it came from.",
    ]
    section = None
    for spec in SETTINGS:
        if spec.section != section:
            section = spec.section
            lines += ["", f"[{section}]"]
        else:
            lines.append("")
        lines += _wrap(spec.help)
        where = f"Environment: {spec.env}."
        if spec.flag:
            where = f"Flag: {spec.flag}. {where}"
        lines.append(f"# {where}")
        lines.append(_placeholder(spec))
    return "\n".join(lines) + "\n"


def _document(text: str | None, path: Path | str) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(template() if text is None else text)
    except TOMLKitError as exc:
        raise SettingsError(f"Cannot parse {path}: {exc}") from None


def _is_standard(doc: tomlkit.TOMLDocument, section: str, item: Any) -> bool:
    """True for a `[section]` header table, the form the editor writes."""
    if not isinstance(item, Table) or item.is_super_table():
        return False
    return not any(
        key is not None and key.key == section and key.is_dotted()
        for key, _ in doc.body
    )


def _table(
    doc: tomlkit.TOMLDocument, spec: Setting, path: Path | str, create: bool = True
) -> Table | None:
    """Return the `[section]` table for SPEC, or None when absent and not
    CREATE.

    The reader accepts an inline table (`keeper = { endpoint = "..." }`),
    dotted keys (`keeper.endpoint = "..."`), and a section made only of
    subtables, but rewriting those would drop their comments, so editing
    them is refused and the person converts the section by hand.
    """
    item = doc.get(spec.section)
    if item is None:
        if not create:
            return None
        table = tomlkit.table()
        doc.add(spec.section, table)
        return table
    if _is_standard(doc, spec.section, item):
        return item
    if not isinstance(getattr(item, "unwrap", lambda: None)(), dict):
        raise InvalidSetting(
            f"{path}: {spec.section!r} must be a table, such as [{spec.section}]"
        )
    raise UneditableSection(
        f"{spec.section} in {path} is an inline table, dotted keys, or only"
        f" subtables; rewrite it as a [{spec.section}] table to edit it with"
        " sai config"
    )


def _is_placeholder(item: Any, spec: Setting) -> bool:
    return isinstance(item, Comment) and item.as_string().strip().startswith(
        f"# {spec.name} ="
    )


def _placeholder_note(item: Any, spec: Setting) -> str | None:
    """The person's note after a placeholder that `unset_value` wrote."""
    text = item.as_string().strip()
    prefix = _placeholder(spec)
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix) :].strip()
    return rest if rest.startswith("#") else None


def set_value(
    text: str | None, key: str, raw: str, path: Path | str = FILE_NAME
) -> tuple[str, str | bool | int]:
    """Return config TEXT with KEY set to RAW, and the parsed value.

    TEXT None starts from `template()`. The key takes the place of its
    commented-out line when there is one; other comments are kept.
    """
    value = parse_value(key, raw)
    spec = setting(key)
    doc = _document(text, path)
    table = _table(doc, spec, path)
    assert table is not None
    if spec.name in table:
        table[spec.name] = value
        return doc.as_string(), value
    body = table.value.body
    for index, (name, item) in enumerate(body):
        if name is None and _is_placeholder(item, spec):
            new = tomlkit.item(value)
            note = _placeholder_note(item, spec)
            if note:
                new.trivia.comment_ws = "  "
                new.trivia.comment = note
            table.value._insert_at(index, spec.name, new)
            body[index + 1] = (None, Null())
            break
    else:
        table.add(spec.name, value)
    return doc.as_string(), value


def unset_value(
    text: str | None, key: str, path: Path | str = FILE_NAME
) -> tuple[str, bool]:
    """Return config TEXT with KEY back to its commented-out default, and
    whether KEY was set. An inline comment on the key's line stays on the
    commented-out line."""
    spec = setting(key)
    doc = _document(text, path)
    table = _table(doc, spec, path, create=False)
    if table is None or spec.name not in table:
        return doc.as_string(), False
    body = table.value.body
    index = next(
        i
        for i, (name, _) in enumerate(body)
        if name is not None and name.key == spec.name
    )
    trivia = body[index][1].trivia
    line = _placeholder(spec)
    if trivia.comment:
        line += (trivia.comment_ws or "  ") + trivia.comment
    del table[spec.name]
    comment = Comment(Trivia(comment_ws="", comment=line, trail="\n"))
    body[index] = (None, comment)
    return doc.as_string(), True


def write_file(path: Path, text: str) -> None:
    """Write TEXT to PATH atomically, mode 0600 in a 0700 directory."""
    paths.private_directory(path.parent, profile_root=True)
    # mkstemp creates the file with mode 0600.
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


@contextmanager
def _locked(path: Path) -> Generator[None]:
    """Hold an exclusive lock on PATH's sibling `.config.lock` file, so
    concurrent edits run one after another instead of losing changes."""
    paths.private_directory(path.parent, profile_root=True)
    fd = os.open(path.parent / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            # LK_LOCK retries for about ten seconds, then raises OSError.
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        # Closing the descriptor releases the lock.
        os.close(fd)


def edit_file(path: Path, edit: Callable[[str | None], str | None]) -> None:
    """Apply EDIT to the text at PATH (None when missing) and save it, holding
    the config lock across the read, the edit, and the replace. EDIT returns
    None to leave the file as it is."""
    with _locked(path):
        try:
            text: str | None = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = None
        except OSError as exc:
            raise SettingsError(f"Cannot read {path}: {exc.strerror}") from None
        new = edit(text)
        if new is not None:
            write_file(path, new)
    if new is not None:
        from . import log

        log.invalidate_config()
