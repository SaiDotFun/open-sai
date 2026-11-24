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
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

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
    invalid_endpoint: bool = False

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
    """Validate file settings before resolving flag and environment overrides."""
    path = settings.config_path(os.environ)
    fix = "Edit config.toml in the Sai home directory and rerun `sai doctor`"
    try:
        values, warnings = settings.read_file(path)
    except settings.ConfigError as exc:
        return Check(
            "config",
            "fail",
            exc.diagnostic,
            fix,
            invalid_endpoint=isinstance(exc, settings.InvalidEndpoint),
        ), None
    try:
        config = settings.resolve(flags, os.environ, values)
    except settings.InvalidEndpoint as exc:
        return Check(
            "config",
            "fail",
            str(exc),
            "Check the settings listed by `sai config --help`",
            invalid_endpoint=True,
        ), None
    except ValueError:
        return Check(
            "config",
            "fail",
            "A flag or environment setting is invalid",
            "Check the settings listed by `sai config --help`",
        ), None
    invalid = [
        key for key, value in values.items() if isinstance(value, settings.Invalid)
    ]
    if invalid:
        return Check(
            "config", "fail", f"Invalid config.toml settings: {', '.join(invalid)}", fix
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
            "config", "warn", "config.toml contains unknown settings", fix
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


def tighten(root: Path, names: Sequence[str]) -> tuple[list[str], list[str]]:
    """Clear group and other bits on ROOT and each of NAMES in it.

    Every chmod goes through a descriptor opened with O_NOFOLLOW, and the
    files open relative to the directory's descriptor, so a path swapped for
    a symlink after the checks fails to open instead of redirecting the
    chmod. Returns the changes made and the fix commands for paths it could
    not change.
    """
    changes: list[str] = []
    failed: list[str] = []
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    except OSError:
        return changes, [f"chmod go-rwx {root}"]
    try:
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
                failed.append(f"chmod go-rwx {path}")
                continue
            try:
                info_ = os.fstat(fd)
                if (
                    name is None
                    and hasattr(os, "getuid")
                    and info_.st_uid != os.getuid()
                ):
                    failed.append(f"{path} belongs to another user; fix by hand")
                    break
                if name is not None and info_.st_nlink > 1:
                    failed.append(f"{path} has other hard links; fix by hand")
                    continue
                mode = stat.S_IMODE(info_.st_mode)
                if mode & 0o077:
                    os.fchmod(fd, mode & ~0o077)
                    changes.append(f"chmod {path} {mode:o} -> {mode & ~0o077:o}")
            except OSError:
                failed.append(f"chmod go-rwx {path}")
            finally:
                if fd != dir_fd:
                    os.close(fd)
    finally:
        os.close(dir_fd)
    return changes, failed


def profile_has_data(fd: int, owner: int, directory: str = "") -> bool | None:
    """Check for Sai data. Return None if the layout contains an unfamiliar entry."""
    has_data = False
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if info.st_uid != owner:
            return None
        if name in PROFILE_DIRS.get(directory, ()) and stat.S_ISDIR(info.st_mode):
            if info.st_mode & PROFILE_SHARED_MODE:
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
            or (directory == "logs" and log.parse_name(name) is not None)
            or (directory == "cache/wheels" and update.is_wheel_name(name))
        ):
            return None
        elif directory != "logs":
            # Doctor creates diagnostics when it runs. Those alone do not
            # establish that the user had a Sai profile here.
            has_data = True
    return has_data


def tighten_profile_root(path: Path) -> str | None:
    """Repair a dedicated, user-owned profile during an explicit doctor fix."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            if (
                info.st_uid != os.getuid()
                or mode & PROFILE_SHARED_MODE
                or not mode & 0o077
                or not profile_has_data(fd, info.st_uid)
                or os.fstat(fd).st_mode != info.st_mode
            ):
                return None
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
    if loose and not ctx.fix:
        trader_fix = "; ".join(f"chmod go-rwx {path}" for path, _, _ in trader_loose)
        return Check(
            "home",
            "warn",
            f"{plural(len(loose), 'path')} in {sai_home} accessible by other users",
            f"{home_fix} {trader_fix}".strip(),
            details,
        )
    if not trader_exists and not ctx.fix:
        return Check(
            "home",
            "pass",
            f"{root} does not exist yet; sai creates it on first use",
            None,
            details,
        )
    changes: list[str] = []
    failed: list[str] = []
    if trader_exists and trader_loose:
        changes, failed = tighten(root, MANAGED)
    if home_loose and not failed:
        change = tighten_profile_root(sai_home)
        if change is not None:
            changes.append(change)
            home_fix = ""
    if not trader_exists:
        home_exists = sai_home.is_dir()
        storage.directory()
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
            f"{sai_home} is accessible by other users",
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
    if not ctx.fix:
        return Check(
            "logs",
            "warn",
            f"{plural(len(loose), 'path')} under {root} readable by other users",
            "; ".join(f"chmod go-rwx {path}" for path, _, _ in loose),
            details,
        )
    changes, failed = tighten(root, names)
    if failed:
        return Check(
            "logs",
            "fail",
            f"Could not make {plural(len(failed), 'path')} private",
            "; ".join(failed),
            details,
            changes=changes,
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
    details = {
        "addr": addr,
        "expiresAt": state["expiresAt"],
        "endpoint": state["endpoint"],
        "endpointMatches": state["endpointMatches"],
    }
    if state["expired"]:
        return Check(
            "session",
            "fail",
            f"Session for {short(addr)} expired at {state['expiresAt']}",
            state.get("renewalCommand", SIGN_IN),
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
            warning,
            state["renewalCommand"],
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


def ledger_fingerprint(path: Path) -> list[tuple[int, int, int] | None]:
    """Size, mtime, and inode of the ledger and each journal, or None."""
    marks: list[tuple[int, int, int] | None] = []
    for suffix in ("", *LEDGER_JOURNALS):
        try:
            info_ = path.with_name(path.name + suffix).stat()
        except FileNotFoundError:
            marks.append(None)
            continue
        marks.append((info_.st_size, info_.st_mtime_ns, info_.st_ino))
    return marks


@contextmanager
def ledger_snapshot(path: Path) -> Generator[sqlite3.Connection]:
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
            before = ledger_fingerprint(path)
            for suffix in ("", *LEDGER_JOURNALS):
                source = path.with_name(path.name + suffix)
                target = copy.with_name(copy.name + suffix)
                target.unlink(missing_ok=True)
                if source.is_file():
                    shutil.copyfile(source, target)
            if ledger_fingerprint(path) == before:
                break
        else:
            raise LedgerChanged
        connection = sqlite3.connect(copy, timeout=5)
        try:
            yield connection
        finally:
            connection.close()


def read_ledger(path: Path) -> tuple[int, str, list[tuple[str, str, str]]]:
    """Return the ledger's schema version, quick_check result, and
    (key, endpoint, account) for each unknown intent."""
    with ledger_snapshot(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='intents'"
        ).fetchone()
        rows = (
            connection.execute(
                "SELECT id, endpoint, account FROM intents"
                " WHERE status='unknown' ORDER BY rowid DESC"
            ).fetchall()
            if tables and integrity == "ok"
            else []
        )
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
            with storage.busy_timeout(left):
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

    thread = threading.Thread(target=work, daemon=True)
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
        f"{url} answered in {elapsed_ms(start)} ms (network {network})",
        None,
        {"endpoint": url, "network": network},
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
    network = lcd_check.details.get("network")
    if chain and network and chain != network:
        lcd_check.status = "warn"
        lcd_check.summary += f"; Keeper trades on {chain}"
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
        threading.Thread(target=work, args=(name, fn), daemon=True)
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
