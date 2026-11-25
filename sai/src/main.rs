mod auth;
mod config;
mod doctor;
mod local;
mod logs;
mod mcp;
mod mcp_registration;
mod reads;
mod repl;
mod skill;
mod trade;
#[cfg(unix)]
mod update;

use std::{
    cell::Cell,
    env,
    ffi::OsString,
    path::Path,
    process,
    sync::Arc,
    time::{Duration, Instant},
};

use clap::{
    CommandFactory, Parser, Subcommand,
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
    /// Install the latest sai-cli release over this install.
    #[cfg(unix)]
    Update(update::UpdateCommand),
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
    /// Run Sai as an MCP server for agent clients.
    Mcp(mcp_registration::McpArgs),
    /// Check the install, config, MCP registrations, session, intent ledger, and endpoints.
    Doctor(doctor::DoctorArgs),
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
    /// Submit authenticated trading intents.
    Trade {
        #[command(subcommand)]
        command: trade::TradeCommand,
    },
}

impl Cli {
    fn run(
        self,
        env: &Environment,
        json: bool,
        events: EventContext<'_>,
        operands: &[String],
        start_log: impl FnOnce(),
        writer: Option<&Arc<LogWriter>>,
    ) -> Result<ExitCode, SaiError> {
        let flags = Flags {
            endpoint: self.endpoint,
            ws_endpoint: self.ws_endpoint,
            lcd_endpoint: self.lcd_endpoint,
        };
        match self.command {
            #[cfg(unix)]
            Some(Command::Update(command)) => command.run(&flags, env, json, events),
            Some(Command::Auth { command }) => {
                auth::dispatch(command, self.account.as_deref(), &flags, env, json, events)
            }
            Some(Command::Intent { command, .. }) => {
                return local::intent(command, self.account.as_deref(), &flags, env, json, events);
            }
            Some(Command::Config { command }) => command.run(&flags, env, json),
            Some(Command::Mcp(args)) => args.run(
                flags,
                env,
                json,
                operands.iter().any(|arg| arg == "--"),
                writer.cloned(),
            ),
            Some(Command::Doctor(args)) => args.run(flags, env, json, events, start_log),
            Some(Command::Market { command }) => command.run(&flags, env, json, events),
            Some(Command::Account { command }) => {
                command.run(self.account.as_deref(), &flags, env, json, events)
            }
            Some(Command::Watch { command }) => command.run(&flags, env, events),
            Some(Command::Skill { command }) => command.run(json),
            Some(Command::Logs { command }) => {
                command.run(env, &flags, self.account.as_deref(), json)
            }
            Some(Command::Trade { command }) => {
                return trade::run(command, self.account.as_deref(), &flags, env, json, events);
            }
            None if self.skill => skill::show(None),
            None if json => Err(SaiError::usage("Pass a command when using --json.")),
            None => {
                if let Some(writer) = writer {
                    writer.flush(Duration::from_secs(1));
                }
                let environment = Ok(env.clone());
                repl::run(&env.home, &flags, self.account.as_deref(), |args| {
                    let _ = execute(args, &environment);
                })
            }
        }
        .map(|()| ExitCode::Ok)
    }
}

fn main() -> process::ExitCode {
    let args: Vec<_> = env::args_os().collect();
    execute(args, &Environment::from_process())
}

fn execute(
    args: Vec<OsString>,
    environment: &Result<Environment, SettingsError>,
) -> process::ExitCode {
    let argv: Vec<_> = args
        .iter()
        .skip(1)
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    let (command, operands) = command_path(&argv);
    let command = command.as_str();
    let auth_attempt = command.split(' ').next() == Some("auth")
        || (command.is_empty() && argv.iter().any(|arg| arg == "auth"));
    let home = environment.as_ref().ok().map(|env| env.home.as_path());
    let requested_json = json_requested(&argv);
    if eager_skill(&argv) {
        let outcome = complete(skill::show(None), Some("skill show"), requested_json, home);
        return (outcome.0 as u8).into();
    }
    let event_command = if argv.is_empty() {
        Some("repl")
    } else {
        (!command.is_empty()).then_some(command)
    };
    let started = Instant::now();
    let surface = match command {
        "mcp serve" => Surface::Mcp,
        "update" => Surface::Update,
        _ => Surface::Cli,
    };
    let writer = if command == "logs"
        || command.starts_with("logs ")
        || command == "skill show"
        || command == "doctor"
            && (operands.iter().any(|arg| arg == "--no-fix")
                || environment
                    .as_ref()
                    .is_ok_and(|env| !doctor::safe_log_path(env)))
        || argv
            .iter()
            .any(|flag| matches!(flag.as_str(), "--help" | "--version" | "--skill"))
    {
        None
    } else {
        let start = match command {
            "doctor" | "mcp install" | "mcp list" | "mcp uninstall" => {
                LogWriter::start_diagnostic_command
            }
            _ => LogWriter::start_command,
        };
        environment
            .as_ref()
            .ok()
            .cloned()
            .and_then(start)
            .map(Arc::new)
    };
    let command_field = || json!(event_command).into();
    let events = EventContext::new(writer.as_deref(), surface, event_command);
    let log_started = Cell::new(false);
    let start_log = || {
        if let Some(writer) = &writer
            && !log_started.replace(true)
        {
            let logged_argv = if auth_attempt {
                Field::argv(command.split_whitespace().map(str::to_owned))
            } else {
                Field::argv(argv.iter().take(64).cloned())
            };
            writer.event(
                surface,
                "command.start",
                Level::Info,
                [("command", command_field()), ("argv", logged_argv)],
            );
        }
    };
    if command != "doctor" {
        start_log();
    }
    let outcome = match Cli::try_parse_from(args) {
        Ok(cli) => {
            let json = cli.json || matches!(&cli.command, Some(Command::Intent { json: true, .. }));
            let usage_command =
                (!matches!(&cli.command, Some(Command::Doctor(_)))).then_some(command);
            let result = environment
                .as_ref()
                .map_err(|error| SaiError::from(error.clone()))
                .and_then(|env| {
                    #[cfg(unix)]
                    update::tick(command, json, &env.home, events);
                    cli.run(env, json, events, operands, start_log, writer.as_ref())
                });
            match result {
                Ok(code) => (code, None),
                Err(error) => complete(
                    Err(error),
                    usage_command,
                    json && command != "mcp serve",
                    home,
                ),
            }
        }
        Err(error) => print_parse_error(
            &error,
            command,
            operands,
            requested_json,
            home,
            auth_attempt,
        ),
    };
    start_log();
    finish(
        writer.as_deref(),
        surface,
        command_field(),
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

fn complete(
    result: Result<(), SaiError>,
    usage_command: Option<&str>,
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
    let redact = |text: &str| home.map_or_else(|| text.to_owned(), |home| log::redact(home, text));
    let message = redact(&error.message);
    let interrupted = error.code == Code::InvalidRequest && error.message == "Aborted";
    // Auth import emits its own newline on cancellation.
    if interrupted && usage_command != Some("auth import") {
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
        if error.exit_code == ExitCode::Usage
            && error.show_usage
            && let Some(command) = usage_command
        {
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
        "config list" | "auth status" | "auth list" | "auth import" | "doctor" | "mcp list"
        | "mcp serve" | "market list" | "market prices" | "trade open" | "account overview"
        | "update" => "",
        "mcp install" | "mcp uninstall" => " [CLIENTS]...",
        "auth login" => " NAME",
        "intent status" | "intent local" | "auth use" | "account positions" | "account history"
        | "account balances" | "watch positions" | "watch balances" => " ADDR",
        "watch price" => " TOKEN_ID",
        "skill show" | "skill source" | "skill install" => "",
        "logs path" | "logs tail" | "logs stats" => "",
        "logs intent" => " KEY",
        "logs run" => " ID",
        "trade close"
        | "trade cancel"
        | "trade leverage"
        | "trade tpsl"
        | "trade margin"
        | "trade partial-close" => " TRADE_ID",
        "intent wait" => " KEY",
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
    surface: Surface,
    command: Field,
    started: Instant,
    (code, error): (ExitCode, Option<SaiError>),
) -> process::ExitCode {
    if let Some(writer) = writer {
        let duration_ms = (started.elapsed().as_secs_f64() * 1_000.0).round_ties_even() as u64;
        let level = if code == ExitCode::Ok {
            Level::Info
        } else {
            Level::Warn
        };
        let mut fields = vec![
            ("command", command),
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
        writer.event(surface, "command.end", level, fields);
        writer.finish(EXIT_FLUSH);
    }
    (code as u8).into()
}

// Recover the command name even when clap rejects its arguments.
fn command_path(args: &[String]) -> (String, &[String]) {
    let mut skip = false;
    for (index, arg) in args.iter().enumerate() {
        if skip {
            skip = false;
            continue;
        }
        match arg.as_str() {
            "--endpoint" | "--ws-endpoint" | "--lcd-endpoint" | "--as" => skip = true,
            _ if arg.starts_with('-') => {}
            _ => {
                let tree = Cli::command();
                let Some(mut node) = tree.find_subcommand(arg) else {
                    break;
                };
                let mut path = node.get_name().to_owned();
                let mut operands = &args[index + 1..];
                while node.has_subcommands() {
                    let Some((index, word)) = operands
                        .iter()
                        .enumerate()
                        .find(|(_, arg)| !arg.starts_with('-'))
                    else {
                        return (path, &[]);
                    };
                    operands = &operands[index + 1..];
                    let Some(child) = node.find_subcommand(word) else {
                        break;
                    };
                    path.push(' ');
                    path.push_str(child.get_name());
                    node = child;
                }
                return (path, operands);
            }
        }
    }
    (String::new(), &[])
}

fn context_text(error: &clap::Error, kind: ContextKind) -> Option<&str> {
    if let Some(ContextValue::String(value)) = error.get(kind) {
        Some(value)
    } else {
        None
    }
}

// Return a diagnostic that is safe for both terminal output and the log.
fn auth_usage_error(error: &clap::Error, command: &str) -> SaiError {
    let option = (error.kind() == ErrorKind::UnknownArgument)
        .then(|| context_text(error, ContextKind::InvalidArg))
        .flatten()
        .filter(|arg| arg.starts_with('-'));
    let message = if option == Some("--json") {
        "No such option: --json"
    } else if command == "auth import" {
        "Unexpected arguments to auth import."
    } else if option.is_some() {
        "No such option for auth."
    } else if error.kind() == ErrorKind::MissingRequiredArgument
        && let Some(missing) = usage_error(error, command)
    {
        return missing;
    } else {
        "Invalid arguments to auth."
    };
    SaiError::usage(message)
}

fn usage_error(error: &clap::Error, command: &str) -> Option<SaiError> {
    let arg = context_text(error, ContextKind::InvalidArg);
    let message = match error.kind() {
        ErrorKind::InvalidSubcommand => "No such command.".into(),
        ErrorKind::UnknownArgument if arg == Some("--json") => "No such option '--json'.".into(),
        ErrorKind::UnknownArgument if arg.is_some_and(|arg| arg.starts_with('-')) => {
            "No such option.".into()
        }
        ErrorKind::UnknownArgument => "Got unexpected extra arguments.".into(),
        ErrorKind::MissingRequiredArgument => {
            let Some(ContextValue::Strings(args)) = error.get(ContextKind::InvalidArg) else {
                return None;
            };
            // Click reports an omitted positional argument before defaulted options.
            if let Some(positional) = args.iter().find(|arg| arg.starts_with('<')) {
                format!(
                    "Missing argument {}.",
                    repr(positional.trim_matches(['<', '>']))
                )
            } else {
                let first = args.first()?;
                if matches!(command, "trade margin" | "trade partial-close")
                    && let Some(error) = trade::missing_position_option(first)
                {
                    return Some(error);
                }
                let option = first.split_whitespace().next()?;
                format!("Missing option {}.", repr(option))
            }
        }
        ErrorKind::InvalidValue if context_text(error, ContextKind::InvalidValue) == Some("") => {
            let argument = arg?.split_whitespace().next()?;
            format!("Option {} requires an argument.", repr(argument))
        }
        ErrorKind::InvalidValue | ErrorKind::ValueValidation | ErrorKind::TooManyValues => {
            // InvalidArg is the argument declaration, not the supplied value.
            let argument = arg?.split_whitespace().next()?;
            format!("Invalid value for {}.", repr(argument))
        }
        _ => return None,
    };
    Some(SaiError::usage(message))
}

fn clap_help(command: &str) -> Option<String> {
    let args = std::iter::once("sai")
        .chain(command.split_whitespace())
        .chain(std::iter::once("--help"));
    let help = Cli::command().try_get_matches_from(args).err()?;
    (help.kind() == ErrorKind::DisplayHelp).then(|| help.to_string().trim_end_matches('\n').into())
}

fn print_parse_error(
    error: &clap::Error,
    command: &str,
    operands: &[String],
    json: bool,
    home: Option<&Path>,
    auth_attempt: bool,
) -> (ExitCode, Option<SaiError>) {
    let help = || {
        logs::help(command)
            .or_else(|| skill::help(command))
            .map(str::to_owned)
            .or_else(|| config::help(command))
            .or_else(|| local::intent_help(command))
            .or_else(|| auth::help(command))
            .or_else(|| reads::help(command))
            .or_else(|| trade::help(command))
    };
    match error.kind() {
        ErrorKind::DisplayHelp => {
            if command == "trade" {
                println!("{}", trade::group_help(true));
            } else if let Some(help) = help() {
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
        ErrorKind::DisplayHelpOnMissingArgumentOrSubcommand | ErrorKind::MissingSubcommand
            if !command.is_empty() && operands.is_empty() =>
        {
            let message = match command {
                "auth" => Some("Invalid arguments to auth.".into()),
                "trade" => Some(trade::group_help(false)),
                _ => help().or_else(|| clap_help(command)),
            };
            if let Some(message) = message {
                return complete(Err(SaiError::usage(message)), Some(command), json, home);
            }
        }
        ErrorKind::MissingRequiredArgument if !json && operands.is_empty() => {
            if let Some(help) = help().or_else(|| clap_help(command)) {
                eprintln!("{help}");
                return (ExitCode::Usage, None);
            }
        }
        _ => {}
    }
    if matches!(command, "mcp install" | "mcp uninstall")
        && error.kind() == ErrorKind::InvalidValue
        && context_text(error, ContextKind::InvalidArg)
            .is_some_and(|arg| arg.split(' ').next() == Some("--scope"))
        && let Some(value) = context_text(error, ContextKind::InvalidValue)
        && let Some(ContextValue::Strings(choices)) = error.get(ContextKind::ValidValue)
    {
        let explicit_empty = operands.iter().any(|arg| arg == "--scope=")
            || operands
                .windows(2)
                .any(|args| args[0] == "--scope" && args[1].is_empty());
        if value.is_empty() && !explicit_empty {
            let usage = SaiError::usage("Option '--scope' requires an argument.");
            if !json {
                eprintln!("Error: {}", usage.message);
                return (ExitCode::Usage, Some(usage));
            }
            return complete(Err(usage), Some(command), json, home);
        }
        let choices = choices
            .iter()
            .map(|choice| repr(choice))
            .collect::<Vec<_>>()
            .join(", ");
        let usage = SaiError::usage(format!(
            "Invalid value for '--scope': expected one of {choices}."
        ));
        return complete(Err(usage), Some(command), json, home);
    }
    let usage_command = match error.get(ContextKind::Usage) {
        Some(ContextValue::StyledStr(usage)) => {
            let usage = usage.to_string();
            let path = usage
                .strip_prefix("Usage: ")
                .and_then(|usage| usage.split_once(' '))
                .map_or("", |(_, path)| path.trim());
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
    if auth_attempt {
        return complete(
            Err(auth_usage_error(error, command)),
            Some(usage_command),
            json,
            home,
        );
    }
    if let Some(usage) = usage_error(error, usage_command) {
        if !json && error.kind() == ErrorKind::InvalidValue && usage.message.starts_with("Option ")
        {
            eprintln!("Error: {}", usage.message);
            return (ExitCode::Usage, Some(usage));
        }
        return complete(Err(usage), Some(usage_command), json, home);
    }
    complete(
        Err(SaiError::usage(INVALID_ARGUMENTS)),
        Some(usage_command),
        json,
        home,
    )
}
