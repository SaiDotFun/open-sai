"""`sai doctor`: check the setup, fix what is safe to fix, and say how to
fix the rest.

Each check returns pass, warn, fail, skipped, or fixed. A fixed check lists
what it changed in `changes`. Doctor fixes only what it can from inspection:
it creates private Sai directories, tightens modes on dedicated profiles and
managed files, repairs stale MCP registrations, migrates the intent ledger,
and reconciles unknown intents for the signed-in account with Keeper
(a read; no trade is placed or cancelled). It never deletes or rewrites the
session. `--no-fix` reports without changing anything.

Rendered diagnostics redact session tokens and show endpoint origins.

To add a check, write a function that takes a `Context` and returns a
`Check` or a list of them (a check that repeats, such as one per MCP client),
then list it in LOCAL_CHECKS or NETWORK_CHECKS. Network checks run in
parallel and `--offline` skips them.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, BinaryIO

from websockets.exceptions import ConnectionClosed, InvalidStatus

from . import mcp_install
from .core import (
    keeper,
    log,
    margin,
    paths,
    reconcile,
    settings,
    storage,
    stream,
    update,
    updater,
)
from .core.settings import Settings
from .mcp_server import info, tools

SCHEMA_VERSION = 1
NETWORK_TIMEOUT = 5.0
# Wall-clock limit for all network checks together, and separately for
# reconciling unknown intents.
NETWORK_DEADLINE = NETWORK_TIMEOUT + 2
STATUSES = ("pass", "fixed", "warn", "fail", "skipped")
SIGN_IN = "sai auth login NAME, or sai auth import"
REINSTALL = update.INSTALL_COMMAND
FILE_MODE = 0o600
CHAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,49}")


@dataclass
class Check:
    id: str
    status: str
    summary: str
    fix: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    changes: list[str] = field(default_factory=list)
    invalid_setting: bool = False
    _lcd_chain_id: str | None = field(default=None, repr=False)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "summary": self.summary,
            "fix": self.fix,
            "changes": self.changes,
            "details": self.details,
            "durationMs": self.duration_ms,
        }


@dataclass(frozen=True)
class Context:
    """What the checks read: endpoints, the session state, the clock, and
    whether they may fix things or reach the network."""

    config: Settings
    session: dict[str, Any]
    now: datetime
    path_env: str = ""
    fix: bool = True
    offline: bool = False
    # How this copy was installed; None detects it from the running prefix.
    install: update.Install | None = None
    config_available: bool = True


CheckFn = Callable[[Context], "Check | Sequence[Check]"]


def check_config(
    flags: dict[str, str | None],
) -> tuple[Check, Settings | None]:
    """Validate the settings that apply to this invocation."""
    path = settings.config_path(os.environ)
    file_fix = "Edit config.toml in the Sai home directory and rerun `sai doctor`"
    fix = file_fix
    try:
        values, warnings = settings.read_effective_file(path, flags, os.environ)
        fix = "Check the settings listed by `sai config --help`"
        config = settings.resolve(flags, os.environ, values)
    except settings.ConfigError as exc:
        return Check("config", "fail", exc.diagnostic, fix), None
    except settings.InvalidSetting as exc:
        return Check(
            "config",
            "fail",
            str(exc),
            fix,
            invalid_setting=True,
        ), None
    invalid = [
        key for key, value in values.items() if isinstance(value, settings.Invalid)
    ]
    if invalid:
        return Check(
            "config",
            "fail",
            f"Invalid settings in {path}: {', '.join(invalid)}",
            file_fix,
            invalid_setting=True,
        ), config
    invalid_env = [
        f"{spec.env} ({spec.key})"
        for spec in settings.SETTINGS
        if config.sources[spec.key] == "invalid"
        and os.environ.get(spec.env, "").strip()
    ]
    if invalid_env:
        return Check(
            "config",
            "warn",
            f"Invalid environment settings: {', '.join(invalid_env)}",
            "Correct or unset the listed environment variables",
        ), config
    if warnings:
        return Check(
            "config", "warn", "config.toml contains unknown settings", file_fix
        ), config
    return Check("config", "pass", "Config file settings are valid"), config


def check_mcp(ctx: Context) -> list[Check]:
    home = Path.home()
    install = ctx.install or update.install_kind()
    target = None
    if install.official:
        launcher = updater.registered_launcher(Path(install.prefix))
        if launcher is not None and shutil.which(str(launcher)):
            target = mcp_install.launcher_target(str(launcher), os.environ)
    else:
        try:
            target, _ = mcp_install.server_target(
                None, os.environ, home, mcp_install.running_prefix()
            )
        except mcp_install.InstallError:
            pass
    return [
        Check(
            f"mcp.{result.client}",
            result.status,
            result.summary,
            result.fix,
            changes=list(result.changes),
        )
        for result in mcp_install.check_registrations(
            target, fix=ctx.fix, cwd=Path.cwd(), compare_profile=True
        )
    ]


def log_entry(check: Check) -> dict[str, Any]:
    """CHECK for the log: a passing check needs no summary."""
    entry: dict[str, Any] = {
        "id": check.id,
        "status": check.status,
        "durationMs": check.duration_ms,
    }
    if check.status not in ("pass", "skipped"):
        entry["summary"] = check.summary
    if check.changes:
        entry["changes"] = check.changes
    return entry


def short(addr: str) -> str:
    return f"{addr[:6]}…" if len(addr) > 6 else addr


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


# Local checks


def sai_on_path(path_env: str) -> list[str]:
    """Return each distinct `sai` executable on PATH_ENV, resolved."""
    names = ("sai.exe", "sai") if os.name == "nt" else ("sai",)
    found: list[str] = []
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                resolved = str(candidate.resolve())
                if resolved not in found:
                    found.append(resolved)
    return found


def check_install(ctx: Context) -> Check:
    try:
        version = metadata.version(info.PACKAGE)
    except metadata.PackageNotFoundError:
        return Check(
            "install",
            "fail",
            "Package metadata for sai-cli is missing",
            f"Reinstall: {REINSTALL}",
        )
    provenance = info.provenance()
    installs = sai_on_path(ctx.path_env)
    details = {"version": version, "provenance": provenance, "onPath": installs}
    if len(installs) > 1:
        return Check(
            "install",
            "warn",
            f"{len(installs)} sai installs on PATH; {installs[0]} runs",
            "Keep one; `which -a sai` lists them",
            details,
        )
    install = ctx.install or update.install_kind()
    details["kind"] = install.kind
    if install.kind == update.UVX:
        return Check(
            "install",
            "warn",
            f"sai {version} runs from a uvx cache, which has no stable path"
            " and does not update",
            f"Install it: {REINSTALL}",
            details,
        )
    source = provenance["kind"] == "source-checkout"
    if install.kind == update.CHECKOUT or (source and install.official):
        at = f" at {provenance['path']}" if source else ""
        return Check(
            "install",
            "warn",
            f"sai {version} runs from a source checkout{at}",
            f"For a regular install: {REINSTALL}",
            details,
        )
    if not install.official:
        return Check(
            "install",
            "warn",
            f"sai {version} is not a uv tool install from {update.REPO};"
            " `sai update` does not update it",
            f"Switch to one: {REINSTALL}",
            details,
        )
    return Check("install", "pass", f"sai {version} installed", None, details)


def check_version(ctx: Context) -> Check:
    """Compare the installed version with the last update check, and report
    an update that failed. It reads $SAI_HOME/update.json; no network."""
    installed = info.version()
    state = updater.read_state()
    latest = state.get("latest")
    result = state.get("result")
    details = {
        "installed": installed,
        "latest": update.tag_version(latest) if isinstance(latest, str) else None,
        "checkedAt": state.get("checkedAt"),
        "lastUpdate": result if isinstance(result, dict) else None,
    }
    install = ctx.install or update.install_kind()
    how = "`sai update`" if install.official else REINSTALL
    behind = isinstance(latest, str) and update.is_newer(latest, installed)
    if isinstance(result, dict) and result.get("status") == updater.FAILED:
        target = result.get("to")
        still = isinstance(target, str) and update.is_newer(
            update.tag_for(target), installed
        )
        if still:
            return Check(
                "version",
                "warn",
                f"The last update failed: {result.get('error')}",
                f"Retry: {how}",
                details,
            )
    if behind:
        return Check(
            "version",
            "warn",
            f"sai {installed} is behind the latest release {details['latest']}",
            f"Update: {how}",
            details,
        )
    if details["latest"] is None:
        return Check(
            "version",
            "pass",
            f"sai {installed}; no release check is recorded yet",
            None,
            details,
        )
    return Check(
        "version",
        "pass",
        f"sai {installed} is up to date with the latest release",
        None,
        details,
    )


# Files sai writes under $SAI_HOME/trader; doctor changes no others.
MANAGED = (
    "sessions.json",
    "session.json",
    "intents.sqlite3",
    "intents.sqlite3-wal",
    "intents.sqlite3-shm",
    "intents.sqlite3-journal",
    "history",
)
TRADER_DATA = ("intents.sqlite3", "sessions.json", "session.json")
PROFILE_FILES = {
    "": (
        settings.FILE_NAME,
        settings.LOCK_NAME,
        "update.json",
        "update.lock",
        "update-cache.lock",
    ),
    "trader": (*MANAGED, "sessions.lock"),
    "logs": (log.PRUNE_MARKER, log.ENDPOINT_KEY, log.ENDPOINT_KEY_LOCK),
}
PROFILE_DIRS = {"": ("trader", "logs", "cache"), "cache": ("wheels",)}
PROFILE_SHARED_MODE = stat.S_ISGID | stat.S_ISVTX | 0o022
SYMLINK_FIX = "Replace the symlink with a real file or directory by hand"
SAI_HOME_MODE_FIX = (
    "This Sai home may be shared. Stop all Sai CLI and MCP processes before moving"
    " the profile to a dedicated directory. Keep the trader data, including the intent"
    " ledger. Set SAI_HOME to the new directory for the CLI and in every MCP client"
    " configuration before restarting clients or resuming trading."
)


def symlinks(names: Sequence[str] = MANAGED) -> list[Path]:
    """Return symlinks in the path to the trader directory or its named files.

    Doctor never writes through one: a chmod, migration, or status update
    would land wherever the link points.
    """
    root = storage.home()
    for directory in (root.parent, root):
        if directory.is_symlink():
            return [directory]
    return [root / name for name in names if (root / name).is_symlink()]


def loose_modes(paths: Sequence[Path]) -> list[tuple[Path, int, int]]:
    """Return each of PATHS that group or others can access, with its mode
    and that mode without group and other bits."""
    if os.name == "nt":
        return []
    loose: list[tuple[Path, int, int]] = []
    for path in paths:
        try:
            mode = stat.S_IMODE(path.lstat().st_mode)
        except OSError:
            continue
        if mode & 0o077:
            loose.append((path, mode, mode & ~0o077))
    return loose


def tighten(
    root: Path, names: Sequence[str], *, apply: bool = True
) -> tuple[list[str], list[str], bool]:
    """Apply or plan mode repairs on ROOT and its managed files.

    Every chmod goes through a descriptor opened with O_NOFOLLOW, and the
    files open relative to the directory's descriptor, so a path swapped for
    a symlink after the checks fails to open instead of redirecting the
    chmod. Return changes or safe commands, paths needing manual attention,
    and whether the parent is eligible for a mode repair.
    """
    changes: list[str] = []
    failed: list[str] = []
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    except OSError:
        return changes, [f"Could not open {root} safely; inspect it by hand"], False
    try:
        eligible = recognized_profile(root.parent, child=(root.name, os.fstat(dir_fd)))
        for name in (None, *names):
            path = root if name is None else root / name
            try:
                fd = (
                    dir_fd
                    if name is None
                    else os.open(
                        name, os.O_RDONLY | os.O_NONBLOCK | nofollow, dir_fd=dir_fd
                    )
                )
            except FileNotFoundError:
                continue
            except OSError:
                failed.append(f"Could not open {path} safely; inspect it by hand")
                continue
            try:
                info_ = os.fstat(fd)
                if hasattr(os, "getuid") and info_.st_uid != os.getuid():
                    failed.append(f"{path} belongs to another user; fix by hand")
                    if name is None:
                        break
                    continue
                if name is not None and info_.st_nlink > 1:
                    failed.append(f"{path} has other hard links; fix by hand")
                    continue
                if name is not None and not stat.S_ISREG(info_.st_mode):
                    failed.append(f"{path} is not a regular file; fix by hand")
                    continue
                mode = stat.S_IMODE(info_.st_mode)
                if eligible and mode & 0o077:
                    if apply:
                        os.fchmod(fd, mode & ~0o077)
                        changes.append(f"chmod {path} {mode:o} -> {mode & ~0o077:o}")
                    else:
                        changes.append(f"chmod go-rwx {shlex.quote(str(path))}")
            except OSError:
                failed.append(f"Could not repair {path} safely; inspect it by hand")
            finally:
                if fd != dir_fd:
                    os.close(fd)
    finally:
        os.close(dir_fd)
    return changes, failed, eligible


@contextmanager
def open_regular(path: str | Path, *, dir_fd: int | None = None) -> Generator[BinaryIO]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags, dir_fd=dir_fd), "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise OSError("Expected a regular file")
        yield source


def trader_file_is_evidence(fd: int, name: str, info: os.stat_result) -> bool:
    if not info.st_size or info.st_nlink != 1:
        return False
    try:
        if name == "intents.sqlite3":
            with ledger_snapshot(Path(name), dir_fd=fd) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if not matches_ledger_schema(connection, version):
                    return False
            return os.path.samestat(
                info, os.stat(name, dir_fd=fd, follow_symlinks=False)
            )
        with open_regular(name, dir_fd=fd) as source:
            return os.path.samestat(info, os.fstat(source.fileno())) and (
                storage.is_session_envelope(name, json.load(source))
            )
    except (LedgerChanged, OSError, RecursionError, ValueError, sqlite3.Error):
        return False


def is_sai_log(fd: int, name: str, info: os.stat_result, surface: str) -> bool:
    if info.st_nlink != 1:
        return False
    try:
        with open_regular(name, dir_fd=fd) as source:
            if not os.path.samestat(info, os.fstat(source.fileno())):
                return False
            line = source.readline(log.MAX_LINE + 1)
        if not line.endswith(b"\n"):
            return False
        row = json.loads(line)
        return (
            isinstance(row, dict)
            # Sai v0.1.0 wrote the same common fields without a schema marker.
            and row.get("schema", log.SCHEMA_VERSION) == log.SCHEMA_VERSION
            and row.get("surface") == surface
            and all(key in row for key in log.COMMON if key != "schema")
        )
    except (OSError, RecursionError, ValueError):
        return False


def profile_has_data(
    fd: int, owner: int, directory: str = "", *, allow_loose_trader: bool = False
) -> bool | None:
    """Check for Sai data. Return None if the layout contains an unfamiliar entry."""
    has_data = False
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if info.st_uid != owner:
            return None
        if name in PROFILE_DIRS.get(directory, ()) and stat.S_ISDIR(info.st_mode):
            mode = info.st_mode
            if allow_loose_trader and not directory and name == "trader":
                # check_home clears these bits before applying a root repair.
                mode &= ~0o077
            if mode & PROFILE_SHARED_MODE:
                return None
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            try:
                relative = f"{directory}/{name}" if directory else name
                if not os.path.samestat(info, os.fstat(child)):
                    return None
                child_has_data = profile_has_data(child, owner, relative)
                if child_has_data is None:
                    return None
                has_data |= child_has_data
            finally:
                os.close(child)
        elif not stat.S_ISREG(info.st_mode) or not (
            name in PROFILE_FILES.get(directory, ())
            or (
                directory == "logs"
                and (parsed := log.parse_name(name)) is not None
                and is_sai_log(fd, name, info, parsed[1])
            )
            or (directory == "cache/wheels" and update.is_wheel_name(name))
        ):
            return None
        elif directory == "trader" and name in TRADER_DATA:
            if not trader_file_is_evidence(fd, name, info):
                return None
            has_data = True
    return has_data


def dedicated_profile(
    fd: int, path: Path, *, allow_loose_trader: bool
) -> os.stat_result | None:
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & PROFILE_SHARED_MODE:
        return None
    if (
        profile_has_data(fd, info.st_uid, allow_loose_trader=allow_loose_trader)
        and os.fstat(fd).st_mode == info.st_mode
        and os.path.samestat(info, path.lstat())
    ):
        return info
    return None


def recognized_profile(
    path: Path, *, child: tuple[str, os.stat_result] | None = None
) -> bool:
    if os.name == "nt":
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if dedicated_profile(fd, path, allow_loose_trader=True) is None:
                return False
            if child is None:
                return True
            name, opened = child
            return os.path.samestat(
                opened, os.stat(name, dir_fd=fd, follow_symlinks=False)
            )
        finally:
            os.close(fd)
    except OSError:
        return False


def can_log_to_profile() -> bool:
    """Avoid the writer's automatic chmod in an existing, ambiguous logs directory."""
    if os.name == "nt":
        return True
    # A preceding command may still be creating its log or endpoint key.
    log.flush(1.0)
    root = log.directory()
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not info.st_mode & 0o077:
            # Existing private Sai logs still accept diagnostics while signed out.
            paths = [path for _, _, path in log.log_files(root)]
            if (
                info.st_uid == os.getuid()
                and not loose_modes(paths)
                and profile_has_data(fd, info.st_uid, "logs") is not None
                and os.path.samestat(info, root.lstat())
            ):
                return True
        return recognized_profile(root.parent, child=(root.name, info))
    except OSError:
        return False
    finally:
        os.close(fd)


def repair_profile_root(path: Path, *, apply: bool) -> str | None:
    """Report or apply the mode repair for a dedicated, user-owned profile."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = dedicated_profile(fd, path, allow_loose_trader=not apply)
            if info is None or not info.st_mode & 0o077:
                return None
            mode = stat.S_IMODE(info.st_mode)
            if not apply:
                return f"chmod go-rwx {shlex.quote(str(path))}"
            private_mode = mode & ~0o077
            os.fchmod(fd, private_mode)
        finally:
            os.close(fd)
    except OSError:
        return None
    return f"chmod {path} {mode:o} -> {private_mode:o}"


def check_home(ctx: Context) -> Check:
    """Check the modes on `$SAI_HOME`, `trader`, and its managed files."""
    root = storage.home()
    sai_home = root.parent
    details: dict[str, Any] = {"path": str(root), "saiHome": str(sai_home)}
    links = symlinks()
    home_loose = [] if sai_home in links else loose_modes([sai_home])
    trader_loose = (
        [] if links else loose_modes([root, *(root / name for name in MANAGED)])
    )
    loose = home_loose + trader_loose
    details["looseModes"] = [
        {"path": str(path), "mode": f"{mode:o}"} for path, mode, _ in loose
    ]
    home_fix = SAI_HOME_MODE_FIX if home_loose else ""
    if links:
        return Check(
            "home",
            "fail",
            f"{', '.join(map(str, links))}: profile path is a symlink",
            f"{SYMLINK_FIX}. {home_fix}" if home_fix else SYMLINK_FIX,
            {**details, "symlinks": [str(path) for path in links]},
        )
    trader_exists = root.is_dir()
    changes: list[str] = []
    failed: list[str] = []
    eligible = True
    if trader_exists and trader_loose:
        changes, failed, eligible = tighten(root, MANAGED, apply=ctx.fix)
        if not eligible and not failed:
            home_fix = SAI_HOME_MODE_FIX
    if home_loose and eligible and not failed:
        change = repair_profile_root(sai_home, apply=ctx.fix)
        if change is not None:
            if ctx.fix:
                changes.append(change)
            else:
                changes.insert(0, change)
            home_fix = ""
    if not ctx.fix and loose:
        return Check(
            "home",
            "fail" if failed else "warn",
            f"Could not make {plural(len(failed), 'path')} private"
            if failed
            else f"{plural(len(loose), 'path')} in {sai_home} accessible by other users",
            f"{home_fix} {'; '.join([*failed, *changes])}".strip(),
            details,
        )
    if not trader_exists:
        if not ctx.fix:
            return Check(
                "home",
                "pass",
                f"{root} does not exist yet; sai creates it on first use",
                None,
                details,
            )
        home_exists = sai_home.is_dir()
        paths.private_directory(sai_home, profile_root=True)
        paths.private_directory(root, only_new=True)
        if not home_exists:
            changes.append(f"created {sai_home} with mode {paths.PRIVATE_MODE:o}")
        changes.append(f"created {root} with mode {paths.PRIVATE_MODE:o}")
    if failed:
        return Check(
            "home",
            "fail",
            f"Could not make {plural(len(failed), 'path')} private",
            f"{home_fix} {'; '.join(failed)}".strip(),
            details,
            changes=changes,
        )
    if home_fix:
        return Check(
            "home",
            "warn",
            f"{sai_home if home_loose else root} is accessible by other users",
            home_fix,
            details,
            changes=changes,
        )
    if not changes:
        return Check(
            "home", "pass", f"{sai_home} and {root} are private", None, details
        )
    return Check(
        "home",
        "fixed",
        f"Made {plural(len(changes), 'path')} in {sai_home} private",
        None,
        details,
        changes=changes,
    )


def check_logs(ctx: Context) -> Check:
    """Only the owner can read `$SAI_HOME/logs` and the log files in it."""
    log.flush(1.0)
    root = log.directory()
    if root.parent.is_symlink():
        return Check(
            "logs",
            "fail",
            f"{root.parent}: Sai home is a symlink",
            SYMLINK_FIX,
            {"path": str(root), "symlinks": [str(root.parent)]},
        )
    on, keep_days = log.config()
    names = [path.name for _day, _surface, path in log.log_files(root)]
    names += [log.PRUNE_MARKER] if (root / log.PRUNE_MARKER).exists() else []
    details: dict[str, Any] = {
        "path": str(root),
        "enabled": on,
        "keepDays": keep_days,
        "files": len(names) - (log.PRUNE_MARKER in names),
    }
    links = [path for path in [root, *(root / n for n in names)] if path.is_symlink()]
    if links:
        return Check(
            "logs",
            "fail",
            f"{', '.join(map(str, links))}: logs path is a symlink; doctor"
            " changes nothing there",
            SYMLINK_FIX,
            {**details, "symlinks": [str(path) for path in links]},
        )
    blocked = log.refused_files(root)
    if blocked:
        listed = ", ".join(f"{path} ({reason})" for path, reason in blocked)
        return Check(
            "logs",
            "fail",
            f"sai will not write to {listed}",
            "Move or remove: " + " ".join(str(path) for path, _ in blocked),
            {**details, "refused": [str(path) for path, _ in blocked]},
        )
    off = "" if on else "; logging is off (logs.enabled)"
    if root.exists() and not root.is_dir():
        return Check(
            "logs",
            "fail",
            f"{root} is not a directory, so sai cannot write logs",
            f"Move or remove {root}; sai creates the directory on its next run",
            details,
        )
    if not root.is_dir():
        return Check("logs", "pass", f"No logs yet at {root}{off}", None, details)
    loose = loose_modes([root, *(root / name for name in names)])
    details["looseModes"] = [
        {"path": str(path), "mode": f"{mode:o}"} for path, mode, _ in loose
    ]
    private = Check("logs", "pass", f"{root} is private{off}", None, details)
    if not loose:
        return private
    changes, failed, eligible = tighten(root, names, apply=ctx.fix)
    if not failed and (not eligible or not ctx.fix):
        return Check(
            "logs",
            "warn",
            f"{plural(len(loose), 'path')} under {root} readable by other users",
            "; ".join(changes) if eligible else SAI_HOME_MODE_FIX,
            details,
        )
    if failed:
        return Check(
            "logs",
            "fail",
            f"Could not make {plural(len(failed), 'path')} private",
            "; ".join([*failed, *changes] if not ctx.fix else failed),
            details,
            changes=changes if ctx.fix else [],
        )
    if not changes:
        return private
    return Check(
        "logs",
        "fixed",
        f"Made {plural(len(changes), 'path')} under {root} private",
        None,
        details,
        changes=changes,
    )


def check_session(ctx: Context) -> Check:
    state = ctx.session
    if state.get("error"):
        return Check(
            "session",
            "fail",
            str(state["error"]),
            SIGN_IN,
            {"signedIn": False},
        )
    if not state.get("signedIn"):
        return Check(
            "session",
            "warn",
            "Signed out; MCP trading tools are unavailable",
            SIGN_IN,
            {"signedIn": False},
        )
    addr = state["addr"]
    guidance = state.get("renewalGuidance")
    details = {
        "addr": addr,
        "expiresAt": state["expiresAt"],
        "endpoint": state["endpoint"],
        "endpointMatches": state["endpointMatches"],
    }
    if state["expired"]:
        summary = f"Session for {short(addr)} expired at {state['expiresAt']}"
        return Check(
            "session",
            "fail",
            f"{summary}. {guidance}" if guidance else summary,
            state.get("renewalCommand", None if guidance else SIGN_IN),
            details,
        )
    if not state["endpointMatches"]:
        login = storage.keeper_command_hint(
            ctx.config.endpoint, "auth", "login", "NAME"
        )
        stored_origin = state["endpoint"]
        if stored_origin is None:
            return Check(
                "session",
                "fail",
                f"Session for {short(addr)} has no stored Keeper endpoint",
                login,
                details,
            )
        return Check(
            "session",
            "fail",
            f"Session for {short(addr)} belongs to a different Keeper endpoint."
            f" Stored origin {stored_origin}, configured origin"
            f" {log.origin(ctx.config.endpoint)}",
            f"{login}, or restore the Keeper URL used to sign in",
            details,
        )
    if warning := state.get("expiryWarning"):
        return Check(
            "session",
            "warn",
            f"{warning} {guidance}" if guidance else warning,
            state.get("renewalCommand"),
            details,
        )
    return Check(
        "session",
        "pass",
        f"Signed in as {short(addr)} until {state['expiresAt']}",
        None,
        details,
    )


def ledger_path() -> Path:
    return storage.home() / "intents.sqlite3"


LEDGER_JOURNALS = ("-wal", "-journal")


SNAPSHOT_ATTEMPTS = 3


class LedgerChanged(Exception):
    """Another process kept writing the ledger while doctor copied it."""


def ledger_fingerprint(
    path: Path, *, dir_fd: int | None = None
) -> list[tuple[int, int, int] | None]:
    """Size, mtime, and inode of the ledger and each journal, or None."""
    marks: list[tuple[int, int, int] | None] = []
    for suffix in ("", *LEDGER_JOURNALS):
        try:
            info_ = os.stat(
                path.with_name(path.name + suffix), dir_fd=dir_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            marks.append(None)
            continue
        marks.append((info_.st_size, info_.st_mtime_ns, info_.st_ino))
    return marks


@contextmanager
def ledger_snapshot(
    path: Path, *, dir_fd: int | None = None
) -> Generator[sqlite3.Connection]:
    """Read a private copy of the ledger at PATH with its WAL or rollback
    journal.

    Opening the ledger in place can write even without a change: SQLite
    creates `-wal` and `-shm` files for a WAL-mode database and recovers a
    hot rollback journal. The copy lives in a temporary directory, so any
    recovery happens there and `$SAI_HOME` stays untouched.

    The database and its journals are copied one at a time, so a checkpoint
    in between could leave a copy missing committed rows. The copy is kept
    only if no file changed while it was taken; otherwise doctor retries, and
    raises LedgerChanged after SNAPSHOT_ATTEMPTS tries.
    """
    with tempfile.TemporaryDirectory(prefix="sai-doctor-") as temp:
        copy = Path(temp) / path.name
        for _attempt in range(SNAPSHOT_ATTEMPTS):
            before = ledger_fingerprint(path, dir_fd=dir_fd)
            for suffix in ("", *LEDGER_JOURNALS):
                source = path.with_name(path.name + suffix)
                target = copy.with_name(copy.name + suffix)
                target.unlink(missing_ok=True)
                try:
                    with (
                        open_regular(source, dir_fd=dir_fd) as infile,
                        target.open("wb") as outfile,
                    ):
                        shutil.copyfileobj(infile, outfile)
                except FileNotFoundError:
                    continue
            if ledger_fingerprint(path, dir_fd=dir_fd) == before:
                break
        else:
            raise LedgerChanged
        connection = sqlite3.connect(copy, timeout=5)
        try:
            yield connection
        finally:
            connection.close()


def matches_ledger_schema(connection: sqlite3.Connection, version: int) -> bool:
    if version not in (0, storage.SCHEMA_VERSION):
        return False
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='intents'"
    ).fetchone()
    if table is None:
        return False
    columns = {
        name: (kind.upper(), not_null, primary_key)
        for _, name, kind, not_null, _, primary_key in connection.execute(
            "PRAGMA table_info(intents)"
        )
    }
    expected = {
        name: ("TEXT", 1, 0)
        for name in ("endpoint", "account", "kind", "status", "entity")
    }
    expected["id"] = ("TEXT", 0, 1)
    if version >= 1:
        expected["created_at"] = ("REAL", 0, 0)
    if "keeper_status" in columns:
        expected["keeper_status"] = ("TEXT", 0, 0)
    return columns == expected


def read_ledger(path: Path) -> tuple[int, str, list[tuple[str, str, str]]]:
    """Return the ledger's schema version, quick_check result, and
    (key, endpoint, account) for each unknown intent."""
    with ledger_snapshot(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok" or version > storage.SCHEMA_VERSION:
            return version, integrity, []
        if not matches_ledger_schema(connection, version):
            raise sqlite3.DatabaseError("Unrecognized Sai intent schema")
        rows = connection.execute(
            "SELECT id, endpoint, account FROM intents"
            " WHERE status='unknown' ORDER BY rowid DESC"
        ).fetchall()
    return version, integrity, rows


class WriteGate:
    """Allows ledger writes until `shut` or END (a monotonic time); a write in
    progress finishes first, and gives up waiting for a lock at END."""

    def __init__(self, end: float) -> None:
        self._lock = threading.Lock()
        self._open = True
        self._end = end

    def shut(self) -> None:
        with self._lock:
            self._open = False

    @contextmanager
    def write(self) -> Generator[None]:
        with self._lock:
            left = self._end - time.monotonic()
            if not self._open or left <= 0:
                raise TimeoutError("doctor's deadline passed; not writing")
            # A write that waits on another process's lock gives up at the
            # deadline instead of holding the gate past it.
            with storage.busy_timeout(left), storage.keep_ledger_modes():
                yield


def reconcile_keys(
    endpoint: str, addr: str, keys: Sequence[str], deadline: float
) -> dict[str, Any]:
    """Run `sai intent status` reconciliation for KEYS within DEADLINE
    seconds.

    Each Keeper request gets what is left of DEADLINE, and the worker stops
    before the next key once it runs out. At the deadline the write gate
    shuts, so a late Keeper answer can no longer change the ledger, and the
    daemon thread cannot keep the process alive. Returns any error.
    """
    outcome: dict[str, Any] = {}
    end = time.monotonic() + deadline
    gate = WriteGate(end)

    def work() -> None:
        try:
            for key in keys:
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                reconcile.reconcile(
                    endpoint,
                    addr,
                    key,
                    timeout=min(NETWORK_TIMEOUT, left),
                    write=gate.write,
                )
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            outcome["error"] = type(exc).__name__

    thread = threading.Thread(target=copy_context().run, args=(work,), daemon=True)
    thread.start()
    thread.join(deadline)
    gate.shut()
    if thread.is_alive():
        outcome["error"] = f"no answer within {deadline:g} s"
    return outcome


LEDGER_FILES = ("", "-wal", "-shm", "-journal")


def ledger_identity(path: Path) -> tuple[int, int] | None:
    """The ledger's (st_dev, st_ino) without following a symlink."""
    try:
        info_ = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info_.st_mode):
        return None
    return info_.st_dev, info_.st_ino


def hard_linked(path: Path) -> list[Path]:
    """Return the ledger or its journals that have other hard links.

    A write to one would also change the file at the other name, which may
    be outside `$SAI_HOME/trader`.
    """
    shared: list[Path] = []
    for suffix in LEDGER_FILES:
        file = path.with_name(path.name + suffix)
        try:
            if file.lstat().st_nlink > 1:
                shared.append(file)
        except OSError:
            continue
    return shared


def check_intents(ctx: Context) -> Check:
    path = ledger_path()
    links = symlinks(
        [
            "intents.sqlite3",
            *(f"intents.sqlite3{s}" for s in LEDGER_FILES[1:]),
        ]
    )
    if links:
        return Check(
            "intents",
            "fail",
            f"{', '.join(map(str, links))}: trader path is a symlink; doctor"
            " does not read or write the ledger through it",
            SYMLINK_FIX,
            {"symlinks": [str(link) for link in links]},
        )
    if not path.is_file():
        return Check("intents", "pass", "No intent ledger yet", None, {})
    identity = ledger_identity(path)
    shared = hard_linked(path)
    if shared:
        return Check(
            "intents",
            "fail",
            f"{', '.join(map(str, shared))} has other hard links; fix by hand",
            "Replace the hard link with a copy of the file",
            {"hardLinks": [str(link) for link in shared]},
        )

    def moved() -> Check | None:
        """Refuse to write if the ledger is no longer the file checked above.

        SQLite opens by path, so this narrows the window but cannot close it.
        """
        if ledger_identity(path) == identity and not hard_linked(path):
            return None
        return Check(
            "intents",
            "fail",
            f"{path} changed during the check; doctor did not write it",
            "Run sai doctor again; replace any link with a real file by hand",
            {},
            changes=changes,
        )

    fix_corrupt = (
        f"Copy {path} aside before any trade and restore it from a backup; it"
        " records intents that may still execute"
    )
    changes: list[str] = []
    try:
        version, integrity, rows = read_ledger(path)
        if version > storage.SCHEMA_VERSION:
            return Check(
                "intents",
                "fail",
                f"Intent ledger has schema {version}; this sai reads up to"
                f" {storage.SCHEMA_VERSION}",
                "Upgrade sai: uv tool upgrade sai-cli",
                {"userVersion": version},
            )
        if integrity != "ok":
            return Check(
                "intents",
                "fail",
                "Intent ledger failed its integrity check",
                fix_corrupt,
                {"userVersion": version},
            )
        if version < storage.SCHEMA_VERSION and ctx.fix:
            if refused := moved():
                return refused
            with storage.keep_ledger_modes():
                storage.db().close()
            changes.append(
                f"migrated the ledger from schema {version} to {storage.SCHEMA_VERSION}"
            )
            version, _integrity, rows = read_ledger(path)
    except LedgerChanged:
        return changed_check({}, changes)
    except (sqlite3.Error, OSError):
        return Check("intents", "fail", "Intent ledger is unreadable", fix_corrupt, {})
    state = ctx.session
    addr = state.get("addr") if state.get("endpointMatches") else None

    def blocks(endpoint: str, owner: str) -> bool:
        return addr is not None and (endpoint, owner) == (
            ctx.config.endpoint,
            addr.lower(),
        )

    blocking = [key for key, endpoint, owner in rows if blocks(endpoint, owner)]
    details: dict[str, Any] = {"userVersion": version}
    if blocking and ctx.fix and not ctx.offline and addr is not None:
        if refused := moved():
            return refused
        outcome = reconcile_keys(ctx.config.endpoint, addr, blocking, NETWORK_DEADLINE)
        if "error" in outcome:
            details["reconcileError"] = outcome["error"]
        try:
            _version, _integrity, rows = read_ledger(path)
        except LedgerChanged:
            return changed_check(details, changes)
        except (sqlite3.Error, OSError):
            return Check(
                "intents", "fail", "Intent ledger is unreadable", fix_corrupt, {}
            )
        remaining = {key for key, _endpoint, _owner in rows}
        for key in blocking:
            if key not in remaining:
                changes.append(f"reconciled intent {key} with Keeper")
        # Classify every unknown row again: another process may have
        # reserved a new intent for this account meanwhile.
        blocking = [key for key, endpoint, owner in rows if blocks(endpoint, owner)]
    others = [
        (endpoint, owner)
        for _key, endpoint, owner in rows
        if not blocks(endpoint, owner)
    ]
    accounts = len(set(others))
    details.update(
        unknown=len(rows),
        blocking=blocking,
        otherAccounts={"unknown": len(others), "accounts": accounts},
    )
    check = intents_status(ctx, blocking, len(others), accounts, details)
    check.changes = changes
    if changes and check.status == "pass":
        check.status = "fixed"
        summary = "; ".join(changes)
        check.summary = summary[0].upper() + summary[1:]
    return check


def changed_check(details: dict[str, Any], changes: list[str]) -> Check:
    return Check(
        "intents",
        "warn",
        "Intent ledger changed while reading; another sai process is writing it",
        "Run sai doctor again once other sai commands finish",
        details,
        changes=changes,
    )


def intents_status(
    ctx: Context,
    blocking: list[str],
    others: int,
    accounts: int,
    details: dict[str, Any],
) -> Check:
    if blocking:
        addr = ctx.session["addr"]
        noun = "intent blocks" if len(blocking) == 1 else "intents block"
        return Check(
            "intents",
            "fail",
            f"{len(blocking)} unknown {noun} trading for {short(addr)}",
            storage.intent_status_hint(ctx.config.endpoint, addr, blocking[0]),
            details,
        )
    if others:
        return Check(
            "intents",
            "warn",
            f"{plural(others, 'unknown intent')} for"
            f" {plural(accounts, 'account')} not signed in"
            " here; they do not block this session",
            "Sign in as each account and run `sai intent status ADDR`",
            details,
        )
    if details["userVersion"] < storage.SCHEMA_VERSION:
        return Check(
            "intents",
            "warn",
            f"Intent ledger has schema {details['userVersion']}; the next trade"
            " migrates it",
            "sai doctor",
            details,
        )
    return Check("intents", "pass", "Intent ledger is readable", None, details)


# Network checks


def elapsed_ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


def check_keeper(ctx: Context) -> Check:
    url = log.origin(ctx.config.endpoint)
    start = time.monotonic()
    try:
        route = keeper.request(
            ctx.config.endpoint, keeper.ROUTE, timeout=NETWORK_TIMEOUT
        )["oneClickTrading"]
        enabled = bool(route["enabled"])
        chain = route.get("chainId")
    except (keeper.KeeperError, KeyError, TypeError) as exc:
        return Check(
            "keeper",
            "fail",
            f"{url} did not answer: {exc}",
            "Check the network and --endpoint",
        )
    details = {"endpoint": url, "tradingEnabled": enabled, "chainId": chain}
    took = elapsed_ms(start)
    if not enabled:
        return Check(
            "keeper",
            "warn",
            f"{url} answered in {took} ms, but trading is disabled",
            "Wait for Keeper to enable trading; reads still work",
            details,
        )
    return Check("keeper", "pass", f"{url} answered in {took} ms", None, details)


def check_websocket(ctx: Context) -> Check:
    ws = ctx.config.ws_endpoint
    url = log.origin(ws)
    start = time.monotonic()
    try:
        stream.handshake(ws, NETWORK_TIMEOUT)
    except (stream.StreamError, *stream.DISCONNECTS) as exc:
        reason = type(exc).__name__
        if isinstance(exc, ConnectionClosed) and exc.rcvd is not None:
            reason += f" (close code {exc.rcvd.code})"
        elif isinstance(exc, InvalidStatus):
            reason += f" (HTTP status {exc.response.status_code})"
        return Check(
            "websocket",
            "fail",
            f"{url} sent no connection_ack: {reason}",
            "Check the network and --ws-endpoint",
            {"endpoint": url},
        )
    return Check(
        "websocket",
        "pass",
        f"{url} acknowledged in {elapsed_ms(start)} ms",
        None,
        {"endpoint": url},
    )


def check_lcd(ctx: Context) -> Check:
    url = log.origin(ctx.config.lcd_endpoint)
    start = time.monotonic()
    try:
        node = margin._get_json(
            margin.lcd_url(ctx.config.lcd_endpoint, margin.NODE_INFO_PATH),
            timeout=NETWORK_TIMEOUT,
        )
    except ValueError as exc:
        return Check(
            "lcd",
            "fail",
            f"{url} did not answer: {exc}",
            "Check the network and --lcd-endpoint",
            {"endpoint": url},
        )
    info_block = node.get("default_node_info")
    network = info_block.get("network") if isinstance(info_block, dict) else None
    if not isinstance(network, str) or CHAIN_ID.fullmatch(network) is None:
        return Check(
            "lcd",
            "fail",
            f"{url} returned a missing or invalid chain ID in node_info network",
            "Check --lcd-endpoint points at a Nibiru REST (LCD) service",
            {"endpoint": url},
        )
    return Check(
        "lcd",
        "pass",
        f"{url} answered in {elapsed_ms(start)} ms",
        details={"endpoint": url},
        _lcd_chain_id=network,
    )


def cross_check_network(checks: list[Check]) -> None:
    """Warn when the LCD serves another chain than Keeper's trading route.

    Margin changes and partial closes read the LCD and refuse a mismatch.
    """
    by_id = {check.id: check for check in checks}
    keeper_check, lcd_check = by_id.get("keeper"), by_id.get("lcd")
    if keeper_check is None or lcd_check is None or lcd_check.status != "pass":
        return
    chain = keeper_check.details.get("chainId")
    network = lcd_check._lcd_chain_id
    if not isinstance(chain, str) or not chain or network is None:
        return
    matches = chain == network
    lcd_check.details["matchesKeeperChain"] = matches
    if matches:
        lcd_check.summary += f" (matches Keeper chain {chain})"
        return
    lcd_check.status = "warn"
    lcd_check.summary += f"; LCD network differs from Keeper chain {chain}"
    lcd_check.fix = "Pass the --lcd-endpoint for Keeper's chain"


LOCAL_CHECKS: list[tuple[str, CheckFn]] = [
    ("install", check_install),
    ("version", check_version),
    ("home", check_home),
    ("logs", check_logs),
    ("mcp", check_mcp),
    ("session", check_session),
    ("intents", check_intents),
]
NETWORK_CHECKS: list[tuple[str, CheckFn]] = [
    ("keeper", check_keeper),
    ("websocket", check_websocket),
    ("lcd", check_lcd),
]


def timed(name: str, fn: CheckFn, ctx: Context) -> list[Check]:
    """Run FN, time it, and turn a crash into a failed check."""
    start = time.monotonic()
    try:
        result = fn(ctx)
        checks = [result] if isinstance(result, Check) else list(result)
    except Exception as exc:  # noqa: BLE001 - one broken check must not hide others
        checks = [
            Check(
                name,
                "fail",
                f"Check crashed with {type(exc).__name__}",
                "Report this at https://github.com/SaiDotFun/open-sai/issues",
            )
        ]
    took = elapsed_ms(start)
    for check in checks:
        check.duration_ms = took
    return checks


def run_network(ctx: Context, deadline: float) -> list[Check]:
    """Run NETWORK_CHECKS in parallel and stop waiting after DEADLINE seconds.

    httpx timeouts apply per read, so a server that drips bytes could hold a
    check open indefinitely. Each check runs in a daemon thread; one still
    running at the deadline becomes a fail, and its thread cannot keep the
    process alive.
    """
    results: dict[str, list[Check]] = {}

    def work(name: str, fn: CheckFn) -> None:
        results[name] = timed(name, fn, ctx)

    threads = [
        threading.Thread(target=copy_context().run, args=(work, name, fn), daemon=True)
        for name, fn in NETWORK_CHECKS
    ]
    end = time.monotonic() + deadline
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(end - time.monotonic(), 0))
    checks: list[Check] = []
    for name, _fn in NETWORK_CHECKS:
        if name in results:
            checks.extend(results[name])
        else:
            checks.append(
                Check(
                    name,
                    "fail",
                    f"No answer within {deadline:g} s",
                    "Check the network and the endpoint options",
                    duration_ms=round(deadline * 1000),
                )
            )
    return checks


def run_checks(
    ctx: Context, *, after_home: Callable[[], None] | None = None
) -> list[Check]:
    """Run local checks in order, then network checks in parallel."""
    checks: list[Check] = []
    for name, fn in LOCAL_CHECKS:
        if not ctx.config_available and name in ("session", "intents"):
            checks.append(Check(name, "skipped", "Settings could not be loaded"))
        else:
            checks.extend(timed(name, fn, ctx))
        if name == "home" and after_home is not None:
            after_home()
    if ctx.offline:
        checks.extend(
            Check(name, "skipped", "Skipped with --offline")
            for name, _fn in NETWORK_CHECKS
        )
        return checks
    if not ctx.config_available:
        checks.extend(
            Check(name, "skipped", "Settings could not be loaded")
            for name, _fn in NETWORK_CHECKS
        )
        return checks
    network = run_network(ctx, NETWORK_DEADLINE)
    cross_check_network(network)
    checks.extend(network)
    return checks


def overall(checks: Sequence[Check]) -> str:
    """fail if any check failed, warn if any warned, else pass; fixed and
    skipped count as pass."""
    statuses = {check.status for check in checks}
    if "fail" in statuses:
        return "fail"
    return "warn" if "warn" in statuses else "pass"


def session_state(config: Settings) -> dict[str, Any]:
    """Return `tools.session_state`, or an error state if the stored session
    cannot be interpreted, such as an expiry without a time zone."""
    try:
        state = tools.session_state(config)
        if state.get("signedIn"):
            expiry = datetime.fromisoformat(state["expiresAt"])
            if expiry.tzinfo is None:
                raise TypeError("expiry has no time zone")
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        return {
            "signedIn": False,
            "error": f"Stored session is invalid ({type(exc).__name__});"
            f" {storage.SIGN_IN_AGAIN}",
        }
    return state


def context(
    config: Settings | None,
    path_env: str | None = None,
    *,
    fix: bool = True,
    offline: bool = False,
) -> Context:
    return Context(
        fix=fix,
        offline=offline,
        config=config if config is not None else settings.resolve({}, {}, {}),
        session=session_state(config) if config is not None else {},
        now=datetime.now(UTC),
        path_env=os.environ.get("PATH", "") if path_env is None else path_env,
        config_available=config is not None,
    )


def report(checks: Sequence[Check], now: datetime | None = None) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": (now or datetime.now(UTC)).isoformat(),
        "sai": {"version": info.version(), "install": info.provenance()},
        "overall": overall(checks),
        "checks": [check.to_json() for check in checks],
    }


def render_json(value: dict[str, Any]) -> str:
    return tools.redact(json.dumps(value, separators=(",", ":"), default=str))


def render_text(checks: Sequence[Check]) -> str:
    width = max([9, *(len(check.id) for check in checks)]) + 2
    lines: list[str] = []
    for check in checks:
        lines.append(f"{check.status:<8}{check.id:<{width}}{check.summary}")
        lines.extend(f"{'':<{8 + width}}did: {c}" for c in check.changes)
        if check.fix:
            lines.append(f"{'':<{8 + width}}fix: {check.fix}")
    counts = ", ".join(
        f"{sum(c.status == s for c in checks)} {s}"
        for s in STATUSES
        if any(c.status == s for c in checks)
    )
    lines.append(f"\noverall: {overall(checks)} ({counts})")
    return tools.redact("\n".join(lines))
