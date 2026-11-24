"""Register `sai mcp serve` with agent clients: Claude Code, Codex, Cursor.

Ordinary Claude Code installs and uninstalls use its `mcp add` and
`mcp remove` commands. Explicit Claude Code repairs edit the existing
entry in its config file. Codex and Cursor writes also edit config files.

Registration inspection keeps only Sai's command, args, and selected
metadata. Repair retains the full entry in memory to preserve settings
when publishing the replacement.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import InlineTable

from .core import settings

NAME = "sai"
RETIRED_NAME = "sai-mcp"
SERVER_NAMES = (NAME, RETIRED_NAME)
HIDDEN = "<hidden>"
CODEX_TOOL_FILTERS = ("enabled_tools", "disabled_tools")
STILL_ACTIVE = "a project .codex/config.toml entry is still active"
CLIENTS = ("claude", "codex", "cursor")
ALIASES = {"claude-code": "claude", "claude_code": "claude"}
SCOPES = ("user", "project")
UNINSTALL_SCOPES = ("user", "project", "local")
SUBPROCESS_TIMEOUT = 60
INSTALL_URL = "git+https://github.com/SaiDotFun/open-sai"
ENDPOINT_FLAGS = {spec.flag: spec.key for spec in settings.SETTINGS if spec.flag}


class InstallError(Exception):
    """A refusal that applies to every client, such as an unstable path."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Target:
    """What a client should launch: the server argv and its environment."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    explicit_command: bool = False


@dataclass(frozen=True)
class Registration:
    """The `sai` entry found in a client.

    Keeps argv, the SAI_HOME value (a path), and only the names of any other
    environment settings. Install writes only SAI_HOME to canonical entries.
    """

    argv: list[str]
    scope: str
    sai_home: str | None = None
    stale_env: tuple[str, ...] = ()
    disabled: bool = False
    # Names of settings that change what the server offers, such as a Codex
    # tool allowlist; install sets none, so an entry with any is not current.
    extra_settings: tuple[str, ...] = ()
    # Hash of the whole entry (env values, URL, and all), so two entries can
    # be compared without keeping or printing their values.
    fingerprint: str = ""
    invalid_sai_home: bool = False
    stdio: bool = True

    def shown(self, target: Target | None) -> dict[str, Any]:
        """Show the expected argv when the saved command matches.

        Saved directories and args can contain private values. An unmatched
        entry shows only the command's file name.
        """
        if target is not None and self.matches_command(target):
            return {"command": target.argv}
        if not self.argv:
            return {"command": []}
        out: dict[str, Any] = {"command": [Path(self.argv[0]).name]}
        if len(self.argv) > 1:
            out["args"] = HIDDEN
        return out

    def matches(self, target: Target) -> bool:
        return (
            self.matches_command(target)
            and self.matches_sai_home(target.env)
            and not self.stale_env
            and not self.extra_settings
            and not self.disabled
        )

    def matches_command(self, target: Target) -> bool:
        if self.argv == target.argv:
            return True
        return bool(
            self.argv
            and target.argv
            and self.argv[1:] == target.argv[1:]
            and normalize_launcher(self.argv[0]) == normalize_launcher(target.argv[0])
        )

    def matches_sai_home(self, env: Mapping[str, str]) -> bool:
        return not self.invalid_sai_home and self.sai_home == env.get("SAI_HOME")

    def to_json(self, target: Target | None, effective: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "scope": self.scope,
            **self.shown(target),
            "current": target is not None and self.matches(target),
            "effective": effective,
        }
        if self.stale_env:
            out["staleEnv"] = list(self.stale_env)
        if self.extra_settings:
            out["extraSettings"] = list(self.extra_settings)
        if self.disabled:
            out["disabled"] = True
        return out


@dataclass
class Result:
    client: str
    action: str
    command: list[str] | None = None
    args: str | None = None
    argv: list[list[str]] | None = None
    path: str | None = None
    would: str | None = None
    error: str | None = None
    fix: str | None = None
    scope: str | None = None
    shadowed_by: str | None = None
    # A client config was written, even if the check afterwards failed.
    changed: bool = False
    name: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"client": self.client, "action": self.action}
        names = {"shadowed_by": "shadowedBy"}
        for key in (
            "scope",
            "name",
            "would",
            "command",
            "args",
            "argv",
            "path",
            "shadowed_by",
            "error",
            "fix",
        ):
            value = getattr(self, key)
            if value is not None:
                out[names.get(key, key)] = value
        return out


class ClientError(Exception):
    """A client command or config file failed; the message is safe to print."""


@dataclass(frozen=True)
class ClaudeRepair:
    scope: str
    name: str
    reset_args: bool = False
    replace_entry: bool = False
    use_profile: bool = False


@dataclass(frozen=True)
class RegistrationCheck:
    client: str
    status: str
    summary: str
    fix: str | None = None
    changes: tuple[str, ...] = ()
    pinned: bool = False
    legacy_name: bool = False
    manual_repairs: tuple[ClaudeRepair, ...] = ()


def claude_replacement_commands(entry: ClaudeRepair) -> list[str]:
    scope_and_name = shlex.join(["--scope", entry.scope, entry.name])
    profile = (
        ' --env="SAI_HOME=${SAI_HOME:?Set SAI_HOME first}"' if entry.use_profile else ""
    )
    return [
        f"claude mcp remove {scope_and_name}",
        f'claude mcp add {scope_and_name}{profile} -- "$(command -v sai)" mcp serve',
    ]


def claude_repair_hint(entries: tuple[ClaudeRepair, ...]) -> str:
    commands = [
        " then ".join(f"`{command}`" for command in claude_replacement_commands(entry))
        if entry.replace_entry
        else (
            f"`sai mcp install --client claude --scope {entry.scope}"
            f" --repair-existing {entry.name}"
            f"{' --reset-args' if entry.reset_args else ''}`"
        )
        for entry in entries
    ]
    hint = "Run " + " and ".join(commands) + "."
    if any(entry.scope != "user" for entry in entries):
        hint += " Run project and local commands from the project directory."
    if any(entry.reset_args for entry in entries):
        hint += (
            " The --reset-args flag replaces unsupported arguments with the defaults."
        )
    return hint


def report_different_claude_entry(result: Result, scope: str, target: Target) -> Result:
    entry = ClaudeRepair(
        scope, NAME, replace_entry=True, use_profile="SAI_HOME" in target.env
    )
    hint = claude_repair_hint((entry,))
    if target.explicit_command:
        hint += " For a custom launcher, substitute your requested command after `--`."
    result.command = None
    result.error = f"The existing {scope} Claude Code sai registration differs from the requested settings"
    result.fix = hint
    return result


def normalize_client(name: str) -> str:
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    if key not in CLIENTS:
        raise ValueError(f"unknown client {name!r}; choose from {', '.join(CLIENTS)}")
    return key


# --- server command -------------------------------------------------------


def uv_cache_dirs(environ: Mapping[str, str], home: Path) -> list[Path]:
    """Directories uv may use as its cache, where `uvx` installs live."""
    dirs = []
    if environ.get("UV_CACHE_DIR"):
        dirs.append(Path(environ["UV_CACHE_DIR"]))
    if environ.get("XDG_CACHE_HOME"):
        dirs.append(Path(environ["XDG_CACHE_HOME"]) / "uv")
    if environ.get("LOCALAPPDATA"):
        dirs.append(Path(environ["LOCALAPPDATA"]) / "uv" / "cache")
    dirs += [home / ".cache" / "uv", home / "Library" / "Caches" / "uv"]
    return dirs


def inside(path: Path, roots: list[Path]) -> bool:
    candidates = {Path(os.path.abspath(path)), Path(os.path.realpath(path))}
    for root in roots:
        for base in {Path(os.path.abspath(root)), Path(os.path.realpath(root))}:
            if any(p.is_relative_to(base) for p in candidates):
                return True
    return False


def normalize_launcher(command: str) -> str:
    """Resolve the directory of an absolute launcher and keep uv's executable link."""
    if not os.path.isabs(command):
        return command
    directory, name = os.path.split(command)
    return os.path.join(os.path.realpath(directory), name)


def launcher_target(launcher: str, environ: Mapping[str, str]) -> Target:
    return Target([launcher, "mcp", "serve"], server_env(environ))


def server_env(environ: Mapping[str, str]) -> dict[str, str]:
    home = environ.get("SAI_HOME")
    return {"SAI_HOME": os.path.abspath(os.path.expanduser(home))} if home else {}


def server_target(
    override: list[str] | None,
    environ: Mapping[str, str],
    home: Path,
    running_prefix: str,
) -> tuple[Target, list[str]]:
    """Choose the argv clients launch, and return warnings for stderr.

    With no override, register the unresolved `sai` on PATH, normally the
    `~/.local/bin/sai` link that `uv tool` keeps across updates.
    """
    if override:
        return Target(list(override), server_env(environ), explicit_command=True), []
    found = shutil.which("sai", path=environ.get("PATH"))
    if found is None:
        raise InstallError(
            "NOT_ON_PATH",
            "No `sai` on PATH to register. Install it with"
            f" `uv tool install {INSTALL_URL}`, or pass the server command"
            " after `--`.",
        )
    path = Path(os.path.abspath(found))
    if inside(path, uv_cache_dirs(environ, home)):
        raise InstallError(
            "UNSTABLE_PATH",
            f"The `sai` on PATH ({path}) is inside the uv cache, which uv"
            " cleans up, so a client registered with it would break. Install"
            f" sai first with `uv tool install {INSTALL_URL}`, then rerun this"
            " command.",
        )
    warnings = []
    if not inside(path, [Path(running_prefix)]):
        warnings.append(
            f"The `sai` on PATH ({path}) is a different install than the one"
            f" running ({running_prefix}); clients get the one on PATH."
        )
    return launcher_target(str(path), environ), warnings


# --- helpers --------------------------------------------------------------


def run(argv: list[str]) -> None:
    """Run a client command without relaying output that may contain config values."""
    command = " ".join([Path(argv[0]).name, *argv[1:3]])
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClientError(f"`{command}` did not run: {type(exc).__name__}") from None
    if done.returncode != 0:
        raise ClientError(
            f"`{command}` exited {done.returncode}. Run the client command directly for details."
        )


def load_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object file; None when missing. Never echoes contents."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ClientError(f"could not read {path}: {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise ClientError(f"could not read {path}: not a JSON object")
    return data


def entry_registration(
    entry: Any,
    scope: str,
    disabled: bool = False,
    whole: Any = None,
    extra_settings: tuple[str, ...] = (),
) -> Registration | None:
    """Keep command, args, SAI_HOME, and other env names from a `sai` entry.

    Other env values are never kept. Codex `env_vars` (variables passed
    through from the client's environment) count as other env too. The
    fingerprint hashes `whole`, everything the client reported for the
    server, or the entry itself when that is all there is.
    """
    if not isinstance(entry, dict):
        return None
    hashed = entry if whole is None else whole
    canonical = json.dumps([hashed, disabled], sort_keys=True, default=str)
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    command = entry.get("command")
    args = entry.get("args", [])
    argv = (
        [command, *args]
        if isinstance(command, str)
        and isinstance(args, list)
        and all(isinstance(arg, str) for arg in args)
        else []
    )
    env = entry.get("env")
    env = env if isinstance(env, dict) else {}
    home = env.get("SAI_HOME")
    stale = {str(k) for k in env if k != "SAI_HOME"}
    passed = entry.get("env_vars")
    for item in passed if isinstance(passed, list) else []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str):
            stale.add(name)
    return Registration(
        argv,
        scope,
        home if isinstance(home, str) else None,
        tuple(sorted(stale)),
        disabled,
        extra_settings,
        fingerprint,
        invalid_sai_home="SAI_HOME" in env and not isinstance(home, str),
        stdio=entry.get("type", "stdio") == "stdio" and "url" not in entry,
    )


def same_entry(a: Registration, b: Registration) -> bool:
    """Equal apart from scope, env values and transport URL included."""
    return a.fingerprint == b.fingerprint


def servers(data: dict[str, Any] | None) -> dict[str, Any]:
    found = (data or {}).get("mcpServers")
    return found if isinstance(found, dict) else {}


def read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def before_replace(path: Path) -> None:
    """Hook between writing the temp file and the final check; tests use it."""


def replace_if_unchanged(
    path: Path, data: dict[str, Any], seen: bytes | None, destination: Path
) -> bool:
    return replace_bytes(
        path,
        (json.dumps(data, indent=2) + "\n").encode(),
        seen,
        destination=destination,
    )


def replace_bytes(
    path: Path, data: bytes, seen: bytes | None, *, destination: Path
) -> bool:
    """Write data over path in one rename if path still holds `seen`.

    Keeps the file mode (0600 for a new file). Returns False, writing
    nothing, when another program changed the file since it was read.
    """
    # Replace the file, including when the configured path is a symlink.
    # Recheck the link before writing so a concurrent retarget is detectable.
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        before_replace(path)
        if path.resolve() != destination or read_bytes(destination) != seen:
            return False
        try:
            mode = stat.S_IMODE(destination.stat().st_mode)
        except FileNotFoundError:
            if seen is not None:
                return False
            mode = 0o600
        os.chmod(temp, mode)
        os.replace(temp, destination)
        return True
    finally:
        Path(temp).unlink(missing_ok=True)


# --- clients --------------------------------------------------------------


class Client:
    name = ""
    binary = ""

    def __init__(
        self, home: Path, environ: Mapping[str, str], cwd: Path | None
    ) -> None:
        self.home = home
        self.environ = environ
        self.cwd = cwd

    def exe(self) -> str | None:
        if not self.binary:
            return None
        return shutil.which(self.binary, path=self.environ.get("PATH"))

    def home_dir(self) -> Path:
        raise NotImplementedError

    def detected(self) -> tuple[bool, str | None]:
        if self.exe():
            return True, "cli"
        if self.home_dir().is_dir():
            return True, "home"
        return False, None

    def require_exe(self) -> str:
        exe = self.exe()
        if exe is None:
            raise ClientError(f"`{self.binary}` is not on PATH")
        return exe

    # Scopes in the order the client prefers them; the first found wins.
    precedence: tuple[str, ...] = ("user",)
    # Scopes sai reads but never edits.
    read_only_scopes: frozenset[str] = frozenset()

    def registrations(self, name: str = NAME) -> list[Registration]:
        """Entries for this server name, the effective one first."""
        raise NotImplementedError

    def rank(self, scope: str) -> int:
        order = self.precedence
        return order.index(scope) if scope in order else len(order)

    def ordered(self, found: list[Registration]) -> list[Registration]:
        return sorted(found, key=lambda r: self.rank(r.scope))

    def effective(self, found: list[Registration]) -> Registration | None:
        return found[0] if found else None

    def registration(self, scope: str, name: str = NAME) -> Registration | None:
        return next((r for r in self.registrations(name) if r.scope == scope), None)

    def add_steps(
        self, scope: str, target: Target, name: str = NAME
    ) -> list[list[str]]:
        raise NotImplementedError

    def remove_steps(self, scope: str, name: str = NAME) -> list[list[str]]:
        raise NotImplementedError

    def verify(self, scope: str, target: Target | None, *, name: str = NAME) -> None:
        """Re-read after a change: the entry matches target, or is gone."""
        reg = self.registration(scope, name)
        if target is not None and (reg is None or not reg.matches(target)):
            raise ClientError(f"{self.name} does not show the new sai entry")
        if target is None and reg is not None:
            raise ClientError(f"{self.name} still shows the sai entry")


class Claude(Client):
    name = "claude"
    binary = "claude"
    precedence = ("local", "project", "user")

    def home_dir(self) -> Path:
        configured = self.environ.get("CLAUDE_CONFIG_DIR")
        return Path(configured) if configured else self.home / ".claude"

    def config_path(self) -> Path:
        configured = self.environ.get("CLAUDE_CONFIG_DIR")
        return (Path(configured) if configured else self.home) / ".claude.json"

    def registrations(self, name: str = NAME) -> list[Registration]:
        # Never `claude mcp get`: it starts the server to check its health.
        data = load_json(self.config_path())
        projects = (data or {}).get("projects")
        project = (
            projects.get(str(self.cwd))
            if self.cwd is not None and isinstance(projects, dict)
            else None
        )
        project = project if isinstance(project, dict) else None
        toggled_off = (project or {}).get("disabledMcpServers")
        disabled = isinstance(toggled_off, list) and name in toggled_off
        found = []
        user = servers(data).get(name)
        if user is not None:
            found.append(self.read_entry(user, "user", name, disabled))
        if self.cwd is None:
            return self.ordered([r for r in found if r is not None])
        local = servers(project).get(name)
        if local is not None:
            found.append(self.read_entry(local, "local", name, disabled))
        shared = servers(load_json(self.cwd / ".mcp.json")).get(name)
        if shared is not None:
            found.append(
                self.read_entry(shared, "project", name, self.project_disabled(name))
            )
        return self.ordered([r for r in found if r is not None])

    @staticmethod
    def read_entry(
        entry: Any, scope: str, name: str, disabled: bool
    ) -> Registration | None:
        extras = ()
        if (
            name == NAME
            and isinstance(entry, dict)
            and (
                entry.keys() - {"type", "command", "args", "env"}
                or entry.get("type", "stdio") != "stdio"
            )
        ):
            extras = ("custom settings",)
        return entry_registration(entry, scope, disabled, extra_settings=extras)

    def project_disabled(self, name: str) -> bool:
        assert self.cwd is not None
        settings_paths = [
            self.home_dir() / "settings.json",
            self.cwd / ".claude" / "settings.json",
            self.cwd / ".claude" / "settings.local.json",
        ]
        main = self.main_checkout()
        if main is not None and main != self.cwd:
            settings_paths.append(main / ".claude" / "settings.local.json")
        for path in settings_paths:
            rejected = (load_json(path) or {}).get("disabledMcpjsonServers")
            if isinstance(rejected, list) and name in rejected:
                return True
        return False

    def main_checkout(self) -> Path | None:
        """Find the Git checkout Claude uses for project-local settings."""
        if self.cwd is None or os.name == "nt":
            return None
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain", "-z"],
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            first = result.stdout.partition(b"\0\0")[0].split(b"\0")
            if (
                result.returncode
                or not first[0].startswith(b"worktree ")
                or b"bare" in first
            ):
                return None
            root = Path(os.fsdecode(first[0].removeprefix(b"worktree ")))
            if not root.is_absolute() or root == self.home:
                return None
            for path in (root, root / ".git", root / ".claude"):
                if path.exists() and path.stat().st_uid != os.getuid():
                    return None
            return root
        except (OSError, subprocess.TimeoutExpired):
            return None

    def add_steps(
        self, scope: str, target: Target, name: str = NAME
    ) -> list[list[str]]:
        exe = self.require_exe()
        env = [f"--env={k}={v}" for k, v in target.env.items()]
        return [[exe, "mcp", "add", "--scope", scope, name, *env, "--", *target.argv]]

    def remove_steps(self, scope: str, name: str = NAME) -> list[list[str]]:
        return [[self.require_exe(), "mcp", "remove", "--scope", scope, name]]


class Codex(Client):
    name = "codex"
    binary = "codex"

    def home_dir(self) -> Path:
        configured = self.environ.get("CODEX_HOME")
        return Path(configured) if configured else self.home / ".codex"

    precedence = ("project", "user")
    read_only_scopes = frozenset({"project"})

    def registrations(self, name: str = NAME) -> list[Registration]:
        """The user entry, plus a project override when Codex reports one.

        Codex decides which project configs load here (project root, trust),
        so sai asks it twice instead of reading project files: once from
        $CODEX_HOME for the user entry, once from the working directory for
        the entry Codex would use here. A difference is a project override.
        """
        neutral = self.neutral_dir()
        user = self.get(neutral, "user", name)
        found = [user] if user is not None else []
        if self.cwd is not None and self.cwd.resolve() != neutral.resolve():
            here = self.get(self.cwd, "project", name)
            if here is not None and (user is None or not same_entry(here, user)):
                found.append(here)
        return self.ordered(found)

    def neutral_dir(self) -> Path:
        home = self.home_dir()
        return home if home.is_dir() else Path(tempfile.gettempdir())

    def verify(self, scope: str, target: Target | None, *, name: str = NAME) -> None:
        """Ask Codex again, from $CODEX_HOME and from the working directory.

        Equal user and project entries look like one entry before a change,
        so only the answer from the working directory afterwards shows
        whether a project entry is still what Codex runs here.
        """
        super().verify(scope, target, name=name)
        if self.cwd is None:
            return
        here = self.get(self.cwd, "project", name)
        if target is None and here is None:
            return
        if target is not None and here is not None and here.matches(target):
            return
        raise ClientError(STILL_ACTIVE)

    def get(self, cwd: Path, scope: str, name: str = NAME) -> Registration | None:
        """Run `codex mcp get` for one Sai server name; keep command and args."""
        exe = self.exe()
        if exe is None:
            return None
        command = f"codex mcp get {name} --json"
        try:
            done = subprocess.run(
                [exe, "mcp", "get", name, "--json"],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=SUBPROCESS_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClientError(
                f"`{command}` did not run: {type(exc).__name__}"
            ) from None
        if done.returncode != 0:
            missing = f"Error: No MCP server named '{name}' found.".encode()
            # Codex can warn about PATH alias initialization before the lookup.
            diagnostics = [
                line
                for line in done.stderr.splitlines()
                if line.strip() and not line.startswith(b"WARNING: ")
            ]
            if (
                done.returncode == 1
                and not done.stdout.strip()
                and diagnostics == [missing]
            ):
                return None
            raise ClientError(
                f"`{command}` exited {done.returncode}. Run the client command directly for details."
            )
        try:
            data = json.loads(done.stdout.decode("utf-8"))
        except ValueError:
            raise ClientError(f"`{command}` printed invalid JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("transport"), dict):
            raise ClientError(f"`{command}` returned an invalid registration")
        disabled = data.get("enabled") is False
        extras = tuple(k for k in CODEX_TOOL_FILTERS if data.get(k) is not None)
        return entry_registration(
            data.get("transport"), scope, disabled, whole=data, extra_settings=extras
        )

    def project_message(self) -> str:
        return (
            "Codex uses a project-level sai entry in this directory that"
            " overrides the user entry; edit or remove it in the project's"
            " .codex/config.toml, then run this again"
        )

    def check_scope(self, scope: str) -> None:
        if scope != "user":
            raise ClientError("Codex registers MCP servers per user only")

    def config_path(self, scope: str) -> Path:
        self.check_scope(scope)
        return self.home_dir() / "config.toml"

    def write(self, scope: str, target: Target | None, *, name: str = NAME) -> None:
        edit_codex_config(self.config_path(scope), target, name=name)


class Cursor(Client):
    name = "cursor"
    binary = "cursor-agent"
    HINT = "You may need to enable sai in Cursor under Cursor Settings > MCP."
    precedence = ("project", "user")

    def home_dir(self) -> Path:
        return self.home / ".cursor"

    def detected(self) -> tuple[bool, str | None]:
        # Cursor's editor CLI and `cursor-agent` both read ~/.cursor/mcp.json.
        if self.exe() or shutil.which("cursor", path=self.environ.get("PATH")):
            return True, "cli"
        if self.home_dir().is_dir():
            return True, "home"
        return False, None

    def config_path(self, scope: str) -> Path:
        base = self.cwd if scope == "project" else self.home
        if base is None:
            raise ClientError("No working directory for the project registration")
        return base / ".cursor" / "mcp.json"

    def registrations(self, name: str = NAME) -> list[Registration]:
        found = []
        for scope in SCOPES if self.cwd is not None else ("user",):
            entry = servers(load_json(self.config_path(scope))).get(name)
            if entry is not None:
                reg = entry_registration(entry, scope)
                if reg is not None:
                    found.append(reg)
        return self.ordered(found)

    def write(self, scope: str, target: Target | None, *, name: str = NAME) -> None:
        """Set or delete the sai entry, merging again once if the file moves."""
        path = self.config_path(scope)
        for _ in range(2):
            destination = path.resolve()
            seen = read_bytes(destination)
            data = load_json(destination) or {}
            if read_bytes(destination) != seen:
                continue
            entries = data.get("mcpServers")
            if not isinstance(entries, dict):
                entries = {}
            if target is None:
                entries.pop(name, None)
            else:
                entry: dict[str, Any] = {
                    "command": target.argv[0],
                    "args": target.argv[1:],
                }
                if target.env:
                    entry["env"] = dict(target.env)
                entries[NAME] = entry
            data["mcpServers"] = entries
            if replace_if_unchanged(path, data, seen, destination):
                return
        raise ClientError(
            f"{path} kept changing while sai was writing it; nothing was"
            " written. Run the command again."
        )


def make_clients(
    home: Path, environ: Mapping[str, str], cwd: Path | None
) -> dict[str, Client]:
    return {
        "claude": Claude(home, environ, cwd),
        "codex": Codex(home, environ, cwd),
        "cursor": Cursor(home, environ, cwd),
    }


# --- commands -------------------------------------------------------------


def list_clients(
    clients: dict[str, Client], target: Target | None
) -> list[dict[str, Any]]:
    rows = []
    for name, client in clients.items():
        detected, via = client.detected()
        row: dict[str, Any] = {
            "client": name,
            "detected": detected,
            "via": via,
            "registered": False,
            "current": False,
            "registeredCommand": None,
            "scope": None,
        }
        try:
            found = [(server, client.registrations(server)) for server in SERVER_NAMES]
        except ClientError as exc:
            row["error"] = str(exc)
            found = []
        row["registrations"] = [
            {
                **reg.to_json(target, reg is client.effective(entries)),
                **({"name": server} if server != NAME else {}),
            }
            for server, entries in found
            for reg in entries
        ]
        primary = next(
            ((server, entries[0]) for server, entries in found if entries), None
        )
        if primary is not None:
            server, reg = primary
            shown = reg.shown(target)
            row.update(
                registered=True,
                current=target is not None and reg.matches(target),
                registeredCommand=shown["command"],
                scope=reg.scope,
            )
            if server != NAME:
                row["registeredName"] = server
            if "args" in shown:
                row["args"] = shown["args"]
            if reg.stale_env:
                row["staleEnv"] = list(reg.stale_env)
            if reg.extra_settings:
                row["extraSettings"] = list(reg.extra_settings)
            if reg.disabled:
                row["disabled"] = True
        rows.append(row)
    return rows


def report_legacy_install(client: Client, reg: Registration, target: Target) -> Result:
    result = Result(
        client.name,
        "unchanged",
        scope=reg.scope,
        name=RETIRED_NAME,
        **reg.shown(target),
    )
    if reg.disabled:
        result.error = (
            f"The existing {RETIRED_NAME} registration is disabled in {client.name}. "
            "Review MCP approvals and settings in the client."
        )
    elif stale := stale_registration(client, RETIRED_NAME, reg):
        result.error = (
            f"The existing {RETIRED_NAME} registration needs repair. {stale.reason}. "
            "Run `sai doctor` for repair instructions."
        )
    elif target.explicit_command and reg.argv != target.argv:
        result.error = (
            f"Kept the existing {RETIRED_NAME} registration and skipped the requested "
            "server command. Edit the existing registration in the client to change it."
        )
    if result.error is not None:
        result.action = "failed"
    return result


def install_one(client: Client, scope: str, target: Target, dry_run: bool) -> Result:
    result = Result(client.name, "failed", command=target.argv, scope=scope)
    try:
        if isinstance(client, Codex):
            client.check_scope(scope)
            client.require_exe()
        found = client.registrations()
        existing = next((r for r in found if r.scope == scope), None)
        if existing is None:
            legacy = client.effective(client.registrations(RETIRED_NAME))
            if legacy is not None:
                return report_legacy_install(client, legacy, target)
        elif isinstance(client, Claude) and existing.disabled:
            result.command = None
            raise ClientError(
                f"Claude Code has disabled the {existing.scope} sai registration. "
                "Review MCP approvals and settings in the client."
            )
        winner = client.effective(found)
        if (
            winner is not None
            and client.rank(winner.scope) < client.rank(scope)
            and not winner.matches(target)
        ):
            if winner.scope in client.read_only_scopes and isinstance(client, Codex):
                raise ClientError(client.project_message())
            result.shadowed_by = winner.scope
        if existing is not None and existing.matches(target):
            result.action = "unchanged"
            return result
        if isinstance(client, Claude) and existing is not None:
            return report_different_claude_entry(result, scope, target)
        action = "added" if existing is None else "replaced"
        if isinstance(client, (Codex, Cursor)):
            result.path = str(client.config_path(scope))
            steps: list[list[str]] = []
        else:
            steps = client.add_steps(scope, target)
            result.argv = steps
        if dry_run:
            result.action, result.would = "planned", action
            return result
        if isinstance(client, (Codex, Cursor)):
            client.write(scope, target)
        else:
            for argv in steps:
                run(argv)
        result.changed = True
        client.verify(scope, target)
        result.action = action
    except (ClientError, OSError) as exc:
        result.action, result.error = "failed", str(exc)
    return result


def uninstall_client(
    client: Client, scope: str | None, dry_run: bool, target: Target | None = None
) -> list[Result]:
    """Remove sai from one scope, or from every scope it is registered in.

    An effective entry in a scope sai does not edit is reported as failed,
    since removing the others leaves it running.
    """
    try:
        found = [(name, client.registrations(name)) for name in SERVER_NAMES]
    except ClientError as exc:
        return [Result(client.name, "failed", scope=scope, error=str(exc))]
    results = []
    for name, entries in found:
        selected = [reg for reg in entries if scope is None or reg.scope == scope]
        winner = client.effective(entries)
        if (
            winner is not None
            and winner.scope in client.read_only_scopes
            and winner not in selected
        ):
            selected.append(winner)
        results.extend(
            uninstall_one(client, reg.scope, reg, dry_run, target, name=name)
            for reg in selected
        )
    return results or [Result(client.name, "absent", scope=scope)]


def uninstall_one(
    client: Client,
    scope: str,
    existing: Registration,
    dry_run: bool,
    target: Target | None = None,
    *,
    name: str = NAME,
) -> Result:
    result = Result(
        client.name, "failed", scope=scope, name=name if name != NAME else None
    )
    try:
        shown = existing.shown(target)
        result.command, result.args = shown["command"], shown.get("args")
        if scope in client.read_only_scopes and isinstance(client, Codex):
            raise ClientError(client.project_message())
        if isinstance(client, (Codex, Cursor)):
            result.path = str(client.config_path(scope))
            steps: list[list[str]] = []
        else:
            steps = client.remove_steps(scope, name)
            result.argv = steps
        if dry_run:
            result.action, result.would = "planned", "removed"
            return result
        if isinstance(client, (Codex, Cursor)):
            client.write(scope, None, name=name)
        else:
            for argv in steps:
                run(argv)
                # Set per step: a remove that ran before a failed add still
                # changed the client.
                result.changed = True
        result.changed = True
        client.verify(scope, None, name=name)
        result.action = "removed"
    except (ClientError, OSError) as exc:
        result.action, result.error = "failed", str(exc)
    return result


@dataclass(frozen=True)
class StaleRegistration:
    name: str
    registration: Registration
    reason: str
    args: list[str] | None = None  # None means a manual edit is needed.


def after_server_options(args: list[str], *, root: bool = False) -> list[str] | None:
    """Consume options that leave the server command running."""
    index = 0
    while index < len(args):
        flag, equals, value = args[index].partition("=")
        if args[index] == "--":
            return args[index + 1 :]
        if root and args[index] == "--json":
            index += 1
            continue
        key = ENDPOINT_FLAGS.get(flag)
        if key is None and not (root and flag == "--as"):
            break
        if not equals:
            index += 1
            if index == len(args):
                return None
            value = args[index]
        if key is not None:
            try:
                settings.parse_value(key, value)
            except settings.SettingsError:
                return None
        index += 1
    return args[index:]


def serves_mcp(args: list[str]) -> bool:
    command = after_server_options(args, root=True)
    if not command or command[0] != "mcp":
        return False
    command = command[2:] if command[1:2] == ["--"] else command[1:]
    return command[:1] == ["serve"] and after_server_options(command[1:]) == []


def stale_registration(
    client: Client, name: str, reg: Registration
) -> StaleRegistration | None:
    if not reg.argv:
        return StaleRegistration(
            name, reg, "Sai registration has no executable command"
        )
    command, *args = reg.argv
    path = Path(command)
    binary = path.name.removesuffix(".exe")
    serve = serves_mcp(args)
    if binary == RETIRED_NAME:
        replacement = args if serve else ["mcp", "serve", *args]
        return StaleRegistration(
            name,
            reg,
            "Sai registration uses the retired executable",
            replacement if serves_mcp(replacement) else None,
        )
    repair = args if binary == NAME and serve else None
    on_path = shutil.which(command, path=client.environ.get("PATH"))
    cached = binary == NAME and inside(
        Path(on_path or command), uv_cache_dirs(client.environ, client.home)
    )
    if not on_path:
        return StaleRegistration(
            name, reg, "Sai registration points to a missing command", repair
        )
    if binary == NAME and not serve:
        return StaleRegistration(
            name, reg, "Sai registration does not run `sai mcp serve`"
        )
    if cached:
        return StaleRegistration(
            name, reg, "Sai registration points inside the uv cache", repair
        )
    if binary == NAME and not path.is_absolute():
        return StaleRegistration(
            name, reg, "Sai registration uses a relative command", repair
        )
    return None


def repair_entry(
    entries: MutableMapping[str, Any], stale: StaleRegistration
) -> MutableMapping[str, Any]:
    original = entries.get(stale.name)
    if not isinstance(original, MutableMapping):
        raise ClientError("The Sai registration changed concurrently")
    args = original.get("args") or []
    if (
        not isinstance(args, list)
        or [original.get("command"), *args] != stale.registration.argv
    ):
        raise ClientError("The Sai registration changed concurrently")
    return original


def edit_codex_config(
    path: Path,
    target: Target | None,
    *,
    stale: StaleRegistration | None = None,
    name: str = NAME,
) -> None:
    """Change only Sai's table, publishing the complete config in one rename."""
    name = stale.name if stale else name
    for _ in range(2):
        destination = path.resolve()
        seen = read_bytes(destination)
        try:
            source = seen.decode() if seen is not None else ""
            rendered = edit_codex_source(source, target, stale, name)
            if unrelated_codex_settings(source, name) != unrelated_codex_settings(
                rendered, name
            ):
                raise ClientError(
                    "Could not preserve settings outside the Sai registration"
                )
        except (ValueError, TOMLKitError, TypeError):
            raise ClientError("Could not edit Codex config.toml") from None
        data = rendered.encode()
        if data == seen or replace_bytes(path, data, seen, destination=destination):
            return
    raise ClientError("Codex config.toml changed while Sai was writing it")


def edit_codex_source(
    source: str, target: Target | None, stale: StaleRegistration | None, name: str
) -> str:
    doc = tomlkit.parse(source)
    entries = doc.get("mcp_servers", {})
    if not isinstance(entries, MutableMapping):
        raise ClientError("Codex mcp_servers must be a table")
    if target is None:
        if entries.pop(name, None) is None:
            return source
        return keep_table_separators(source, doc.as_string())

    original = repair_entry(entries, stale) if stale else entries.get(NAME)
    entry: MutableMapping[str, Any]
    if isinstance(original, MutableMapping):
        entry = original
    else:
        entry = (
            tomlkit.inline_table()
            if isinstance(entries, InlineTable)
            else tomlkit.table()
        )
    if not stale:
        for key in list(entry):
            if key not in ("command", "args") and (key != "env" or not target.env):
                del entry[key]
        if target.env:
            env = tomlkit.inline_table()
            env.update(target.env)
            entry["env"] = env
    if entry.get("command") != target.argv[0]:
        entry["command"] = target.argv[0]
    if entry.get("args") != target.argv[1:]:
        entry["args"] = target.argv[1:]
    if original is entry:
        return keep_table_separators(source, doc.as_string())
    if isinstance(entries, InlineTable):
        entries[NAME] = entry
        return doc.as_string()
    # Appending a new header to the original text leaves root-level dotted
    # keys in place. Inserting a header through the AST can capture them.
    table = tomlkit.table(is_super_table=True)
    table[NAME] = entry
    addition = tomlkit.document()
    addition["mcp_servers"] = table
    separator = "" if not source or source.endswith("\n") else "\n"
    return source + separator + addition.as_string()


def keep_table_separators(source: str, rendered: str) -> str:
    """Keep comments and whitespace between a removed table and its neighbors."""
    before = source.splitlines(keepends=True)
    after = rendered.splitlines(keepends=True)
    result = []
    for tag, start, end, left, right in SequenceMatcher(
        None, before, after
    ).get_opcodes():
        result.extend(after[left:right])
        if tag in ("delete", "replace"):
            tail = end
            while tail > start and (
                not before[tail - 1].strip()
                or before[tail - 1].lstrip().startswith("#")
            ):
                tail -= 1
            trivia = before[tail:end]
            if trivia and after[max(left, right - len(trivia)) : right] != trivia:
                result.extend(trivia)
    return "".join(result)


def unrelated_codex_settings(source: str, name: str) -> dict[str, Any]:
    """Check the resulting TOML independently of the editor's in-memory tree."""
    doc = tomllib.loads(source)
    if entries := doc.get("mcp_servers"):
        entries.pop(name, None)
    if doc.get("mcp_servers") == {}:
        del doc["mcp_servers"]
    return doc


@dataclass
class ClientConfig:
    entries: MutableMapping[str, Any]
    render: Callable[[], str]
    seen: bytes | None
    destination: Path


def read_client_config(client: Client, scope: str, path: Path) -> ClientConfig:
    """Read an editable scope. Keep the full entry local to the repair."""
    destination = path.resolve()
    seen = read_bytes(destination)
    raw = seen.decode() if seen is not None else ""
    data = json.loads(raw) if seen is not None else {}
    if not isinstance(data, dict):
        raise ClientError("Expected an MCP config object")
    holder = data
    if scope == "local":
        projects = data.get("projects", {})
        holder = projects.get(str(client.cwd)) if isinstance(projects, dict) else None
        if not isinstance(holder, dict):
            raise ClientError("Could not find the local MCP config")
    entries = holder.setdefault("mcpServers", {})

    def render() -> str:
        return json.dumps(data, indent=2) + "\n"

    if not isinstance(entries, MutableMapping):
        raise ClientError("Expected a table of MCP servers")
    return ClientConfig(entries, render, seen, destination)


def repair_path(client: Client, scope: str) -> Path:
    if isinstance(client, Claude):
        if scope == "project" and client.cwd is None:
            raise ClientError("No working directory for the project registration")
        if scope == "project":
            assert client.cwd is not None
            return client.cwd / ".mcp.json"
        return client.config_path()
    assert isinstance(client, Cursor)
    return client.config_path(scope)


def replace_registration(
    client: Client, stale: StaleRegistration, target: Target
) -> None:
    """Automatically repair an existing Codex or Cursor entry."""
    scope, name = stale.registration.scope, stale.name
    if stale.args is None or scope in client.read_only_scopes:
        raise ClientError("This registration needs a manual edit")
    if isinstance(client, Codex):
        expected = Target([normalize_launcher(target.argv[0]), *stale.args])
        path = client.config_path(scope)
        edit_codex_config(path, expected, stale=stale)
        actual = client.get(client.neutral_dir(), "user", name)
        if actual is None or not actual.matches_command(expected):
            raise ClientError("Codex did not load the repaired Sai registration")
        return
    if not isinstance(client, Cursor):
        raise ClientError("Only Codex and Cursor support automatic repair")
    replace_json_registration(client, stale, target.argv[0], stale.args)


def replace_json_registration(
    client: Claude | Cursor, stale: StaleRegistration, launcher: str, args: list[str]
) -> None:
    scope, name = stale.registration.scope, stale.name
    path = repair_path(client, scope)
    config = read_client_config(client, scope, path)
    if isinstance(client, Claude):
        entry = config.entries.get(name)
        if (
            not isinstance(entry, MutableMapping)
            or entry_registration(entry, scope, stale.registration.disabled)
            != stale.registration
        ):
            raise ClientError("The Sai registration changed concurrently")
    else:
        entry = repair_entry(config.entries, stale)
    entry["command"], entry["args"] = launcher, args
    # Publish a complete entry in one rename. In particular, Claude must
    # never observe a replacement that is missing its profile or endpoints.
    if not replace_bytes(
        path, config.render().encode(), config.seen, destination=config.destination
    ):
        raise ClientError("The Sai registration changed concurrently")


def claude_repair_args(stale: StaleRegistration) -> list[str] | None:
    if stale.args is not None:
        return stale.args
    if not stale.registration.argv:
        return None
    args = stale.registration.argv[1:]
    if serves_mcp(args):
        return args
    replacement = ["mcp", "serve", *args]
    return replacement if serves_mcp(replacement) else None


def repair_claude_existing(
    client: Claude,
    scope: str,
    name: str,
    target: Target,
    dry_run: bool,
    *,
    reset_args: bool,
) -> Result:
    """Explicitly repair a named entry. Refuse if the entry disappeared."""
    result = Result(client.name, "failed", scope=scope, name=name)
    try:
        existing = client.registration(scope, name)
        if existing is None:
            raise ClientError(f"No existing {name} registration in {scope} scope")
        if name == NAME:
            if existing.disabled:
                raise ClientError(
                    f"Claude Code has disabled the {scope} sai registration. "
                    "Review MCP approvals and settings in the client."
                )
            if not existing.matches(target):
                return report_different_claude_entry(result, scope, target)
            result.action = "unchanged"
            return result
        if not existing.stdio:
            raise ClientError("The existing Claude Code entry uses a remote transport")
        stale = stale_registration(client, name, existing)
        if stale is None:
            result.action = "unchanged"
            return result
        args = target.argv[1:] if reset_args else claude_repair_args(stale)
        if args is None:
            raise ClientError(
                "Unsupported server arguments. Use --reset-args to replace them with the defaults."
            )
        if dry_run:
            result.action, result.would = "planned", "replaced"
        else:
            replace_json_registration(client, stale, target.argv[0], args)
            result.action, result.changed = "replaced", True
    except ClientError as exc:
        result.error = str(exc)
    except (OSError, ValueError):
        result.error = "Could not edit the Claude Code registration"
    return result


def check_claude_differences(
    found: list[tuple[str, Registration]],
    stale: list[StaleRegistration],
    target: Target | None,
    legacy_name: bool,
) -> RegistrationCheck | None:
    stale_by_scope = {(issue.name, issue.registration.scope): issue for issue in stale}
    repairs = []
    differences = []
    manual = []
    for name, reg in found:
        issue = stale_by_scope.get((name, reg.scope))
        if name == NAME:
            if issue is None and (target is None or reg.matches(target)):
                continue
            if reg.disabled:
                manual.append("Review MCP approvals and settings in Claude Code.")
            elif target is None:
                manual.append("Install a stable Sai launcher on PATH.")
            else:
                repairs.append(
                    ClaudeRepair(
                        reg.scope,
                        name,
                        replace_entry=True,
                        use_profile="SAI_HOME" in target.env,
                    )
                )
        elif issue is None:
            continue
        elif reg.stdio:
            repairs.append(
                ClaudeRepair(reg.scope, name, claude_repair_args(issue) is None)
            )
        else:
            manual.append("Review the remote Sai registration in Claude Code.")
        reason = (
            issue.reason
            if issue is not None
            else "Sai registration has a different SAI_HOME setting"
            if target is not None and not reg.matches_sai_home(target.env)
            else "Sai registration differs from the requested settings"
        )
        differences.append(f"{reg.scope} {name}: {reason}")
    if not differences:
        return None
    entries = tuple(repairs)
    if entries:
        manual.insert(0, claude_repair_hint(entries))
    return RegistrationCheck(
        "claude",
        "fail" if stale else "warn",
        ". ".join(differences),
        " ".join(dict.fromkeys(manual)),
        legacy_name=legacy_name,
        manual_repairs=entries,
    )


def check_registration(
    client: Client,
    target: Target | None,
    *,
    fix: bool,
    compare_profile: bool,
) -> RegistrationCheck:
    """Inspect recognized Sai registrations without exposing client data."""
    name = client.name
    install = f"Run `sai mcp install {name}`"
    if isinstance(client, Codex) and not client.exe():
        return RegistrationCheck(
            name, "fail", "Could not inspect Sai registration", "Put `codex` on PATH"
        )
    try:
        found = [
            (server, reg)
            for server in SERVER_NAMES
            for reg in client.registrations(server)
        ]
        inspected = [
            (reg, stale_registration(client, server, reg)) for server, reg in found
        ]
    except (ClientError, OSError, ValueError):
        return RegistrationCheck(
            name, "fail", "Could not read the client's Sai registrations", install
        )
    if not found:
        return RegistrationCheck(name, "warn", "Sai is not registered", install)
    stale = [issue for _, issue in inspected if issue is not None]
    legacy_name = any(server == RETIRED_NAME for server, _ in found)
    legacy_warning = f"Sai retains its existing {RETIRED_NAME} registration name"
    expected_env = server_env(client.environ) if compare_profile else {}
    if isinstance(client, Claude):
        claude_check = check_claude_differences(found, stale, target, legacy_name)
        if claude_check is not None:
            return claude_check
    different_profile = compare_profile and any(
        not reg.matches_sai_home(expected_env) for reg, _ in inspected
    )
    pinned = target is not None and any(
        reg.argv
        and Path(reg.argv[0]).is_absolute()
        and issue is None
        and Path(reg.argv[0]).resolve() != Path(target.argv[0]).resolve()
        for reg, issue in inspected
    )
    changes, failures = [], []
    for issue in stale:
        scope = issue.registration.scope
        if issue.args is None or scope in client.read_only_scopes:
            failures.append(
                (issue.reason, "Edit the Sai registration in this client's config")
            )
        elif target is None:
            failures.append((issue.reason, "Install a stable Sai launcher on PATH"))
        elif not fix:
            failures.append((issue.reason, "Run `sai doctor` with fixes enabled"))
        else:
            try:
                replace_registration(client, issue, target)
            except (ClientError, OSError, ValueError, TOMLKitError, TypeError):
                failures.append(
                    (
                        f"{issue.reason}. Could not repair the {scope} registration",
                        "Check this client's Sai registration and rerun `sai doctor`",
                    )
                )
            else:
                changes.append(f"Repaired the Sai registration in {scope} scope")
    if changes and isinstance(client, Codex) and not failures:
        # A project entry identical to the old user entry only becomes
        # visible to the adapter after the user entry changes.
        try:
            if any(
                stale_registration(client, server, reg) is not None
                for server in SERVER_NAMES
                for reg in client.registrations(server)
            ):
                failures.append(
                    (
                        "A stale Codex project registration is still active",
                        "Edit the Sai registration in .codex/config.toml",
                    )
                )
        except (ClientError, OSError, ValueError):
            failures.append(
                (
                    "Could not verify the Codex registration",
                    "Check the Sai registration and rerun `sai doctor`",
                )
            )
    if failures:
        summary, hint = failures[0]
        if legacy_name:
            summary += f". {legacy_warning}"
        return RegistrationCheck(
            name,
            "fail",
            summary,
            hint,
            tuple(changes),
            legacy_name=legacy_name,
        )
    if pinned or different_profile or legacy_name:
        warnings = []
        if pinned:
            warnings.append("Sai uses a pinned launcher from a different install")
        if different_profile:
            warnings.append("Sai registration has a different SAI_HOME setting")
        if legacy_name:
            warnings.append(legacy_warning)
        return RegistrationCheck(
            name,
            "warn",
            ". ".join(warnings),
            "Check SAI_HOME in the client registration" if different_profile else None,
            changes=tuple(changes),
            pinned=pinned,
            legacy_name=legacy_name,
        )
    if not changes:
        return RegistrationCheck(name, "pass", "Sai registration is current")
    return RegistrationCheck(
        name, "fixed", "Sai registration repaired", changes=tuple(changes)
    )


def check_registrations(
    target: Target | None,
    *,
    fix: bool,
    cwd: Path | None,
    compare_profile: bool = False,
) -> list[RegistrationCheck]:
    """Report only for detected clients, without running any registered server."""
    clients = make_clients(Path.home(), os.environ, cwd)
    return [
        check_registration(
            client,
            target,
            fix=fix,
            compare_profile=compare_profile,
        )
        for client in clients.values()
        if client.detected()[0]
    ]


def summarize(results: list[Result]) -> dict[str, Any]:
    return {
        "results": [r.to_json() for r in results],
        "reconnectNeeded": any(r.changed for r in results),
    }


def running_prefix() -> str:
    return sys.prefix
