"""Sai trader CLI with Keeper queries, intents, and live feeds."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from functools import wraps
from importlib import metadata
from pathlib import Path
from typing import Any

import click

from . import mcp_install, skill
from .core import (
    clipboard,
    confirmation,
    intents,
    keeper,
    log,
    log_queries,
    overview,
    reconcile,
    storage,
    stream,
    trading,
    update,
    updater,
    wait,
)
from .core import session as sessions
from .core import settings as sai_settings
from .core.errors import Code, ExitCode, McpStdioError, SaiError, SessionError, describe
from .utils import overview_format
from .utils.repl_skin import ReplSkin

PACKAGE = "sai-cli"


def package_version() -> str:
    """Return the installed package version; pyproject.toml sets it."""
    return metadata.version(PACKAGE)


def print_skill(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """`sai --skill`: the same output as `sai skill show`."""
    if not value or ctx.resilient_parsing:
        return
    click.echo(skill.skill_bytes(), nl=False)
    ctx.exit()


def emit(ctx: click.Context, value: Any, *, wait_receipt: bool = False) -> None:
    encoded = json.dumps(value, indent=None if ctx.obj["json"] else 2, default=str)
    if wait_receipt:
        ctx.meta["wait_output_started"] = True
    click.echo(encoded)


# Global flags that override a setting: option name, setting key.
GLOBAL_FLAGS = (
    ("--endpoint", "keeper.endpoint"),
    ("--ws-endpoint", "keeper.ws_endpoint"),
    ("--lcd-endpoint", "lcd.endpoint"),
)
LCD_TRADE_COMMANDS = frozenset({"trade margin", "trade partial-close"})


def settings(ctx: click.Context) -> sai_settings.Settings:
    """Resolve settings once per command: flags, environment, config file,
    then mainnet defaults."""
    obj = ctx.find_root().obj
    if obj.get("settings") is None:
        obj["settings"] = sai_settings.load(obj["flags"], warn=warn)
    return obj["settings"]


def keeper_url(ctx: click.Context) -> str:
    return settings(ctx).endpoint


def session(ctx: click.Context) -> dict[str, str]:
    return sessions.load_active_session(keeper_url(ctx), ctx.obj["as_addr"])


def command_path(group: click.Group, args: list[str]) -> list[str]:
    """Find the command names past the options and their values."""
    names, _, _ = _command_parts(group, args)
    return names


def _command_parts(
    group: click.Group, args: list[str]
) -> tuple[list[str], int, frozenset[str]]:
    """Find command names, the argument offset, and explicit setting flags."""
    names: list[str] = []
    current: click.Command = group
    tail = 0
    flag_keys: set[str] = set()
    endpoint_options = dict(GLOBAL_FLAGS)
    options = True
    indexed = iter(enumerate(args))
    for index, token in indexed:
        if options and token == "--":
            options = False
            continue
        if options and token.startswith("-"):
            option_name, equals, _ = token.partition("=")
            option = next(
                (
                    param
                    for param in current.params
                    if isinstance(param, click.Option) and option_name in param.opts
                ),
                None,
            )
            if option is not None and not option.is_flag and not option.count:
                supplied = bool(equals) or next(indexed, None) is not None
                if supplied and (key := endpoint_options.get(option_name)):
                    flag_keys.add(key)
            continue
        if not isinstance(current, click.Group):
            continue
        found = current.commands.get(token)
        if found is None:
            break
        names.append(token)
        tail = index + 1
        current = found
        options = True
    return names, tail, frozenset(flag_keys)


def audit_argv(args: list[str], config_set_at: int | None) -> list[log.Arg]:
    """Prepare command.start arguments before Click validates their values."""
    safe = args.copy()
    if config_set_at is not None:
        key_at = config_set_at
        if safe[key_at : key_at + 1] == ["--"]:
            key_at += 1
        value_at = key_at + 1
        if key_at == config_set_at and safe[value_at : value_at + 1] == ["--"]:
            value_at += 1
        if value_at < len(safe):
            key = safe[key_at]
            spec = sai_settings.BY_KEY.get(key)
            if spec is None:
                safe[value_at] = "[redacted]"
            elif spec.kind == "url":
                safe[value_at] = audit_origin(key, safe[value_at])
            else:
                try:
                    sai_settings.parse_value(key, safe[value_at])
                except sai_settings.InvalidSetting:
                    safe[value_at] = "[redacted]"

    endpoint_options = dict(GLOBAL_FLAGS)
    indexed = iter(enumerate(safe))
    for index, token in indexed:
        option, equals, value = token.partition("=")
        if key := endpoint_options.get(option):
            if equals:
                safe[index] = f"{option}={audit_origin(key, value)}"
            elif next(indexed, None) is not None:
                safe[index + 1] = audit_origin(key, safe[index + 1])
    return log.argv(safe)


def audit_origin(key: str, value: str) -> str:
    try:
        sai_settings.check_value(key, value)
    except sai_settings.InvalidSetting:
        return "[redacted]"
    origin = log.origin(value)
    return "[redacted]" if origin == log.REDACTED_URL else origin


# Options that print something and exit before any command runs.
INFO_FLAGS = ("--help", "--version", "--skill")


def json_requested(argv: Sequence[str]) -> bool:
    """Find the root --json flag even if Click fails before invoking main."""
    valued = {option for option, _ in GLOBAL_FLAGS} | {"--as"}
    args = iter(argv)
    for arg in args:
        if arg == "--" or not arg.startswith("-"):
            break
        if arg == "--json":
            return True
        if arg in valued:
            next(args, None)
    return False


def cli_failure(exc: Exception) -> SaiError:
    if isinstance(exc, sai_settings.InvalidSetting):
        return SaiError(str(exc), Code.INVALID_REQUEST, exit_code=ExitCode.USAGE)
    if isinstance(exc, click.ClickException):
        return SaiError(
            exc.format_message(),
            Code.INVALID_REQUEST,
            exit_code=(
                ExitCode.USAGE if isinstance(exc, click.UsageError) else ExitCode.ERROR
            ),
        )
    if isinstance(exc, click.Abort):
        return SaiError("Aborted", Code.INVALID_REQUEST)
    if isinstance(exc, mcp_install.InstallError):
        return SaiError(
            str(exc), exit_code=ExitCode.UNSUPPORTED, hint="sai mcp install --help"
        )
    return describe(exc)


def redact_error(value: Any) -> Any:
    """Redact stored tokens in an error without changing its JSON structure."""
    if isinstance(value, dict):
        return {log.redact(key): redact_error(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_error(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return log.redact(str(value))


def show_failure(exc: Exception, failure: SaiError, json_output: bool) -> None:
    if isinstance(exc, WaitRecovery):
        result = exc.result.copy()
        if json_output:
            result["error"] = redact_error(failure.as_dict())
        click.echo(
            json.dumps(result, indent=None if json_output else 2, default=str),
            err=exc.output_started,
        )
        return
    if json_output and not isinstance(failure, McpStdioError):
        error = failure.as_dict()
        for key in ("message", "hint", "details"):
            if key in error:
                error[key] = redact_error(error[key])
        click.echo(json.dumps({"error": error}, separators=(",", ":")))
    elif isinstance(exc, click.UsageError):
        click.UsageError(log.redact(failure.message), exc.ctx).show()
    elif isinstance(exc, click.Abort):
        click.echo("Aborted!", err=True)
    else:
        message = failure.message
        if isinstance(failure, storage.IntentReservationError) and failure.hint:
            message += f" Check the local reservation with `{failure.hint}`."
        click.ClickException(log.redact(message)).show()


def unlogged(names: list[str], argv: list[str]) -> bool:
    """True for runs that write no log lines: reading the log, which should
    not add to it; `sai doctor --no-fix`, which promises to change nothing
    under $SAI_HOME; and help, version, and skill output, as `--skill` or
    as `sai skill show`."""
    return (
        names[:1] == ["logs"]
        or names[:2] == ["skill", "show"]
        or (names == ["doctor"] and "--no-fix" in argv)
        or any(flag in argv for flag in INFO_FLAGS)
    )


class HelpOnMissingInput(click.Command):
    """Show full help when Click requires input and the command received none."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        bare = not args
        try:
            return super().parse_args(ctx, args)
        except click.MissingParameter:
            if not bare or ctx.find_root().params.get("json_output"):
                raise
            click.echo(ctx.get_help(), err=True, color=ctx.color)
            ctx.exit(ExitCode.USAGE)


class SaiGroup(click.Group):
    command_class = HelpOnMissingInput
    group_class = type


class LoggedGroup(SaiGroup):
    """The root group: logs `command.start` and `command.end` for each run.

    Events written while the command runs carry its `command`. `sai mcp
    serve` logs to the `mcp` surface and `sai update` to `update`.
    """

    group_class = SaiGroup

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        argv = list(sys.argv[1:] if args is None else args)
        names, tail, flag_keys = _command_parts(self, argv)
        quiet = unlogged(names, argv)
        command = " ".join(names) or ("repl" if not argv else None)
        surface = {"mcp serve": "mcp", "update": "update"}.get(command or "", "cli")
        started = time.monotonic()
        exit_code: int = ExitCode.OK
        failure: SaiError | None = None
        start_logged = False
        obj = extra.get("obj")
        if not isinstance(obj, dict):
            obj = extra["obj"] = {}
        obj["command"] = command

        def log_start() -> None:
            nonlocal start_logged
            if not quiet and not start_logged:
                log.event(
                    "command.start",
                    argv=audit_argv(
                        argv[:64], tail if names == ["config", "set"] else None
                    ),
                )
                start_logged = True

        with (
            log.setting_flags(flag_keys),
            log.silenced()
            if quiet
            else log.bind(surface=surface, command=command, commandId=uuid.uuid4().hex),
        ):
            if command == "doctor" and not quiet:
                # Let doctor report directory creation before logging creates
                # the profile to record its first event.
                obj["doctor_log_start"] = log_start
            else:
                log_start()
            try:
                result = super().main(
                    args,
                    prog_name,
                    complete_var,
                    standalone_mode=False,
                    windows_expand_args=windows_expand_args,
                    **extra,
                )
            except Exception as exc:
                failure = cli_failure(exc)
                exit_code = failure.exit_code
                if not standalone_mode:
                    raise
                show_failure(exc, failure, obj.get("json", json_requested(argv)))
            else:
                # With standalone mode disabled, Click returns explicit exit codes.
                if isinstance(result, int):
                    exit_code = result
                if not standalone_mode:
                    return result
            finally:
                if not quiet:
                    log_start()
                    log.event(
                        "command.end",
                        "info" if exit_code == ExitCode.OK else "warn",
                        durationMs=round((time.monotonic() - started) * 1000),
                        exitCode=exit_code,
                        code=failure.code if failure is not None else None,
                        error=log.untrusted(failure.message)
                        if failure is not None
                        else None,
                        intentKey=failure.intent_key if failure is not None else None,
                    )
        raise SystemExit(exit_code)


@click.group(cls=LoggedGroup, invoke_without_command=True)
@click.version_option(package_name=PACKAGE, prog_name="sai")
@click.option(
    "--skill",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=print_skill,
    help="Print the agent skill and exit; same as `sai skill show`.",
)
@click.option(
    "--endpoint",
    help="Keeper GraphQL HTTP endpoint. Overrides SAI_ENDPOINT and"
    " keeper.endpoint in the config file.",
)
@click.option(
    "--ws-endpoint",
    help="Keeper WebSocket endpoint. Overrides SAI_WS_ENDPOINT and"
    " keeper.ws_endpoint; derived from the Keeper endpoint when unset.",
)
@click.option(
    "--lcd-endpoint",
    help="Nibiru REST endpoint. Overrides SAI_LCD_ENDPOINT and lcd.endpoint.",
)
@click.option(
    "--json", "json_output", is_flag=True, help="Compact machine-readable JSON."
)
@click.option(
    "--as",
    "as_addr",
    metavar="ADDR",
    help="Run this command as another connected account instead of the"
    " active one. See `sai auth list`.",
)
@click.pass_context
def main(
    ctx: click.Context,
    endpoint: str | None,
    ws_endpoint: str | None,
    lcd_endpoint: str | None,
    json_output: bool,
    as_addr: str | None,
) -> None:
    """Inspect Sai markets and account data, and submit trading intents.

    Settings come from flags, then SAI_* environment variables, then
    $SAI_HOME/config.toml (see `sai config`), then mainnet defaults.
    """
    ctx.ensure_object(dict)
    passed = (endpoint, ws_endpoint, lcd_endpoint)
    flags = {key: value for (_, key), value in zip(GLOBAL_FLAGS, passed, strict=True)}
    ctx.obj.update(flags=flags, settings=None, json=json_output, as_addr=as_addr)
    if (
        ctx.invoked_subcommand
        not in {"account", "auth", "doctor", "mcp", "logs", "skill"}
        and ctx.obj.get("command") not in LCD_TRADE_COMMANDS
    ):
        session_notice(ctx, as_addr)
    if ctx.invoked_subcommand != "account" and auto_update_applies(
        ctx.invoked_subcommand, json_output
    ):
        updater.tick(warn)
    if ctx.invoked_subcommand is None:
        if json_output:
            raise click.UsageError("Pass a command when using --json.", ctx)
        repl(ctx)


def session_notice(ctx: click.Context, addr: str | None) -> None:
    """Warn on a terminal without touching piped output or MCP stdio."""
    if ctx.obj["json"] or not (sys.stdin.isatty() and sys.stderr.isatty()):
        return
    try:
        stored = storage.load_session(addr)
        if stored.get("endpoint") == keeper_url(ctx):
            show_expiry_notice(ctx, sessions.expiry_fields(stored))
    except (ValueError, click.ClickException):
        # This notice is optional. The command handles its own settings and session.
        return


def show_expiry_notice(ctx: click.Context, fields: Mapping[str, Any]) -> None:
    if (
        fields.get("expiryWarning")
        and not ctx.obj["json"]
        and sys.stdin.isatty()
        and sys.stderr.isatty()
    ):
        warn(sessions.expiry_notice_text(fields))


# Commands that never start or announce an automatic update. `mcp` serves
# agents over stdio; `update` and `doctor` report on updates themselves;
# `config` must be able to turn updates off before one starts; `logs` and
# `skill` only print.
NO_AUTO_UPDATE = frozenset({"mcp", "update", "doctor", "config", "logs", "skill"})


def auto_update_applies(
    subcommand: str | None,
    json_output: bool,
    environ: Mapping[str, str] | None = None,
    terminal: bool | None = None,
) -> bool:
    """True for a command a person runs at a terminal, outside CI."""
    env = os.environ if environ is None else environ
    if json_output or subcommand in NO_AUTO_UPDATE or env.get("CI"):
        return False
    if terminal is None:
        terminal = sys.stdin.isatty() and sys.stderr.isatty()
    return terminal


def forwarded_flags(obj: dict[str, Any]) -> list[str]:
    """The global flags the person passed when starting the REPL, so each
    command still reads the config file for the rest."""
    args: list[str] = []
    for option, key in GLOBAL_FLAGS:
        value = obj["flags"].get(key)
        if value is not None:
            args += [option, value]
    if obj["json"]:
        args.append("--json")
    if obj.get("as_addr"):
        args += ["--as", obj["as_addr"]]
    return args


def repl_args(line: str) -> list[str]:
    """Turn one REPL line into CLI arguments; `help [COMMAND...]` shows help."""
    args = shlex.split(line)
    if args and args[0] in ("help", "?"):
        return [*args[1:], "--help"]
    return args


def repl(ctx: click.Context, pt_input: Any = None, pt_output: Any = None) -> None:
    """Run the interactive shell.

    History is kept in $SAI_HOME/trader/history when stdin is a terminal or a
    prompt_toolkit input is supplied. Ctrl-C cancels the current line or
    command and returns to the prompt; Ctrl-D or `exit` leaves.
    """
    skin = ReplSkin("sai", version=package_version())
    skin.print_banner()
    click.echo("Enter a command, 'help', or 'exit'.")
    pt_session = (
        skin.create_prompt_session(input=pt_input, output=pt_output)
        if pt_input is not None or sys.stdin.isatty()
        else None
    )
    while True:
        try:
            line = skin.get_input(pt_session)
        except KeyboardInterrupt:
            continue
        except EOFError:
            click.echo()
            break
        if line in ("exit", "quit"):
            break
        args: list[str] = []
        command_obj: dict[str, Any] = {}
        try:
            args = repl_args(line)
            if args:
                main.main(
                    args=[*forwarded_flags(ctx.obj), *args],
                    prog_name="sai",
                    standalone_mode=False,
                    obj=command_obj,
                )
        except (click.Abort, KeyboardInterrupt):
            click.echo("Interrupted", err=True)
        except Exception as exc:  # noqa: BLE001 - keep the REPL open
            show_failure(
                exc, cli_failure(exc), command_obj.get("json", json_requested(args))
            )


@main.group()
def auth() -> None:
    """Connect, list, switch, and remove Sai accounts on this machine."""


@auth.command("login")
@click.argument("name")
@click.option(
    "--addr",
    metavar="ADDR",
    help="EVM addr you expect NAME to hold. Sign-in stops before signing if"
    " ud or the keystore file reports another addr.",
)
@click.pass_context
def login(ctx: click.Context, name: str, addr: str | None) -> None:
    """Connect the account whose key is NAME by signing Keeper's challenge.

    NAME is a Foundry keystore in ~/.foundry/keystores (see `cast wallet
    list`). sai fetches Keeper's sign-in challenge, checks that it is a Sai
    `UserTradingSession` for that account, has the key sign it, and stores
    the session Keeper returns. The session lasts as long as Keeper issues
    it, 30 days in production, and lets sai trade for that account.

    If the `ud` tool is on PATH and has a wallet named NAME, sai signs
    through `ud evm wallet run`, which supplies the keystore password.
    Otherwise `cast wallet sign` asks for it.

    To connect without a local key, copy a token from Sai Web and run `sai
    auth import`.

    \b
    Examples:
      sai auth login trading-bot
          Sign with ~/.foundry/keystores/trading-bot; cast asks for its
          password.
      sai auth login trading-bot --addr 0xYOUR_ADDR
          Same, but stop if the keystore records another addr.
      sai auth import
          Use a session token copied from Sai Web instead of a key.
    """
    endpoint = keeper_url(ctx)
    try:
        result = signed_login(endpoint, name, addr)
        result["endpoint"] = endpoint
        result["keyName"] = name
        storage.save_session(result)
        emit(ctx, sessions.summary(result))
    except NeedsTerminal as exc:
        json_flag = ("--json",) if ctx.obj["json"] else ()
        chosen = ("--addr", addr) if addr else ()
        command = storage.keeper_command_hint(
            endpoint, *json_flag, "auth", "login", name, *chosen
        )
        raise SaiError(
            f"{exc} Run `{command}` in a terminal.",
            Code.INVALID_REQUEST,
            hint=command,
            exit_code=ExitCode.UNSUPPORTED,
        ) from exc


@auth.command("renew")
@click.argument("addr", required=False)
@click.pass_context
def renew(ctx: click.Context, addr: str | None) -> None:
    """Renew ADDR's session with its saved key name, or renew the active account."""
    chosen = addr or ctx.obj["as_addr"]
    try:
        stored = storage.load_session(chosen)
        endpoint = keeper_url(ctx)
        if stored.get("endpoint") != endpoint:
            raise SessionError("Stored session belongs to another Keeper endpoint")
        if not keeper.is_address(stored["evmAddress"]):
            raise ValueError(sessions.INVALID_RENEWAL_ADDRESS)
        name = stored.get("keyName")
        if not name:
            fields = sessions.renewal_fields(stored)
            command = fields["renewalCommand"]
            guidance = fields.get("renewalGuidance", "")
            if stored.get("source") == "imported":
                message = (
                    f"This session was imported. Run `{command}` with a fresh token."
                )
            else:
                message = f"This session has no saved key name. Run `{command}`."
            raise ValueError(f"{message} {guidance}".rstrip())
        result = signed_login(endpoint, name, stored["evmAddress"])
        result.update(endpoint=endpoint, keyName=name)
        storage.replace_session(stored, result)
        emit(ctx, sessions.summary(result))
    except NeedsTerminal as exc:
        fields = sessions.renewal_fields(stored)
        command = fields["renewalCommand"]
        guidance = fields.get("renewalGuidance", "")
        raise SaiError(
            f"{exc} Run `{command}` in a terminal. {guidance}".rstrip(),
            Code.SESSION_UNAVAILABLE,
        ) from exc


class NeedsTerminal(ValueError):
    """Sign-in can continue only where cast can prompt for a password."""


def keystore_path(name: str) -> Path:
    return Path.home() / ".foundry" / "keystores" / name


def keystore_addr(name: str) -> str | None:
    """Read the plaintext `address` a Foundry keystore records, if any.

    Reading it needs no password. The field is not bound to the encrypted
    key, so an edited file can pass the --addr check; Keeper then rejects
    the signature, and no session is stored.
    """
    try:
        value = json.loads(keystore_path(name).read_text(encoding="utf-8"))
        raw = value["address"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(raw, str):
        return None
    addr = raw if raw.lower().startswith("0x") else "0x" + raw
    return addr if keeper.is_address(addr) else None


def checked_address(found: str, expected: str | None) -> str:
    """Return FOUND, or fail when the --addr cross-check disagrees."""
    if expected is not None and expected.lower() != found.lower():
        raise ValueError(f"--addr {expected} does not match the wallet addr {found}")
    return found


def ud_state_hint(wallet: str, state: str | None) -> str:
    """Explain why ud cannot sign with WALLET and how to fix it."""
    prefix = f"ud wallet {wallet} has Cast keystore state {state or 'unknown'}"
    if state == "missing":
        return f"{prefix}; run `ud evm wallet sync {wallet}` to create it"
    if state == "name-conflict":
        return (
            f"{prefix}: another keystore already uses that name; rename it,"
            f" then run `ud evm wallet sync {wallet}`"
        )
    return f"{prefix}; check `ud evm wallet show {wallet}`"


def signed_login(endpoint: str, name: str, addr: str | None) -> dict[str, str]:
    """Sign in with the key named NAME.

    A ud wallet that can sign goes first, because ud keeps the password for
    the keystore of the same name and signs without a prompt. If ud then
    fails to sign, for example because the keystore has another password,
    cast signs with the same keystore and asks for the password. Without a
    ud wallet that can sign, the Foundry keystore NAME signs through cast.

    When ud and the keystore hold different accounts under NAME, nothing
    signs unless --addr picks the keystore's account. The same holds when ud
    is installed but its lookup fails, since it may hold another account.
    """
    if addr is not None and not keeper.is_address(addr):
        raise ValueError(f"Not an EVM addr: {addr}")
    has_keystore = keystore_path(name).is_file()
    try:
        found = keeper.ud_wallet(name)
    except keeper.UdUnavailable as exc:
        if not has_keystore:
            raise ValueError(f"{exc}; check that ud works, then retry") from exc
        found_addr = keystore_addr(name) or keeper.cast_address(name)
        if addr is None or addr.lower() != found_addr.lower():
            raise ValueError(
                f"{exc}, so sai cannot tell whether ud holds another account"
                f" named {name}. Check that ud works, then retry. To sign in"
                f" as the Foundry keystore's account {found_addr}, pass"
                f" `--addr {found_addr}`."
            ) from exc
        return keeper.cast_session(endpoint, addr, name)
    if found is not None and found.can_sign:
        addr = checked_address(found.addr, addr)
        try:
            return keeper.wallet_session(endpoint, addr, name)
        except keeper.SigningFailed as exc:
            if not has_keystore:
                raise
            if not sys.stdin.isatty():
                raise NeedsTerminal(
                    f"ud could not sign with {name}. The Foundry keystore"
                    f" ~/.foundry/keystores/{name} may have another password,"
                    " which cast can ask for."
                ) from exc
            # The keystore may have changed since ud looked it up. cast finds
            # it by name, so read it again and sign only as the same account.
            current = keystore_addr(name) or keeper.cast_address(name)
            if current.lower() != addr.lower():
                raise ValueError(
                    f"The Foundry keystore ~/.foundry/keystores/{name} now"
                    f" holds {current}, not {addr}; nothing was signed with it."
                ) from exc
            warn(
                f"ud could not sign with {name}; signing with the Foundry"
                f" keystore ~/.foundry/keystores/{name} instead."
            )
            return keeper.cast_session(endpoint, addr, name)
    if has_keystore:
        # Check --addr before anything signs: from the keystore file when it
        # records the addr, otherwise from cast, which may ask for the
        # password.
        found_addr = keystore_addr(name) or keeper.cast_address(name)
        if found is not None and found.addr.lower() != found_addr.lower():
            addr = chosen_keystore_addr(name, found, found_addr, addr)
        addr = checked_address(found_addr, addr)
        return keeper.cast_session(endpoint, addr, name)
    if found is not None:
        raise ValueError(ud_state_hint(name, found.cast_state))
    raise ValueError(
        f"No key named {name}: there is no Foundry keystore"
        f" ~/.foundry/keystores/{name}. Run `cast wallet list` to see"
        " keystores, or connect with `sai auth import`."
    )


def chosen_keystore_addr(
    name: str, ud: keeper.UdWallet, keystore: str, addr: str | None
) -> str:
    """Return ADDR when it picks the keystore's account, or fail.

    ud and the Foundry keystore hold different accounts under NAME, and ud
    cannot sign with its own until the names stop colliding. sai never picks
    one for the person.
    """
    both = (
        f"{name} names two accounts: ud's wallet holds {ud.addr} and the"
        f" Foundry keystore ~/.foundry/keystores/{name} holds {keystore}."
    )
    if addr is None:
        raise ValueError(
            f"{both} To sign in as the keystore's account, run `sai auth login"
            f" {name} --addr {keystore}`. To sign in as ud's account,"
            f" rename the keystore, then run `ud evm wallet sync {name}`."
        )
    if addr.lower() == ud.addr.lower():
        raise ValueError(f"{both} {ud_state_hint(name, ud.cast_state)}")
    if addr.lower() != keystore.lower():
        raise ValueError(f"{both} --addr {addr} matches neither.")
    return addr


NO_CLIPBOARD = (
    "No terminal and no working clipboard command ({names}). Ask the person"
    " to run `sai auth import` in their own terminal."
)


def read_token() -> str:
    """Read the token from a hidden prompt or, without a terminal, the
    clipboard. Never from arguments, the environment, or piped stdin."""
    if sys.stdin.isatty():
        token = click.prompt(
            "Paste the Sai session token (Enter reads the clipboard)",
            hide_input=True,
            default="",
            show_default=False,
            err=True,
        ).strip()
        if token:
            return token
    copied = clipboard.read()
    if copied is None:
        if sys.stdin.isatty():
            raise ValueError(f"No working clipboard command ({clipboard.NAMES})")
        raise SaiError(
            NO_CLIPBOARD.format(names=clipboard.NAMES),
            Code.INVALID_REQUEST,
            hint="sai auth import",
            exit_code=ExitCode.UNSUPPORTED,
        )
    if not copied[0]:
        raise ValueError(
            "Clipboard is empty; copy the session token from Sai Web and retry"
        )
    return copied[0]


@auth.command("import")
@click.option(
    "--addr", metavar="ADDR", help="Require the token to match this EVM address."
)
@click.pass_context
def import_token(ctx: click.Context, addr: str | None) -> None:
    """Connect with a session token copied from Sai Web.

    In a terminal, paste the token at the hidden prompt or press Enter to read
    the clipboard. Without a terminal, as when an agent runs this, read the
    clipboard. After storing the token, clear the clipboard if it still holds
    the token. Prints the addr, expiry, and endpoint, never the token.
    """
    token = read_token()
    result = sessions.import_session(token, keeper_url(ctx))
    if addr is not None and addr.lower() != result["evmAddress"].lower():
        raise ValueError("The Sai session token is for a different account than --addr")
    storage.save_session(result)
    # Re-read: the person may have copied something else since, and that
    # must survive.
    copied = clipboard.read()
    if copied is not None and copied[0] == token and not clipboard.clear(copied[1]):
        click.echo("Warning: could not clear the clipboard", err=True)
    emit(ctx, sessions.summary(result))


@auth.command("logout")
@click.argument("addr", required=False)
@click.pass_context
def logout(ctx: click.Context, addr: str | None) -> None:
    """Remove ADDR, or the active account, from this machine.

    Other connected accounts stay connected; when the active one is removed,
    none is active until `sai auth use`. Makes no network call. Keeper cannot
    revoke session tokens yet, so a token copied elsewhere stays valid until
    it expires.
    """
    removed = storage.delete_session(addr or ctx.obj["as_addr"])
    emit(
        ctx,
        {
            "removed": removed is not None,
            "addr": None if removed == storage.UNREADABLE_REMOVED else removed,
            "note": "Keeper cannot revoke session tokens yet; the token stays"
            " valid until it expires.",
        },
    )


def account_rows(endpoint: str) -> list[dict[str, Any]]:
    """Describe each connected account without its token."""
    accounts = storage.load_accounts()
    now = datetime.now(UTC)
    return [
        {
            **sessions.summary(result),
            "active": key == accounts["active"],
            "expired": sessions.expiry_time(result) <= now,
            "endpointMatches": result.get("endpoint") == endpoint,
            **sessions.expiry_fields(result, now),
        }
        for key, result in sorted(accounts["sessions"].items())
    ]


@auth.command("list")
@click.pass_context
def auth_list(ctx: click.Context) -> None:
    """List connected accounts, their expiry, and which one is active."""
    rows = account_rows(keeper_url(ctx))
    for row in rows:
        if row["active"] and row["endpointMatches"]:
            show_expiry_notice(ctx, row)
    emit(ctx, {"accounts": rows})


@auth.command("use")
@click.argument("addr")
@click.pass_context
def auth_use(ctx: click.Context, addr: str) -> None:
    """Make the connected ADDR the active account.

    Commands without `--as` act on the active account. Sign in again to
    connect a new one: `sai auth login NAME` and `sai auth import` add accounts.
    """
    emit(ctx, {**sessions.summary(storage.use_session(addr)), "active": True})


@auth.command("status")
@click.pass_context
def auth_status(ctx: click.Context) -> None:
    """Show the active account, or the --as account, and its expiry without
    showing the token."""
    result = session(ctx)
    fields = sessions.expiry_fields(result)
    show_expiry_notice(ctx, fields)
    emit(ctx, {**sessions.summary(result), **fields})


@main.group()
def market() -> None:
    """Inspect markets and prices."""


@market.command("list")
@click.pass_context
def market_list(ctx: click.Context) -> None:
    """List Keeper borrowing markets."""
    perform_query(ctx, keeper.MARKETS)


@market.command("prices")
@click.pass_context
def market_prices(ctx: click.Context) -> None:
    """List current indexed prices."""
    perform_query(ctx, keeper.PRICES)


@main.group()
@click.pass_context
def account(ctx: click.Context) -> None:
    """Inspect positions and account activity."""
    if ctx.invoked_subcommand != "overview":
        session_notice(ctx, ctx.obj["as_addr"])
        if auto_update_applies("account", ctx.obj["json"]):
            updater.tick(warn)


def perform_query(
    ctx: click.Context, query: str, variables: dict[str, Any] | None = None
) -> None:
    emit(ctx, keeper.request(keeper_url(ctx), query, variables))


@account.command("overview")
@click.option("--as", "addr", metavar="ADDR", help="Read this EVM account.")
@click.option("--json", "json_output", is_flag=True, help="Print compact JSON.")
@click.pass_context
def account_overview(ctx: click.Context, addr: str | None, json_output: bool) -> None:
    """Show Sai funds, positions, orders, local blockers, and session expiry."""
    ctx.obj["json"] = ctx.obj["json"] or json_output
    if auto_update_applies("account", ctx.obj["json"]):
        updater.tick(warn)
    try:
        result = overview.account_overview(settings(ctx), addr or ctx.obj["as_addr"])
    except overview.AccountUnavailable as exc:
        raise SessionError(f"{exc}. Pass --as ADDR explicitly.") from exc
    if ctx.obj["json"]:
        emit(ctx, result)
    else:
        show_expiry_notice(ctx, result["session"].get("data", {}))
        overview_format.show(result)


@account.command("positions")
@click.argument("addr")
@click.pass_context
def positions(ctx: click.Context, addr: str) -> None:
    """List positions and pending orders for ADDR."""
    perform_query(ctx, keeper.POSITIONS, {"trader": addr})


@account.command("history")
@click.argument("addr")
@click.pass_context
def history(ctx: click.Context, addr: str) -> None:
    """Read trade history for ADDR."""
    data = keeper.request(keeper_url(ctx), keeper.HISTORY, {"trader": addr})
    emit(ctx, keeper.safe_history_response(data))


@account.command("balances")
@click.argument("addr")
@click.option("--ws-endpoint", help="Override the keeper.ws_endpoint setting.")
@click.pass_context
def balances(ctx: click.Context, addr: str, ws_endpoint: str | None) -> None:
    """Read one account balance snapshot from Keeper's subscription."""
    emit(
        ctx,
        stream.snapshot(
            ws_url(ctx, ws_endpoint),
            stream.BALANCES,
            {"user": addr},
            notify=warn,
        ),
    )


@main.group()
def intent() -> None:
    """Inspect and reconcile trading request outcomes."""


@intent.command("status")
@click.argument("addr")
@click.option("--key", help="Specific intent key.")
@click.pass_context
def intent_status(ctx: click.Context, addr: str, key: str | None) -> None:
    """Read Keeper's latest intent records and record local pending fills.

    Use --key to check one intent for a final outcome.
    """
    data = reconcile.reconcile(keeper_url(ctx), addr, key)
    emit(ctx, reconcile.status_report(data))


@intent.command("wait")
@click.argument("key")
@click.option("--addr", help="EVM or Nibiru address that submitted the intent.")
@click.option(
    "--timeout",
    type=float,
    default=wait.DEFAULT_TIMEOUT,
    show_default=True,
    help="Maximum wait in seconds.",
)
@click.pass_context
def intent_wait(ctx: click.Context, key: str, addr: str | None, timeout: float) -> None:
    """Wait for an intent's final outcome and print its receipt."""
    started = time.monotonic()
    wait.validate_timeout(timeout)
    endpoint = keeper_url(ctx)
    account = addr or ctx.obj["as_addr"]
    if account is None:
        try:
            with storage.busy_timeout(max(0, started + timeout - time.monotonic())):
                local = storage.intent_record(endpoint, key)
        except sqlite3.OperationalError as exc:
            if not wait.ledger_busy(exc):
                raise
            emit_wait(ctx, wait.unknown_result(key, None))
            return
        if local is not None:
            account = local[0]
        elif storage.session_exists():
            stored = storage.load_session()
            if stored.get("endpoint") == endpoint:
                account = stored["evmAddress"]
    if account is None:
        raise ValueError("Pass --addr with the account that submitted this intent")
    with protect_wait(ctx, lambda: (endpoint, account, key)):
        emit_wait(
            ctx,
            wait.for_intent(endpoint, account, key, timeout=timeout, started=started),
        )


@intent.command("local")
@click.argument("addr")
@click.pass_context
def intent_local(ctx: click.Context, addr: str) -> None:
    """List locally recorded intent identities for ADDR."""
    emit(ctx, storage.list_pending(keeper_url(ctx), addr))


@main.group()
def trade() -> None:
    """Submit authenticated trading intents."""


class PinnedSession:
    """Load the --as account, or the active one, and keep using that addr.

    The first load fixes the addr, so the output names the account that
    submitted even if another process switches accounts meanwhile.
    """

    def __init__(self, ctx: click.Context) -> None:
        self.ctx = ctx
        self.addr: str | None = None

    def __call__(self) -> dict[str, str]:
        if self.addr is None:
            result = session(self.ctx)
        else:
            result = sessions.load_active_session(keeper_url(self.ctx), self.addr)
        self.addr = result["evmAddress"]
        return result


def loader(ctx: click.Context) -> PinnedSession:
    """Return this command's session loader; it loads only when called."""
    return ctx.obj.setdefault("loader", PinnedSession(ctx))


class WaitRecovery(SaiError):
    """Render recovery independently of the normal receipt output."""

    def __init__(self, result: dict[str, Any], *, output_started: bool) -> None:
        self.result = result
        self.output_started = output_started
        super().__init__(
            "The receipt did not finish. Check the intent status before retrying.",
            result["code"],
            hint=result["hint"],
            intent_key=result["intentKey"],
        )


@contextlib.contextmanager
def protect_wait(
    ctx: click.Context,
    identity: Callable[[], intents.IntentIdentity | None],
) -> Generator[None]:
    ctx.meta["wait_output_started"] = False
    try:
        yield
    except (click.exceptions.Exit, storage.UnresolvedIntent):
        # A completed receipt has already printed. A reservation refusal
        # carries the existing key, if any, and the new key was never sent.
        raise
    except (KeyboardInterrupt, Exception) as exc:
        submission = identity()
        if submission is None:
            raise
        confirmed: wait.Outcome = "unknown"
        if isinstance(exc, keeper.AdmissionRejected):
            confirmed = "rejected"
        elif isinstance(exc, keeper.NotSent):
            confirmed = "not_sent"
        result = wait.recovery_result(
            *submission,
            interrupted=isinstance(exc, KeyboardInterrupt)
            or isinstance(exc.__cause__, KeyboardInterrupt),
            confirmed=confirmed,
        )
        # The normal receipt may already occupy stdout. Keep recovery on
        # stderr once printing began so consumers see one JSON per stream.
        raise WaitRecovery(
            result, output_started=ctx.meta["wait_output_started"]
        ) from exc


def wait_option(action: Callable[..., None]) -> Callable[..., None]:
    def record(ctx: click.Context, _param: click.Parameter, enabled: bool) -> None:
        ctx.meta["wait_for_trade"] = enabled

    @wraps(action)
    def run(*args: Any, **kwargs: Any) -> None:
        ctx = click.get_current_context()
        if ctx.meta.get("wait_for_trade"):
            with (
                intents.track_submission() as trace,
                protect_wait(ctx, lambda: trace.identity),
            ):
                action(*args, **kwargs)
        else:
            action(*args, **kwargs)

    return click.option(
        "--wait",
        is_flag=True,
        expose_value=False,
        callback=record,
        help=f"Wait up to {wait.DEFAULT_TIMEOUT:g} seconds for the intent outcome.",
    )(run)


def emit_wait(ctx: click.Context, result: dict[str, Any]) -> None:
    outcome = result["outcome"]
    exit_code = ExitCode.OK
    if outcome == "unknown":
        addr_args = ["--addr", result["addr"]] if result.get("addr") else []
        result["hint"] = storage.keeper_command_hint(
            keeper_url(ctx),
            "intent",
            "wait",
            result["intentKey"],
            *addr_args,
        )
    if isinstance(result.get("message"), str):
        result["message"] = log.redact(result["message"])
    if outcome not in ("filled", "placed", "succeeded"):
        failure = SaiError(
            result.get("message") or "The intent outcome is still unknown.",
            result.get("code", Code.KEEPER_ERROR),
            intent_key=result["intentKey"],
            hint=result.get("hint"),
        )
        exit_code = failure.exit_code
        if ctx.obj["json"]:
            result["error"] = redact_error(failure.as_dict())
    emit(ctx, result, wait_receipt=True)
    if exit_code != ExitCode.OK:
        ctx.exit(exit_code)


def emit_trade(ctx: click.Context, result: dict[str, Any]) -> None:
    """Print a trade result with the account it used or would use.

    A dry run without a usable session names no account, unless --as asked
    for one, which then must be connected.
    """
    load = loader(ctx)
    account = load.addr
    if account is None:
        try:
            account = load()["evmAddress"]
        except ValueError:
            # A dry run needs no session, but a requested account must exist.
            if ctx.obj["as_addr"]:
                raise
            account = None
    if (
        ctx.meta.get("wait_for_trade")
        and account
        and result.get("status") == "accepted"
    ):
        final = wait.for_intent(keeper_url(ctx), account, result["intentKey"])
        # Keep trade quote metadata, but report the final outcome as the status.
        metadata = {key: value for key, value in result.items() if key != "message"}
        emit_wait(ctx, {**metadata, **final, "status": final["outcome"]})
        return
    output = {"addr": account, **result}
    if "positionRoute" in result:
        output = confirmation.with_digest(output)
    emit(ctx, output)


def trade_error(ctx: click.Context, exc: Exception) -> None:
    if isinstance(exc, trading.PositionChanged) and exc.preview is not None:
        if ctx.obj["json"]:
            exc.details = {"preview": confirmation.with_digest(exc.preview)}
        else:
            exc.message += " Run with --dry-run to review a fresh preview."
    if (
        ctx.meta.get("wait_for_trade")
        and isinstance(exc, (keeper.OutcomeUnknown, keeper.AdmissionRejected))
        and not isinstance(exc.__cause__, KeyboardInterrupt)
    ):
        account = loader(ctx).addr
        if account and exc.intent_key:
            final = (
                wait.rejected_result(exc.intent_key, account, str(exc))
                if isinstance(exc, keeper.AdmissionRejected)
                else wait.for_intent(keeper_url(ctx), account, exc.intent_key)
            )
            emit_wait(ctx, {**final, "status": final["outcome"]})
            return
    raise exc


def warn(message: str) -> None:
    click.echo(message, err=True)


@trade.command("open")
@click.option("--market", "market_id", type=int, required=True)
@click.option("--side", type=click.Choice(["long", "short"]), required=True)
@click.option(
    "--type",
    "order_type",
    type=click.Choice(["market", "limit", "stop"]),
    required=True,
)
@click.option("--amount", required=True, help="USDC amount in USD.")
@click.option("--leverage", required=True)
@click.option(
    "--price",
    help="Required trigger for limit/stop; optional dry-run reference for market orders. Live market orders use a refreshed price.",
)
@click.option("--slippage", required=True, help="Maximum slippage percentage.")
@click.option("--take-profit")
@click.option("--stop-loss")
@click.option(
    "--allow-concurrent-open",
    is_flag=True,
    help="Allow another open while an earlier open is unknown or accepted."
    " Any locally pending open on the same account and Keeper still blocks,"
    " including a late acknowledgement.",
)
@click.option("--dry-run", is_flag=True)
@wait_option
@click.pass_context
def trade_open(
    ctx: click.Context,
    market_id: int,
    side: str,
    order_type: str,
    amount: str,
    leverage: str,
    price: str | None,
    slippage: str,
    take_profit: str | None,
    stop_loss: str | None,
    allow_concurrent_open: bool,
    dry_run: bool,
) -> None:
    """Open a market, limit, or stop trade."""
    try:
        emit_trade(
            ctx,
            trading.open_trade(
                keeper_url(ctx),
                loader(ctx),
                market_id=market_id,
                side=side,
                order_type=order_type,
                amount=amount,
                leverage=leverage,
                price=price,
                slippage=slippage,
                take_profit=take_profit,
                stop_loss=stop_loss,
                allow_concurrent_open=allow_concurrent_open,
                dry_run=dry_run,
                lcd_endpoint=settings(ctx).lcd_endpoint,
                notify=warn,
            ),
        )
    except (ValueError, KeyError, keeper.KeeperError) as exc:
        trade_error(ctx, exc)


@trade.command("close")
@click.argument("trade_id", type=int)
@click.option(
    "--cancel-order",
    is_flag=True,
    help="Cancel a pending order instead of closing a position.",
)
@click.option("--dry-run", is_flag=True)
@wait_option
@click.pass_context
def trade_close(
    ctx: click.Context, trade_id: int, cancel_order: bool, dry_run: bool
) -> None:
    """Close a position or cancel a pending order."""
    try:
        emit_trade(
            ctx,
            trading.close_trade(
                keeper_url(ctx),
                loader(ctx),
                trade_id=trade_id,
                cancel_order=cancel_order,
                dry_run=dry_run,
                lcd_endpoint=settings(ctx).lcd_endpoint,
            ),
        )
    except (ValueError, keeper.KeeperError) as exc:
        trade_error(ctx, exc)


@trade.command("tpsl")
@click.argument("trade_id", type=int)
@click.option("--take-profit")
@click.option("--stop-loss")
@click.option("--clear-take-profit", is_flag=True)
@click.option("--clear-stop-loss", is_flag=True)
@click.option("--dry-run", is_flag=True)
@wait_option
@click.pass_context
def trade_tpsl(
    ctx: click.Context,
    trade_id: int,
    take_profit: str | None,
    stop_loss: str | None,
    clear_take_profit: bool,
    clear_stop_loss: bool,
    dry_run: bool,
) -> None:
    """Set or clear take-profit and stop-loss prices."""
    try:
        emit_trade(
            ctx,
            trading.set_tpsl(
                keeper_url(ctx),
                loader(ctx),
                trade_id=trade_id,
                take_profit=take_profit,
                stop_loss=stop_loss,
                clear_take_profit=clear_take_profit,
                clear_stop_loss=clear_stop_loss,
                dry_run=dry_run,
                lcd_endpoint=settings(ctx).lcd_endpoint,
            ),
        )
    except (ValueError, keeper.KeeperError) as exc:
        trade_error(ctx, exc)


@trade.command("leverage")
@click.argument("trade_id", type=int)
@click.option("--leverage", required=True, help="Target leverage as a decimal.")
@click.option("--dry-run", is_flag=True)
@wait_option
@click.pass_context
def trade_leverage(
    ctx: click.Context, trade_id: int, leverage: str, dry_run: bool
) -> None:
    """Set target leverage without changing TP or SL."""
    try:
        emit_trade(
            ctx,
            trading.set_leverage(
                keeper_url(ctx),
                loader(ctx),
                trade_id=trade_id,
                leverage=leverage,
                dry_run=dry_run,
                lcd_endpoint=settings(ctx).lcd_endpoint,
            ),
        )
    except (ValueError, keeper.KeeperError) as exc:
        trade_error(ctx, exc)


def position_submit_check(
    load: PinnedSession, digest: str | None
) -> trading.SubmitCheck | None:
    if digest is None:
        return None

    def check(preview: dict[str, Any]) -> None:
        if digest != confirmation.preview_digest({**preview, "addr": load.addr}):
            raise trading.PositionChanged(
                "The trade no longer matches the confirmed preview. Run with"
                " --dry-run to review a new preview before submitting."
            )

    return check


position_digest_option = click.option(
    "--preview-digest", help="Submit only if the trade matches this dry-run digest."
)


def lcd_trade_settings(
    ctx: click.Context, lcd_endpoint: str | None
) -> sai_settings.Settings:
    """Apply a position command's LCD flag before resolving its settings."""
    if lcd_endpoint is not None:
        ctx.obj["flags"]["lcd.endpoint"] = lcd_endpoint
    session_notice(ctx, ctx.obj["as_addr"])
    return settings(ctx)


@trade.command("margin")
@click.argument("trade_id", type=int)
@click.option("--direction", type=click.Choice(["add", "remove"]), required=True)
@click.option(
    "--amount",
    required=True,
    help="USDC amount for a leverage quote, not an execution cap.",
)
@click.option(
    "--lcd-endpoint",
    help="Nibiru REST endpoint for exact contract state. Defaults to the"
    " lcd.endpoint setting.",
)
@click.option("--dry-run", is_flag=True)
@position_digest_option
@wait_option
@click.pass_context
def trade_margin(
    ctx: click.Context,
    trade_id: int,
    direction: str,
    amount: str,
    lcd_endpoint: str | None,
    dry_run: bool,
    preview_digest: str | None,
) -> None:
    """Add or remove margin through a target-leverage intent.

    The amount is a quote, not a cap: collateral can change before execution.
    """
    resolved = lcd_trade_settings(ctx, lcd_endpoint)
    try:
        emit_trade(
            ctx,
            trading.change_margin(
                resolved.endpoint,
                loader(ctx),
                trade_id=trade_id,
                direction=direction,
                amount=amount,
                lcd_endpoint=resolved.lcd_endpoint,
                dry_run=dry_run,
                before_submit=position_submit_check(loader(ctx), preview_digest),
            ),
        )
    except (ValueError, KeyError, TypeError, keeper.KeeperError) as exc:
        trade_error(ctx, exc)


@trade.command("partial-close")
@click.argument("trade_id", type=int)
@click.option("--percent", type=click.IntRange(1, 99), required=True)
@click.option(
    "--lcd-endpoint",
    help="Nibiru REST endpoint for exact contract state. Defaults to the"
    " lcd.endpoint setting.",
)
@click.option("--dry-run", is_flag=True)
@position_digest_option
@wait_option
@click.pass_context
def partial_close(
    ctx: click.Context,
    trade_id: int,
    percent: int,
    lcd_endpoint: str | None,
    dry_run: bool,
    preview_digest: str | None,
) -> None:
    """Reduce an open USDC position by a whole percentage."""
    resolved = lcd_trade_settings(ctx, lcd_endpoint)
    try:
        emit_trade(
            ctx,
            trading.partial_close(
                resolved.endpoint,
                loader(ctx),
                trade_id=trade_id,
                percent=percent,
                lcd_endpoint=resolved.lcd_endpoint,
                dry_run=dry_run,
                before_submit=position_submit_check(loader(ctx), preview_digest),
            ),
        )
    except (ValueError, KeyError, TypeError, keeper.KeeperError) as exc:
        trade_error(ctx, exc)


@main.command("doctor")
@click.option(
    "--offline", is_flag=True, help="Skip the Keeper, WebSocket, and LCD checks."
)
@click.option("--no-fix", is_flag=True, help="Report problems without fixing any.")
@click.option(
    "--ws-endpoint",
    help="Keeper WebSocket endpoint. Defaults to the keeper.ws_endpoint setting.",
)
@click.option(
    "--lcd-endpoint",
    help="Nibiru REST endpoint. Defaults to the lcd.endpoint setting.",
)
@click.pass_context
def doctor_command(
    ctx: click.Context,
    offline: bool,
    no_fix: bool,
    ws_endpoint: str | None,
    lcd_endpoint: str | None,
) -> None:
    """Check the install, config, MCP registrations, session, intent ledger,
    and endpoints.

    With fixes enabled, doctor creates private Sai directories, tightens
    permissions on managed files, repairs
    stale Codex and Cursor MCP registrations, migrates the intent ledger,
    and reconciles unknown intents for the signed-in account with Keeper.
    It reports stale Claude Code registrations with a command to repair
    each existing entry. It never deletes or rewrites the session, and
    never prints its token. Each check prints pass, fixed, warn, fail, or
    skipped. Doctor exits 2 for invalid settings and 1 for other failed checks.
    """
    from . import doctor

    flags = dict(ctx.find_root().obj["flags"])
    for key, value in (
        ("keeper.ws_endpoint", ws_endpoint),
        ("lcd.endpoint", lcd_endpoint),
    ):
        if value is not None:
            flags[key] = value
    config_check, config = doctor.check_config(flags)
    checks = [
        config_check,
        *doctor.run_checks(
            doctor.context(config, fix=not no_fix, offline=offline),
            after_home=ctx.obj.get("doctor_log_start"),
        ),
    ]
    overall = doctor.overall(checks)
    code = (
        Code.INVALID_REQUEST if config_check.status == "fail" else Code.INTERNAL_ERROR
    )
    log.event(
        "doctor.result",
        "info" if overall == "pass" else "warn",
        overall=overall,
        fix=not no_fix,
        offline=offline,
        checks=[doctor.log_entry(check) for check in checks],
        code=code if overall == "fail" else None,
    )
    if overall == "fail":
        if not ctx.obj["json"]:
            report = doctor.render_text(checks)
        else:
            report = "\n".join(
                f"{check.id}: {check.summary}"
                + (f" Fix: {check.fix}" if check.fix else "")
                for check in checks
                if check.status == "fail"
            )
        raise SaiError(
            report,
            code,
            details=doctor.report(checks) if ctx.obj["json"] else None,
            exit_code=ExitCode.USAGE if config_check.invalid_setting else None,
        )
    if ctx.obj["json"]:
        click.echo(doctor.render_json(doctor.report(checks)))
    else:
        click.echo(doctor.render_text(checks))


@main.group()
def watch() -> None:
    """Stream live Keeper updates as JSON lines."""


@watch.command("positions")
@click.argument("addr")
@click.option("--ws-endpoint")
@click.pass_context
def watch_positions(ctx: click.Context, addr: str, ws_endpoint: str | None) -> None:
    """Follow position snapshots for ADDR until interrupted."""
    watch_stream(
        ws_url(ctx, ws_endpoint),
        stream.POSITIONS,
        {"user": addr},
    )


@watch.command("balances")
@click.argument("addr")
@click.option("--ws-endpoint")
@click.pass_context
def watch_balances(ctx: click.Context, addr: str, ws_endpoint: str | None) -> None:
    """Follow balances, preserving Sai deposit and credit labels."""
    watch_stream(
        ws_url(ctx, ws_endpoint),
        stream.BALANCES,
        {"user": addr},
    )


@watch.command("price")
@click.argument("token_id", type=int)
@click.option("--ws-endpoint")
@click.pass_context
def watch_price(ctx: click.Context, token_id: int, ws_endpoint: str | None) -> None:
    """Follow the current price of TOKEN_ID."""
    watch_stream(
        ws_url(ctx, ws_endpoint),
        stream.PRICE,
        {"id": token_id},
    )


def ws_url(ctx: click.Context, ws_endpoint: str | None) -> str:
    """The command's --ws-endpoint, else the resolved WebSocket setting."""
    if ws_endpoint is not None:
        sai_settings.check_value(
            "keeper.ws_endpoint", ws_endpoint, source="--ws-endpoint"
        )
        return ws_endpoint
    return settings(ctx).ws_endpoint


def watch_stream(ws_endpoint: str, query: str, variables: dict[str, Any]) -> None:
    """Print each snapshot as a JSON line, reconnecting until interrupted."""
    stream.check_ws(ws_endpoint)
    while True:
        try:
            for payload in stream.payloads(ws_endpoint, query, variables, notify=warn):
                click.echo(json.dumps(payload, separators=(",", ":")))
        except stream.DISCONNECTS as exc:
            log.event(
                "watch.disconnected",
                "warn",
                code=Code.STREAM_ERROR,
                errorType=type(exc).__name__,
                endpoint=log.endpoint(ws_endpoint),
            )
            warn(
                f"Disconnected ({type(exc).__name__}); data may be stale;"
                " reconnecting in 2s"
            )
            time.sleep(2)


@main.group("config")
def config_group() -> None:
    """Show and change settings in $SAI_HOME/config.toml.

    Keys: keeper.endpoint, keeper.ws_endpoint, lcd.endpoint, update.auto.
    A flag or SAI_* environment variable still overrides the file.
    """


def config_key(key: str) -> sai_settings.Setting:
    """Return KEY's registry entry; an unknown key is a usage error (exit 2)."""
    try:
        return sai_settings.setting(key)
    except sai_settings.SettingsError as exc:
        raise click.UsageError(str(exc)) from exc


def config_file() -> Path:
    return sai_settings.config_path(os.environ)


@config_group.command("list")
@click.pass_context
def config_list(ctx: click.Context) -> None:
    """Show every setting with its value and source.

    The source is flag, env, file, default, or derived. Endpoint URLs are
    shown as origins. `sai config get` prints the exact value.
    """
    shown = sai_settings.public(settings(ctx))
    if ctx.obj["json"]:
        emit(ctx, {"path": str(config_file()), "settings": shown})
        return
    click.echo(f"# {config_file()}")
    for key, entry in shown.items():
        value = json.dumps(entry["value"])
        click.echo(f"{key} = {value}  # {entry['source']}, {entry['env']}")


@config_group.command("get")
@click.argument("key")
@click.pass_context
def config_get(ctx: click.Context, key: str) -> None:
    """Print the exact value of KEY as resolved now."""
    spec = config_key(key)
    resolved = settings(ctx)
    value = resolved.value(spec.key)
    if ctx.obj["json"]:
        emit(
            ctx,
            {"key": spec.key, "value": value, "source": resolved.sources[spec.key]},
        )
        return
    click.echo(json.dumps(value) if isinstance(value, bool) else value)


@config_group.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_set(ctx: click.Context, key: str, value: str) -> None:
    """Set KEY to VALUE in the config file, keeping its comments."""
    spec = config_key(key)
    path = config_file()
    try:
        parsed = sai_settings.parse_value(spec.key, value)
    except sai_settings.SettingsError as exc:
        raise click.UsageError(
            f"{exc}. Valid keys: {', '.join(sai_settings.KEYS)}"
        ) from exc
    try:
        sai_settings.edit_file(
            path, lambda text: sai_settings.set_value(text, spec.key, value, path)[0]
        )
    except sai_settings.UneditableSection as exc:
        raise click.UsageError(str(exc)) from exc
    report_change(
        ctx,
        spec,
        {
            "key": spec.key,
            "value": sai_settings.public_value(spec, parsed),
            "path": str(path),
        },
    )


@config_group.command("unset")
@click.argument("key")
@click.pass_context
def config_unset(ctx: click.Context, key: str) -> None:
    """Remove KEY from the config file so the default applies again."""
    spec = config_key(key)
    path = config_file()
    removed: list[bool] = []

    def edit(text: str | None) -> str | None:
        new, was_set = sai_settings.unset_value(text, spec.key, path)
        removed.append(was_set)
        # An already unset key leaves the file untouched.
        return new if was_set else None

    try:
        sai_settings.edit_file(path, edit)
    except sai_settings.UneditableSection as exc:
        raise click.UsageError(str(exc)) from exc
    report_change(
        ctx, spec, {"key": spec.key, "removed": removed[0], "path": str(path)}
    )


def report_change(
    ctx: click.Context, spec: sai_settings.Setting, result: dict[str, Any]
) -> None:
    """Print RESULT, and warn when a flag or variable still overrides it."""
    if ctx.obj["json"]:
        emit(ctx, result)
    elif "value" in result:
        click.echo(f"Set {spec.key} in {result['path']}")
    else:
        verb = "Unset" if result["removed"] else "Already unset:"
        click.echo(f"{verb} {spec.key} in {result['path']}")
    if os.environ.get(spec.env, "").strip():
        warn(f"Warning: {spec.env} is set and overrides {spec.key} in the file")


@main.group("mcp", invoke_without_command=True)
@click.pass_context
def mcp_group(ctx: click.Context) -> None:
    """Run Sai as an MCP server for agent clients.

    Register it with Claude Code, Codex, or Cursor with
    `sai mcp install claude` (or codex, cursor, --all).
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(0)


SERVE_TTY_NOTICE = (
    "sai mcp serve speaks MCP over stdin and stdout; an agent client should"
    " launch it; register it with `sai mcp install`."
    " Press Ctrl-C to stop."
)


@mcp_group.command("serve")
@click.option(
    "--endpoint",
    help="Keeper GraphQL HTTP endpoint. Defaults to `sai --endpoint` when"
    " given, then SAI_ENDPOINT, then keeper.endpoint in the config file,"
    " then mainnet.",
)
@click.option(
    "--ws-endpoint",
    help="Keeper WebSocket endpoint. Defaults to `sai --ws-endpoint`, then"
    " SAI_WS_ENDPOINT, then keeper.ws_endpoint, then one derived from the"
    " Keeper endpoint.",
)
@click.option(
    "--lcd-endpoint",
    help="Nibiru LCD endpoint for margin and partial-close reads. Defaults to"
    " `sai --lcd-endpoint`, then SAI_LCD_ENDPOINT, then lcd.endpoint, then"
    " mainnet.",
)
@click.pass_context
def mcp_serve(
    ctx: click.Context,
    endpoint: str | None,
    ws_endpoint: str | None,
    lcd_endpoint: str | None,
) -> None:
    """Serve Sai tools to an agent client over stdio.

    Stdout carries only MCP protocol messages. Every tool is listed; trade
    tools submit only while an account is signed in with `sai auth login NAME`
    or `sai auth import`. Endpoints resolve once, at start, like the CLI's:
    these flags, then SAI_* variables, then $SAI_HOME/config.toml.
    """
    from .mcp_server import server

    flags = serve_flags(
        ctx.find_root().obj["flags"], endpoint, ws_endpoint, lcd_endpoint
    )
    if sys.stdin.isatty():
        click.echo(SERVE_TTY_NOTICE, err=True)
    server.serve(flags)


def serve_flags(
    global_flags: dict[str, str | None],
    endpoint: str | None,
    ws_endpoint: str | None,
    lcd_endpoint: str | None,
) -> dict[str, str | None]:
    """The flag layer for `sai mcp serve`: its own options, then the same
    `sai` global flags."""
    passed = (endpoint, ws_endpoint, lcd_endpoint)
    return {
        key: value if value is not None else global_flags.get(key)
        for (_, key), value in zip(GLOBAL_FLAGS, passed, strict=True)
    }


class ArgvAfterDash(click.Command):
    """Keep everything after `--` as the server command instead of CLIENTs."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if "--" in args:
            at = args.index("--")
            ctx.meta["server_argv"] = args[at + 1 :]
            if not ctx.meta["server_argv"]:
                raise click.UsageError("Give the server command after `--`.", ctx)
            args = args[:at]
        return super().parse_args(ctx, args)


def interactive() -> bool:
    """Prompt only when a person is at both ends; agents never see prompts."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def mcp_clients() -> dict[str, mcp_install.Client]:
    return mcp_install.make_clients(Path.home(), os.environ, Path.cwd())


def chosen_clients(names: tuple[str, ...]) -> list[str]:
    try:
        return list(dict.fromkeys(mcp_install.normalize_client(n) for n in names))
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc


def pick(prompt: str, options: list[str]) -> list[str]:
    """Ask on the terminal which of options to act on; blank means all."""
    for number, name in enumerate(options, 1):
        click.echo(f"  {number}. {name}", err=True)
    answer = click.prompt(prompt, default="all", err=True)
    if answer.strip().lower() == "all":
        return options
    picked = []
    for part in answer.replace(",", " ").split():
        if part.isdigit() and 1 <= int(part) <= len(options):
            picked.append(options[int(part) - 1])
        else:
            picked.append(part)
    return chosen_clients(tuple(picked))


def server_target(ctx: click.Context) -> mcp_install.Target:
    target, warnings = mcp_install.server_target(
        ctx.meta.get("server_argv"),
        os.environ,
        Path.home(),
        mcp_install.running_prefix(),
    )
    for message in warnings:
        warn(message)
    return target


def quiet_target(ctx: click.Context) -> mcp_install.Target | None:
    """The command install would register, or None; prints nothing."""
    try:
        target, _ = mcp_install.server_target(
            ctx.meta.get("server_argv"),
            os.environ,
            Path.home(),
            mcp_install.running_prefix(),
        )
    except mcp_install.InstallError:
        return None
    return target


def finish(ctx: click.Context, results: list[mcp_install.Result]) -> None:
    summary = mcp_install.summarize(results)
    if summary["reconnectNeeded"]:
        warn("Restart or reconnect the changed clients to load the change.")
    if any(r.client == "cursor" and r.action in ("added", "replaced") for r in results):
        warn(mcp_install.Cursor.HINT)
    if any(result.action == "failed" for result in results):
        if ctx.obj["json"]:
            lines = []
            for result in results:
                target = (
                    f"{result.client} ({result.scope})"
                    if result.scope
                    else result.client
                )
                detail = f": {result.error}" if result.error else ""
                lines.append(f"{target}: {result.action}{detail}")
            if summary["reconnectNeeded"]:
                lines.append("Client registrations changed.")
            details = "\n".join(lines)
        else:
            details = json.dumps(summary, indent=2)
        report = {
            **summary,
            "results": [{**r.to_json(), "changed": r.changed} for r in results],
        }
        raise SaiError(details, hint="sai mcp list", details=report)
    emit(ctx, summary)


SCOPE_OPTION = click.option(
    "--scope",
    type=click.Choice(mcp_install.UNINSTALL_SCOPES),
    default="user",
    show_default=True,
    help="user: every project. project: this directory's shared config"
    " (Claude Code .mcp.json, Cursor .cursor/mcp.json)."
    " local: this directory's Claude Code local config. Codex is user only.",
)


@mcp_group.command("list")
@click.pass_context
def mcp_list(ctx: click.Context) -> None:
    """Show detected agent clients and where Sai is registered.

    `current` is true when the entry runs the command `install` would
    register. Only the sai entry is read; other servers are never shown.
    """
    try:
        target = server_target(ctx)
        command: list[str] | None = target.argv
    except mcp_install.InstallError as exc:
        warn(str(exc))
        target, command = None, None
    emit(
        ctx,
        {
            "command": command,
            "clients": mcp_install.list_clients(mcp_clients(), target),
        },
    )


@mcp_group.command("install", cls=ArgvAfterDash)
@click.argument("clients", nargs=-1)
@click.option("--client", "client_options", multiple=True, help="Name a client.")
@click.option(
    "--repair-existing",
    type=click.Choice(mcp_install.SERVER_NAMES),
    help="Repair only this existing Claude Code entry in the selected scope.",
)
@click.option(
    "--reset-args",
    is_flag=True,
    help="Use default server arguments when repairing an existing entry.",
)
@click.option("--all", "all_clients", is_flag=True, help="Every detected client.")
@click.option("--dry-run", is_flag=True, help="Show the changes; make none.")
@SCOPE_OPTION
@click.pass_context
def mcp_install_command(
    ctx: click.Context,
    clients: tuple[str, ...],
    client_options: tuple[str, ...],
    repair_existing: str | None,
    reset_args: bool,
    all_clients: bool,
    dry_run: bool,
    scope: str,
) -> None:
    """Register `sai mcp serve` with Claude Code, Codex, or Cursor.

    CLIENT is claude, codex, or cursor. Registers the stable `sai` on PATH,
    normally ~/.local/bin/sai, so updates need no re-registering. For a dev
    checkout, give the server command after `--`, for example
    `sai mcp install claude -- uv run --directory ~/open-sai sai mcp serve`.

    With no CLIENT and no --all, a terminal asks which clients to register;
    without a terminal, this lists detected clients and changes nothing.
    """
    names = chosen_clients((*clients, *client_options))
    if reset_args and repair_existing is None:
        raise click.UsageError("--reset-args requires --repair-existing.")
    if repair_existing is not None and (
        names != ["claude"] or all_clients or ctx.meta.get("server_argv")
    ):
        raise click.UsageError(
            "--repair-existing requires --client claude and the default server command."
        )
    found = mcp_clients()
    target = server_target(ctx)
    if repair_existing is not None:
        claude = found["claude"]
        assert isinstance(claude, mcp_install.Claude)
        finish(
            ctx,
            [
                mcp_install.repair_claude_existing(
                    claude,
                    scope,
                    repair_existing,
                    target,
                    dry_run,
                    reset_args=reset_args,
                )
            ],
        )
        return
    detected = [n for n, c in found.items() if c.detected()[0]]
    if all_clients:
        names = list(dict.fromkeys([*names, *detected]))
    if not names:
        if not interactive():
            emit(
                ctx,
                {
                    "command": target.argv,
                    "clients": mcp_install.list_clients(found, target),
                },
            )
            warn(
                "Nothing changed. Name clients, as in `sai mcp install claude`,"
                " or pass --all."
            )
            return
        if not detected:
            raise click.UsageError("No agent clients found; name one to register.")
        names = pick("Register Sai with (numbers, names, or all)", detected)
    if scope == "local" and names != ["claude"]:
        raise click.UsageError("Install scope local requires the Claude Code client.")
    results = [mcp_install.install_one(found[n], scope, target, dry_run) for n in names]
    finish(ctx, results)


@mcp_group.command("uninstall")
@click.argument("clients", nargs=-1)
@click.option("--all", "all_clients", is_flag=True, help="Every client.")
@click.option("--dry-run", is_flag=True, help="Show the changes; make none.")
@click.option(
    "--scope",
    type=click.Choice(mcp_install.UNINSTALL_SCOPES),
    help="Remove only this scope. Default: every scope sai is registered in"
    " (Claude Code local entries are this directory's).",
)
@click.pass_context
def mcp_uninstall(
    ctx: click.Context,
    clients: tuple[str, ...],
    all_clients: bool,
    dry_run: bool,
    scope: str | None,
) -> None:
    """Remove Sai from Claude Code, Codex, or Cursor.

    Removes canonical and legacy Sai entries from every scope for each client, unless
    --scope names one. With no CLIENT and no --all, a terminal asks which to
    remove; without a terminal this is a usage error that names where Sai is
    registered.
    """
    found = mcp_clients()
    names = chosen_clients(clients)
    target = quiet_target(ctx)
    if all_clients:
        names = list(found)
    if not names:
        rows = mcp_install.list_clients(found, target)
        registered = [r["client"] for r in rows if r["registered"]]
        where = ", ".join(
            f"{r['client']} ({', '.join(e['scope'] for e in r['registrations'])})"
            for r in rows
            if r["registered"]
        )
        if not interactive():
            raise click.UsageError(
                "Name clients to remove, or pass --all. Sai is registered with: "
                + (where or "no client")
                + "."
            )
        if not registered:
            raise click.UsageError("Sai is not registered with any client.")
        names = pick("Remove Sai from (numbers, names, or all)", registered)
    results = [
        r
        for n in names
        for r in mcp_install.uninstall_client(found[n], scope, dry_run, target)
    ]
    finish(ctx, results)


@main.group("logs")
def logs_group() -> None:
    """Read the JSON-lines logs that sai and its MCP server keep.

    sai records each command, MCP tool call, Keeper request, and intent
    status change under $SAI_HOME/logs, one file per surface (cli, mcp,
    update) and UTC day. The session token is never written. Turn logging
    off with `sai config set logs.enabled false` or SAI_LOG=0; files older
    than logs.keep_days are deleted.
    """


@logs_group.command("path")
@click.pass_context
def logs_path(ctx: click.Context) -> None:
    """Print the logs directory. With --json, also whether logging is on,
    how many days are kept, and the files there."""
    root = log.directory()
    if not ctx.obj["json"]:
        click.echo(root)
        return
    log.flush(1.0)
    on, keep_days = log.config()
    emit(
        ctx,
        {
            "dir": str(root),
            "enabled": on,
            "keepDays": keep_days,
            "files": [path.name for _day, _surface, path in log.log_files(root)],
        },
    )


@logs_group.command("tail")
@click.option(
    "-n",
    "count",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="How many events to show.",
)
@click.option(
    "--surface",
    type=click.Choice(log.SURFACES),
    help="Only events from the CLI, the MCP server, or the updater.",
)
@click.option(
    "--event",
    "name",
    metavar="EVENT",
    help="Only this event, such as keeper.request, or a family by its"
    " prefix: `intent` matches intent.reserved and intent.status.",
)
@click.pass_context
def logs_tail(
    ctx: click.Context, count: int, surface: str | None, name: str | None
) -> None:
    """Show the most recent events, oldest first.

    Plain output is one line per event: time (UTC), surface, level, event,
    then its fields. With --json, each line is a JSON object.

    \b
    Examples:
      sai logs tail
      sai logs tail -n 50 --surface mcp
      sai logs tail --event intent
      sai --json logs tail --event keeper.request
    """
    for line in log.tail(count, surface, name):
        if ctx.obj["json"]:
            click.echo(json.dumps(line, separators=(",", ":"), default=str))
        else:
            click.echo(log.render(line))


def print_log_trail(ctx: click.Context, report: dict[str, Any]) -> None:
    if ctx.obj["json"]:
        emit(ctx, report)
        return
    lines = sorted(report["events"] + report["gaps"], key=log.event_order)
    if not lines:
        click.echo("No matching log events.")
    for line in lines:
        prefix = "Gap  " if line.get("event") == "log.dropped" else ""
        click.echo(prefix + log.render(line))
    if warning := report.get("scopeWarning"):
        click.echo(warning)
    for line in report.get("unattributedGaps", ()):
        click.echo("Unattributed gap  " + log.render(line))
    if (count := report.get("unattributedGapCount", 0)) > len(
        report.get("unattributedGaps", ())
    ):
        click.echo(f"Unattributed drop notices in scan  {count}")
    print_log_limit(report)


def print_log_limit(report: dict[str, Any]) -> None:
    if report.get("truncated"):
        click.echo("Report truncated. " + report["more"])
        if before := report.get("nextBefore"):
            click.echo(f"Continue with --before {before}")


@logs_group.command("intent")
@click.argument("key")
@click.option("--offline", is_flag=True, help="Read local logs only.")
@click.option("--addr", help="EVM address to use for the Keeper lookup.")
@click.option("--before", help="Resume from a cursor or read before a UTC timestamp.")
@click.pass_context
def logs_intent(
    ctx: click.Context, key: str, offline: bool, addr: str | None, before: str | None
) -> None:
    """Show an intent's CLI and MCP trail and its current Keeper record."""
    endpoint = ctx.obj["flags"].get("keeper.endpoint") if offline else keeper_url(ctx)
    if offline and endpoint is not None:
        sai_settings.check_value("keeper.endpoint", endpoint, source="--endpoint")
    account = addr if addr is not None else ctx.obj["as_addr"]
    report = log_queries.intent(
        key, endpoint=endpoint, addr=account, offline=offline, before=before
    )
    print_log_trail(ctx, report)
    if ctx.obj["json"]:
        return
    if report["txHash"]:
        click.echo(f"Request transaction {report['txHash']}")
    if record := report["keeper"]:
        click.echo("Keeper " + json.dumps(record, separators=(",", ":")))
    elif error := report.get("keeperError"):
        click.echo(f"Keeper lookup failed ({error['code']}). {error['message']}")
    elif not offline:
        click.echo("Keeper has no record for this intent.")


@logs_group.command("run")
@click.argument("id")
@click.option("--before", help="Resume from a cursor or read before a UTC timestamp.")
@click.pass_context
def logs_run(ctx: click.Context, id: str, before: str | None) -> None:
    """Show one process run or one MCP call by ID."""
    print_log_trail(ctx, log_queries.run(id, before=before))


@logs_group.command("stats")
@click.option(
    "--since", metavar="DURATION", help="Look back by a duration such as 24h."
)
@click.option("--before", help="Resume from a cursor or read before a UTC timestamp.")
@click.pass_context
def logs_stats(ctx: click.Context, since: str | None, before: str | None) -> None:
    """Summarize Keeper latency and error codes in the retained logs."""
    report = log_queries.stats(since, before=before)
    if ctx.obj["json"]:
        emit(ctx, report)
        return
    if report["keeper"]:
        click.echo("Keeper operation  Requests  p50 (ms)  p95 (ms)")
        for name, values in report["keeper"].items():
            click.echo(
                f"{name:16}  {values['count']:8}  {values['p50Ms']:8}  {values['p95Ms']:8}"
            )
    else:
        click.echo("No timed Keeper requests.")
    click.echo("Errors by code" if report["errors"] else "No coded errors.")
    for code, count in report["errors"].items():
        click.echo(f"{code}  {count}")
    for line in report["gaps"]:
        click.echo("Gap  " + log.render(line))
    print_log_limit(report)


# Exit code for an install `sai update` cannot change in place.
EXIT_UNSUPPORTED = updater.EXIT_UNSUPPORTED


def update_report(
    installed: str, latest: update.Release | None, install: update.Install
) -> dict[str, Any]:
    """Describe the installed version next to LATEST, or next to no release
    when LATEST is None."""
    newer = latest is not None and update.is_newer(latest.tag, installed)
    return {
        "installed": installed,
        "latest": latest.version if latest else None,
        "latestTag": latest.tag if latest else None,
        "wheelUrl": latest.wheel_url if latest else None,
        "sha256sumsUrl": latest.sums_url if latest else None,
        "updateAvailable": newer,
        "install": install.to_json(),
        "update": asdict(update.guidance(install)) if newer else None,
    }


def print_update_report(report: dict[str, Any]) -> None:
    kind = report["install"]["kind"]
    click.echo(f"installed  {report['installed']} ({kind})")
    if report["latest"] is None:
        click.echo("latest     none")
        click.echo("No sai-cli release is published yet; nothing to update to.")
        return
    click.echo(f"latest     {report['latest']}")
    if not report["updateAvailable"]:
        click.echo("sai is up to date.")
        return
    guide = report["update"]
    click.echo(f"A newer version is available. {guide['note']}")
    if guide["command"]:
        click.echo(f"  {guide['command']}")


@main.group("skill")
def skill_group() -> None:
    """Read, locate, and install the agent skill packaged with this CLI.

    The skill is a SKILL.md router plus child pages for the CLI, the MCP
    server, trading safety, and Sai protocol references. It is edited in
    jiyuu and copied here; `sai skill source` names the commit.
    """


@skill_group.command("show")
@click.option(
    "--page",
    metavar="NAME",
    help="Print a child page instead, such as cli, mcp, trading-safety, or"
    " references/contracts.",
)
def skill_show(page: str | None) -> None:
    """Print the skill's SKILL.md, or one child page with --page.

    Output is the raw Markdown, also under --json.

    \b
    Examples:
      sai skill show
      sai skill show --page trading-safety
      sai skill show --page references/contracts
    """
    files = skill.packaged_files()
    data = files["SKILL.md"] if page is None else skill.find_page(files, page)
    click.echo(data, nl=False)


@skill_group.command("source")
@click.pass_context
def skill_source(ctx: click.Context) -> None:
    """Show where the packaged skill lives and where it came from.

    Prints the packaged path, its pages, the CLI and skill repositories, the
    jiyuu commit the skill was synced from, and the install commands.
    """
    info = skill.source_info(skill.packaged_files(), package_version())
    if ctx.obj["json"]:
        emit(ctx, info)
    else:
        click.echo(skill.render_source(info))


@skill_group.command("install")
@click.option(
    "--dir",
    "target",
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to write the skill into. [default: ~/.agents/skills/sai]",
)
@click.option(
    "--force",
    is_flag=True,
    help="Replace a symlink, or a sai skill whose content differs.",
)
@click.pass_context
def skill_install(ctx: click.Context, target: Path | None, force: bool) -> None:
    """Copy the packaged skill directory to where agents load skills.

    Writes SKILL.md and every child page into DIR itself. Running it again
    with the same version reports `unchanged`. It refuses to replace a
    symlink or a directory that differs unless --force is given, and never
    replaces a directory that is not a sai skill.

    \b
    Examples:
      sai skill install
      sai skill install --dir ~/.claude/skills/sai
      sai skill install --force
    """
    destination = (target or skill.default_dir()).expanduser()
    result = skill.install(skill.packaged_files(), destination, force)
    if ctx.obj["json"]:
        emit(ctx, result.as_dict())
    else:
        click.echo(skill.render_installed(result))


def log_update(outcome: updater.Outcome, *, background: bool) -> None:
    """Record what an update run did in the log's `update` surface."""
    log.event(
        "update.result",
        "warn" if outcome.exit_code else "info",
        status=outcome.status,
        installed=outcome.installed,
        target=outcome.target,
        rolledBack=outcome.rolled_back,
        background=background,
        message=log.untrusted(outcome.message),
        code=Code.INTERNAL_ERROR if outcome.exit_code else None,
    )


def check_update(ctx: click.Context, api_base: str, web_base: str) -> None:
    latest: update.Release | None
    try:
        latest = update.fetch_latest(api_base=api_base, web_base=web_base)
    except update.NoRelease:
        latest = None
    except update.UpdateError as exc:
        log.event(
            "update.check",
            "warn",
            ok=False,
            code=Code.INTERNAL_ERROR,
            error=log.untrusted(str(exc)),
        )
        raise SaiError(f"update check failed: {exc}") from exc
    # No release is a result too: it replaces a cached, withdrawn one.
    changes = {"latest": latest.tag if latest else None, "checkedAt": update.now()}
    if not updater.save_state(changes):
        warn("Warning: could not save the update check.")
    install = update.install_kind(web_base=web_base)
    report = update_report(package_version(), latest, install)
    log.event(
        "update.check",
        ok=True,
        installed=report["installed"],
        latest=report["latest"],
        updateAvailable=report["updateAvailable"],
        installKind=report["install"]["kind"],
    )
    if ctx.obj["json"]:
        emit(ctx, report)
    else:
        print_update_report(report)


@main.command("update")
@click.option("--check", is_flag=True, help="Report the latest version only.")
@click.option(
    "--to",
    "to_version",
    metavar="X.Y.Z",
    help="Install this release instead of the latest; the only way to go back.",
)
@click.option(
    "--source",
    is_flag=True,
    help="Build the release from Git instead of installing its wheel.",
)
@click.option("--background", is_flag=True, hidden=True)
@click.option("--api-base", default=update.API_BASE, hidden=True)
@click.option("--web-base", default=update.WEB_BASE, hidden=True)
@click.pass_context
def update_command(
    ctx: click.Context,
    check: bool,
    to_version: str | None,
    source: bool,
    background: bool,
    api_base: str,
    web_base: str,
) -> None:
    """Install the latest sai-cli release over this install.

    Downloads the release wheel, checks it against the release's SHA256SUMS,
    installs it with `uv tool install`, and then runs the new version. If any
    step fails, the previous version stays or is put back. The `sai` path and
    your Sai data stay in place. The update repairs stale MCP registrations.
    Restart agent clients to load the new version.

    Only a `uv tool install` of a release or of the official Git source is
    updated; a checkout, uvx, or other install gets a note and exit code 3.
    sai never moves to an older version unless `--to` names one.

    `--check` only reports the installed and latest versions. sai also
    updates by itself about once a day; `sai config set update.auto false`
    makes that notify only.
    """
    if check:
        if to_version or source or background:
            raise click.UsageError("--check takes no other update option.")
        check_update(ctx, api_base, web_base)
        return
    flags = ctx.find_root().obj["flags"]

    def auto() -> bool:
        """The update.auto setting, read when the install is about to start,
        so an opt-out made after this run began still holds. Windows locks
        the running sai.exe, so there the background run only notifies."""
        try:
            return sai_settings.load(flags).update_auto and os.name != "nt"
        except sai_settings.SettingsError:
            return False

    options = updater.Options(
        api_base=api_base,
        web_base=web_base,
        to=to_version,
        source=source,
        allowed=auto if background else None,
        background=background,
    )
    if background:
        # The detached worker has no terminal: its record is the log's
        # `update` surface and update.json, never stdout.
        with contextlib.suppress(KeyboardInterrupt):
            log_update(updater.update_now(options), background=True)
        return
    outcome = updater.update_now(options)
    log_update(outcome, background=False)
    if outcome.exit_code:
        raise SaiError(
            outcome.message,
            exit_code=outcome.exit_code,
            hint=update.INSTALL_COMMAND
            if outcome.exit_code == ExitCode.UNSUPPORTED
            else None,
        )
    if ctx.obj["json"]:
        emit(ctx, outcome.to_json())
    else:
        click.echo(outcome.message)
        if notice := updater.mcp_notice(outcome.mcp_results(), outcome.mcp_notes):
            click.echo(notice)


if __name__ == "__main__":
    main()
