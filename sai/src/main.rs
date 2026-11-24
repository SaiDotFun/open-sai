mod auth;
mod config;
mod local;
mod logs;
mod reads;
mod repl;
mod skill;

use std::{
    env,
    ffi::OsString,
    path::Path,
    process,
    time::{Duration, Instant},
};

use clap::{
    Parser, Subcommand,
    error::{ContextKind, ContextValue, ErrorKind},
};
use sai_core::errors::{Code, ExitCode, SaiError};
use sai_core::log::{self, EXIT_FLUSH, EventContext, Field, Level, LogWriter, Surface};
use sai_core::settings::{Environment, Flags, Settings, SettingsError, repr};
use serde_json::{Value, json};

use auth::AuthCommand;
use config::ConfigCommand;
use local::IntentCommand;
use reads::{AccountCommand, MarketCommand, WatchCommand};

#[derive(Parser)]
#[command(
    bin_name = "sai",
    version,
    about,
    disable_help_subcommand = true,
    args_override_self = true
)]
struct Cli {
    /// Print the bundled agent skill and exit.
    #[arg(long)]
    skill: bool,
    /// Print compact machine-readable JSON.
    #[arg(long)]
    json: bool,
    /// Keeper GraphQL HTTP endpoint.
    #[arg(long)]
    endpoint: Option<String>,
    /// Keeper WebSocket endpoint.
    #[arg(long)]
    ws_endpoint: Option<String>,
    /// Nibiru REST endpoint.
    #[arg(long)]
    lcd_endpoint: Option<String>,
    /// Run the command as another connected account.
    #[arg(long = "as", value_name = "ADDR")]
    account: Option<String>,
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Inspect markets and prices.
    Market {
        #[command(subcommand)]
        command: MarketCommand,
    },
    /// Inspect positions and account activity.
    Account {
        #[command(subcommand)]
        command: AccountCommand,
    },
    /// Stream live Keeper updates as JSON lines.
    Watch {
        #[command(subcommand)]
        command: WatchCommand,
    },
    /// Manage local Sai sessions.
    Auth {
        #[command(subcommand)]
        command: AuthCommand,
    },
    /// Inspect trading request outcomes.
    Intent {
        #[arg(long, global = true, help = "Print machine-readable JSON")]
        json: bool,
        #[command(subcommand)]
        command: IntentCommand,
    },
    /// Show and change settings in $SAI_HOME/config.toml.
    Config {
        #[command(subcommand)]
        command: ConfigCommand,
    },
    /// Read, locate, and install the bundled agent skill.
    Skill {
        #[command(subcommand)]
        command: skill::SkillCommand,
    },
    /// Read the retained JSON-lines logs.
    Logs {
        #[command(subcommand)]
        command: logs::LogsCommand,
    },
}

fn main() -> process::ExitCode {
    let environment = Environment::from_process();
    let writer = environment
        .as_ref()
        .ok()
        .cloned()
        .and_then(LogWriter::start);
    let code = execute(env::args_os().collect(), &environment, writer.as_ref());
    if let Some(writer) = writer {
        writer.finish(EXIT_FLUSH);
    }
    (code as u8).into()
}

fn execute(
    args: Vec<OsString>,
    environment: &Result<Environment, SettingsError>,
    writer: Option<&LogWriter>,
) -> ExitCode {
    let argv: Vec<_> = args
        .iter()
        .skip(1)
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    let (command, operands) = command_path(&argv);
    let home = environment.as_ref().ok().map(|env| env.home.as_path());
    let requested_json = json_requested(&argv);
    if eager_skill(&argv) {
        return complete(skill::show(None), "skill show", requested_json, home).0;
    }
    let started = Instant::now();
    if command.starts_with("logs")
        && let Some(writer) = writer
    {
        writer.flush(Duration::from_secs(1));
    }
    let events_writer = if command.starts_with("logs")
        || command == "skill show"
        || argv
            .iter()
            .any(|flag| matches!(flag.as_str(), "--help" | "--version" | "--skill"))
    {
        None
    } else {
        writer
    };
    let command_field = || {
        if command.is_empty() {
            if argv.is_empty() {
                json!("repl")
            } else {
                Value::Null
            }
        } else {
            json!(command)
        }
        .into()
    };
    let command_id = log::command_id();
    let events = EventContext {
        writer: events_writer,
        surface: Surface::Cli,
        command: (!command.is_empty()).then_some(command),
        command_id: Some(&command_id),
    };
    if let Some(writer) = events_writer {
        // Import accepts no operands, and a stray argument can contain a token.
        let logged_argv = if command == "auth import" {
            &argv[..argv.len() - operands.len()]
        } else {
            &argv
        };
        writer.event(
            Surface::Cli,
            "command.start",
            Level::Info,
            [
                ("command", command_field()),
                ("commandId", json!(command_id).into()),
                ("argv", Field::argv(logged_argv.iter().take(64).cloned())),
            ],
        );
    }
    let outcome = match Cli::try_parse_from(args) {
        Ok(cli) => {
            let json = cli.json || matches!(&cli.command, Some(Command::Intent { json: true, .. }));
            let result = environment
                .as_ref()
                .map_err(|error| SaiError::from(error.clone()))
                .and_then(|env| {
                    let flags = Flags {
                        endpoint: cli.endpoint,
                        ws_endpoint: cli.ws_endpoint,
                        lcd_endpoint: cli.lcd_endpoint,
                    };
                    match cli.command {
                        Some(Command::Auth { command }) => auth::dispatch(
                            command,
                            cli.account.as_deref(),
                            &flags,
                            env,
                            json,
                            events,
                        ),
                        Some(Command::Intent { command, .. }) => {
                            local::intent(command, &flags, env, json, events)
                        }
                        Some(Command::Config { command }) => command.run(&flags, env, json),
                        Some(Command::Skill { command }) => command.run(json),
                        Some(Command::Logs { command }) => {
                            command.run(env, &flags, cli.account.as_deref(), json)
                        }
                        None if cli.skill => skill::show(None),
                        None if json => Err(SaiError::usage("Pass a command when using --json.")),
                        None => repl::run(&env.home, &flags, cli.account.as_deref(), |args| {
                            execute(args, environment, writer);
                        }),
                        Some(Command::Market { command }) => command.run(&flags, env, json, events),
                        Some(Command::Account { command }) => {
                            command.run(&flags, env, json, events)
                        }
                        Some(Command::Watch { command }) => command.run(&flags, env, events),
                    }
                });
            complete(result, command, json, home)
        }
        Err(error) => print_parse_error(&error, command, operands, requested_json, home),
    };
    finish(
        events_writer,
        command_field(),
        &command_id,
        started,
        outcome,
    )
}

fn eager_skill(argv: &[String]) -> bool {
    let mut args = argv.iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--skill" => return true,
            "--endpoint" | "--ws-endpoint" | "--lcd-endpoint" | "--as" => {
                args.next();
            }
            "--help" | "--version" | "--" => break,
            arg if !arg.starts_with('-') => break,
            _ => {}
        }
    }
    false
}

fn json_requested(args: &[String]) -> bool {
    let mut args = args.iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--json" => return true,
            "--endpoint" | "--ws-endpoint" | "--lcd-endpoint" | "--as" => {
                args.next();
            }
            "--" => break,
            "intent" => {
                return args
                    .take_while(|arg| *arg != "--")
                    .any(|arg| arg == "--json");
            }
            arg if !arg.starts_with('-') => break,
            _ => {}
        }
    }
    false
}

fn load_settings(flags: &Flags, env: &Environment) -> Result<Settings, SettingsError> {
    let settings = Settings::load(flags, env)?;
    for warning in &settings.warnings {
        eprintln!("{warning}");
    }
    Ok(settings)
}

const INVALID_ARGUMENTS: &str = "Invalid command arguments.";

fn usage_diagnostic(text: &str) -> &str {
    if log::contains_url(text) {
        INVALID_ARGUMENTS
    } else {
        text
    }
}

fn complete(
    result: Result<(), SaiError>,
    command: &str,
    json_output: bool,
    home: Option<&Path>,
) -> (ExitCode, Option<SaiError>) {
    let Err(error) = result else {
        return (ExitCode::Ok, None);
    };
    if repl::active()
        && error.code == sai_core::errors::Code::InvalidRequest
        && error.message == "Aborted"
    {
        eprintln!("Interrupted");
        return (error.exit_code, Some(error));
    }
    let redact = |text: &str| {
        let text = if error.exit_code == ExitCode::Usage {
            usage_diagnostic(text)
        } else {
            text
        };
        home.map_or_else(|| text.to_owned(), |home| log::redact(home, text))
    };
    let message = redact(&error.message);
    let interrupted = error.code == Code::InvalidRequest && error.message == "Aborted";
    // Auth import emits its own newline on cancellation.
    if interrupted && command != "auth import" {
        eprintln!();
    }
    if json_output {
        let mut body =
            json!({"code": error.code, "message": message, "retryable": error.retryable()});
        if let Some(key) = &error.intent_key {
            body["intentKey"] = json!(redact(key));
        }
        if let Some(hint) = &error.hint {
            body["hint"] = json!(redact(hint));
        }
        if let Some(details) = &error.details {
            body["details"] = redact_details(&Value::Object((**details).clone()), &redact);
        }
        println!("{}", local::ascii_json(&json!({"error": body}).to_string()));
    } else if interrupted {
        eprintln!("Aborted!");
    } else {
        if error.exit_code == ExitCode::Usage && error.show_usage {
            eprintln!(
                "{}\nTry '{} --help' for help.\n",
                usage(command),
                command_line(command)
            );
        }
        eprintln!("Error: {message}");
    }
    (error.exit_code, Some(error))
}

fn usage(command: &str) -> String {
    let args = match command {
        "config get" | "config unset" => " KEY",
        "config set" => " KEY VALUE",
        "config list" | "auth status" | "auth list" | "auth import" | "market list"
        | "market prices" => "",
        "auth login" => " NAME",
        "intent status" | "intent local" | "auth use" | "account positions" | "account history"
        | "account balances" | "watch positions" | "watch balances" => " ADDR",
        "watch price" => " TOKEN_ID",
        "skill show" | "skill source" | "skill install" => "",
        "logs path" | "logs tail" | "logs stats" => "",
        "logs intent" => " KEY",
        "logs run" => " ID",
        "auth logout" | "auth renew" => " [ADDR]",
        "" => " [COMMAND] [ARGS]...",
        _ => " COMMAND [ARGS]...",
    };
    format!("Usage: {} [OPTIONS]{args}", command_line(command))
}

fn command_line(command: &str) -> String {
    if command.is_empty() {
        "sai".into()
    } else {
        format!("sai {command}")
    }
}

fn redact_details(value: &Value, redact: &impl Fn(&str) -> String) -> Value {
    match value {
        Value::Object(fields) => Value::Object(
            fields
                .iter()
                .map(|(key, item)| (redact(key), redact_details(item, redact)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(
            items
                .iter()
                .map(|item| redact_details(item, redact))
                .collect(),
        ),
        Value::String(text) => Value::String(redact(text)),
        other => other.clone(),
    }
}

fn finish(
    writer: Option<&LogWriter>,
    command: Field,
    command_id: &str,
    started: Instant,
    (code, error): (ExitCode, Option<SaiError>),
) -> ExitCode {
    if let Some(writer) = writer {
        let duration_ms = (started.elapsed().as_secs_f64() * 1_000.0).round_ties_even() as u64;
        let level = if code == ExitCode::Ok {
            Level::Info
        } else {
            Level::Warn
        };
        let mut fields = vec![
            ("command", command),
            ("commandId", json!(command_id).into()),
            ("durationMs", json!(duration_ms).into()),
            ("exitCode", json!(code as u8).into()),
        ];
        if let Some(error) = error {
            fields.push(("code", json!(error.code).into()));
            fields.push(("error", Field::Untrusted(error.message)));
            if let Some(key) = error.intent_key {
                fields.push(("intentKey", Field::Text(key)));
            }
        }
        writer.event(Surface::Cli, "command.end", level, fields);
    }
    code
}

fn command_path(args: &[String]) -> (&'static str, &[String]) {
    let mut skip = false;
    for (index, arg) in args.iter().enumerate() {
        if skip {
            skip = false;
            continue;
        }
        match arg.as_str() {
            "--endpoint" | "--ws-endpoint" | "--lcd-endpoint" | "--as" => skip = true,
            "config" | "auth" | "intent" | "skill" | "logs" | "market" | "account" | "watch" => {
                let group = match arg.as_str() {
                    "auth" => "auth",
                    "intent" => "intent",
                    "skill" => "skill",
                    "logs" => "logs",
                    "market" => "market",
                    "account" => "account",
                    "watch" => "watch",
                    _ => "config",
                };
                let Some((index, leaf)) = args
                    .iter()
                    .enumerate()
                    .skip(index + 1)
                    .find(|(_, arg)| !arg.starts_with('-'))
                else {
                    return (group, &[]);
                };
                let path = match (group, leaf.as_str()) {
                    ("config", "get") => "config get",
                    ("config", "list") => "config list",
                    ("config", "set") => "config set",
                    ("config", "unset") => "config unset",
                    ("auth", "status") => "auth status",
                    ("auth", "list") => "auth list",
                    ("auth", "use") => "auth use",
                    ("auth", "logout") => "auth logout",
                    ("auth", "login") => "auth login",
                    ("auth", "import") => "auth import",
                    ("auth", "renew") => "auth renew",
                    ("intent", "local") => "intent local",
                    ("intent", "status") => "intent status",
                    ("skill", "show") => "skill show",
                    ("skill", "source") => "skill source",
                    ("skill", "install") => "skill install",
                    ("logs", "path") => "logs path",
                    ("logs", "tail") => "logs tail",
                    ("logs", "intent") => "logs intent",
                    ("logs", "run") => "logs run",
                    ("logs", "stats") => "logs stats",
                    ("market", "list") => "market list",
                    ("market", "prices") => "market prices",
                    ("account", "positions") => "account positions",
                    ("account", "history") => "account history",
                    ("account", "balances") => "account balances",
                    ("watch", "positions") => "watch positions",
                    ("watch", "balances") => "watch balances",
                    ("watch", "price") => "watch price",
                    _ => group,
                };
                return (path, &args[index + 1..]);
            }
            _ if arg.starts_with('-') => {}
            _ => break,
        }
    }
    ("", &[])
}

fn context_text(error: &clap::Error, kind: ContextKind) -> Option<&str> {
    if let Some(ContextValue::String(value)) = error.get(kind) {
        Some(value)
    } else {
        None
    }
}

fn usage_error(error: &clap::Error, command: &str, operands: &[String]) -> Option<SaiError> {
    let arg = context_text(error, ContextKind::InvalidArg);
    if command == "auth import"
        && !(error.kind() == ErrorKind::UnknownArgument
            && arg == Some("--json")
            && operands.first().is_some_and(|arg| arg == "--json"))
    {
        return Some(SaiError::usage("Unexpected arguments to auth import."));
    }
    let message = match error.kind() {
        ErrorKind::InvalidSubcommand => {
            let command = context_text(error, ContextKind::InvalidSubcommand)?;
            format!("No such command {}.", repr(command))
        }
        ErrorKind::UnknownArgument => {
            let argument = arg?;
            if argument.starts_with('-') {
                format!("No such option {}.", repr(argument))
            } else {
                let consumed = match command {
                    "config get" | "config unset" | "intent status" | "intent local"
                    | "auth use" | "auth logout" | "auth login" | "auth renew" | "logs intent"
                    | "logs run" | "account positions" | "account history" | "account balances"
                    | "watch positions" | "watch balances" | "watch price" => 1,
                    "config set" => 2,
                    _ => 0,
                };
                let extra = positional(operands, command)
                    .into_iter()
                    .skip(consumed)
                    .collect::<Vec<_>>();
                if extra.len() > 1 {
                    format!("Got unexpected extra arguments ({})", extra.join(" "))
                } else {
                    format!("Got unexpected extra argument ({argument})")
                }
            }
        }
        ErrorKind::MissingRequiredArgument => {
            let Some(ContextValue::Strings(args)) = error.get(ContextKind::InvalidArg) else {
                return None;
            };
            let first = args.first()?.trim_matches(['<', '>']);
            format!("Missing argument {}.", repr(first))
        }
        ErrorKind::InvalidValue => {
            let argument = arg?.split(' ').next()?;
            if command == "logs tail"
                && argument == "--surface"
                && let Some(value) =
                    context_text(error, ContextKind::InvalidValue).filter(|value| !value.is_empty())
            {
                return Some(SaiError::usage(format!(
                    "Invalid value for '--surface': {} is not one of 'cli', 'mcp', 'update'.",
                    repr(value)
                )));
            }
            format!("Option {} requires an argument.", repr(argument))
        }
        ErrorKind::ValueValidation if command == "logs tail" => {
            std::error::Error::source(error)?.to_string()
        }
        _ => return None,
    };
    Some(SaiError::usage(message))
}

fn positional<'a>(operands: &'a [String], command: &str) -> Vec<&'a str> {
    let mut operands = operands.iter().map(String::as_str);
    let mut options = true;
    let mut positional = Vec::new();
    let websocket = command.starts_with("watch ") || command == "account balances";
    while let Some(arg) = operands.next() {
        match arg {
            "--" if options => options = false,
            "--key" if options && command == "intent status" => {
                operands.next();
            }
            "--addr" if options && command == "auth login" => {
                operands.next();
            }
            arg if options && command == "intent status" && arg.starts_with("--key=") => {}
            arg if options && command == "auth login" && arg.starts_with("--addr=") => {}
            "--ws-endpoint" if options && websocket => {
                operands.next();
            }
            arg if options && websocket && arg.starts_with("--ws-endpoint=") => {}
            "--json" if options && command.starts_with("intent ") => {}
            _ => positional.push(arg),
        }
    }
    positional
}

fn print_parse_error(
    error: &clap::Error,
    command: &str,
    operands: &[String],
    json: bool,
    home: Option<&Path>,
) -> (ExitCode, Option<SaiError>) {
    let help = || {
        logs::help(command)
            .or_else(|| skill::help(command))
            .map(str::to_owned)
            .or_else(|| config::help(command))
            .or_else(|| local::intent_help(command))
            .or_else(|| auth::help(command))
            .or_else(|| reads::help(command))
    };
    match error.kind() {
        ErrorKind::DisplayHelp => {
            if let Some(help) = help() {
                println!("{help}");
            } else {
                let _ = error.print();
            }
            return (ExitCode::Ok, None);
        }
        ErrorKind::DisplayVersion => {
            let _ = error.print();
            return (ExitCode::Ok, None);
        }
        ErrorKind::DisplayHelpOnMissingArgumentOrSubcommand | ErrorKind::MissingSubcommand => {
            if matches!(command, "logs" | "skill")
                && let Some(help) = help()
            {
                return complete(Err(SaiError::usage(help)), command, json, home);
            }
            if matches!(
                command,
                "config" | "intent" | "auth" | "market" | "account" | "watch"
            ) && let Some(help) = help()
            {
                let error = SaiError::usage(help);
                if json || matches!(command, "market" | "account" | "watch") {
                    return complete(Err(error), command, json, home);
                }
                eprintln!("{}", error.message);
                return (ExitCode::Usage, Some(error));
            }
        }
        ErrorKind::MissingRequiredArgument if !json && operands.is_empty() => {
            let missing_help = if matches!(command, "logs run" | "logs intent") {
                help()
            } else if matches!(command, "auth login" | "auth use") {
                auth::help(command)
            } else {
                reads::help(command)
            };
            if let Some(help) = missing_help {
                eprintln!("{help}");
                return (ExitCode::Usage, None);
            }
        }
        _ => {}
    }
    let usage_command = match error.get(ContextKind::Usage) {
        Some(ContextValue::StyledStr(usage)) => {
            let usage = usage.to_string();
            let path = usage.trim_start_matches("Usage: sai").trim();
            if path == command || path.starts_with(&format!("{command} ")) {
                command
            } else if path == "config" || path.starts_with("config ") {
                "config"
            } else if path == "auth" || path.starts_with("auth ") {
                "auth"
            } else {
                ""
            }
        }
        _ => command,
    };
    if let Some(usage) = usage_error(error, usage_command, operands) {
        if !json && error.kind() == ErrorKind::InvalidValue && usage.message.starts_with("Option ")
        {
            eprintln!("Error: {}", usage_diagnostic(&usage.message));
            return (ExitCode::Usage, Some(usage));
        }
        return complete(Err(usage), usage_command, json, home);
    }
    if json {
        let message = match error.kind() {
            ErrorKind::MissingSubcommand if command.is_empty() => {
                "Pass a command when using --json."
            }
            _ => INVALID_ARGUMENTS,
        };
        return complete(Err(SaiError::usage(message)), usage_command, true, home);
    }
    if log::contains_url(&error.to_string()) {
        return complete(
            Err(SaiError::usage(INVALID_ARGUMENTS)),
            usage_command,
            false,
            home,
        );
    }
    let _ = error.print();
    (ExitCode::Usage, None)
}
