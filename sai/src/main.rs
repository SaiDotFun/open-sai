mod config;
mod local;

use std::{env, path::Path, process, time::Instant};

use clap::{
    Parser, Subcommand,
    error::{ContextKind, ContextValue, ErrorKind},
};
use sai_core::errors::{ExitCode, SaiError};
use sai_core::log::{self, EXIT_FLUSH, EventContext, Field, Level, LogWriter, Surface};
use sai_core::settings::{Environment, Flags, Settings, SettingsError, repr};
use serde_json::{Value, json};

use config::ConfigCommand;
use local::{AuthCommand, IntentCommand};

#[derive(Parser)]
#[command(
    version,
    about,
    disable_help_subcommand = true,
    args_override_self = true
)]
struct Cli {
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
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Manage local Sai sessions.
    Auth {
        #[arg(long, global = true, help = "Print machine-readable JSON")]
        json: bool,
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
}

fn main() -> process::ExitCode {
    let args: Vec<_> = env::args_os().collect();
    let argv: Vec<_> = args
        .iter()
        .skip(1)
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    let (command, operands) = command_path(&argv);
    let environment = Environment::from_process();
    let home = environment.as_ref().ok().map(|env| env.home.as_path());
    let requested_json = json_requested(&argv);
    let started = Instant::now();
    let writer = if command == "logs"
        || argv
            .iter()
            .any(|flag| matches!(flag.as_str(), "--help" | "--version" | "--skill"))
    {
        None
    } else {
        environment
            .as_ref()
            .ok()
            .cloned()
            .and_then(LogWriter::start)
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
    let events = EventContext {
        writer: writer.as_ref(),
        surface: Surface::Cli,
        command: (!command.is_empty()).then_some(command),
    };
    if let Some(writer) = &writer {
        writer.event(
            Surface::Cli,
            "command.start",
            Level::Info,
            [
                ("command", command_field()),
                ("argv", Field::argv(argv.iter().take(64).cloned())),
            ],
        );
    }
    let outcome = match Cli::try_parse_from(args) {
        Ok(cli) => {
            let json = cli.json
                || matches!(
                    &cli.command,
                    Command::Auth { json: true, .. } | Command::Intent { json: true, .. }
                );
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
                        Command::Auth { command, .. } => {
                            local::auth(command, cli.account.as_deref(), &flags, env, json, events)
                                .map(|()| ExitCode::Ok)
                        }
                        Command::Intent { command, .. } => local::intent(
                            command,
                            cli.account.as_deref(),
                            &flags,
                            env,
                            json,
                            events,
                        ),
                        Command::Config { command } => {
                            command.run(&flags, env, json).map(|()| ExitCode::Ok)
                        }
                    }
                });
            match result {
                Ok(code) => (code, None),
                Err(error) => complete(Err(error), command, json, home),
            }
        }
        Err(error) => print_parse_error(&error, command, operands, requested_json, home),
    };
    finish(writer, command_field(), started, outcome)
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
            "auth" | "intent" => {
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

fn complete(
    result: Result<(), SaiError>,
    command: &str,
    json_output: bool,
    home: Option<&Path>,
) -> (ExitCode, Option<SaiError>) {
    let Err(error) = result else {
        return (ExitCode::Ok, None);
    };
    let redact = |text: &str| home.map_or_else(|| text.to_owned(), |home| log::redact(home, text));
    let message = redact(&error.message);
    if json_output {
        let mut body =
            json!({"code": error.code, "message": message, "retryable": error.retryable()});
        if let Some(key) = &error.intent_key {
            body["intentKey"] = json!(key);
        }
        if let Some(hint) = &error.hint {
            body["hint"] = json!(redact(hint));
        }
        if let Some(details) = &error.details {
            body["details"] = redact_details(&Value::Object((**details).clone()), &redact);
        }
        println!("{}", local::ascii_json(&json!({"error": body}).to_string()));
    } else {
        if error.exit_code == ExitCode::Usage {
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
        "config list" | "auth status" | "auth list" => "",
        "intent status" | "intent local" | "auth use" => " ADDR",
        "intent wait" => " KEY",
        "auth logout" => " [ADDR]",
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
    writer: Option<LogWriter>,
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
        writer.event(Surface::Cli, "command.end", level, fields);
        writer.finish(EXIT_FLUSH);
    }
    (code as u8).into()
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
            "logs" => return ("logs", &[]),
            "config" | "auth" | "intent" => {
                let group = match arg.as_str() {
                    "auth" => "auth",
                    "intent" => "intent",
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
                    ("intent", "local") => "intent local",
                    ("intent", "status") => "intent status",
                    ("intent", "wait") => "intent wait",
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
                    | "intent wait" | "auth use" | "auth logout" => 1,
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
            format!("Option {} requires an argument.", repr(argument))
        }
        _ => return None,
    };
    Some(SaiError::usage(message))
}

fn positional<'a>(operands: &'a [String], command: &str) -> Vec<&'a str> {
    let mut operands = operands.iter().map(String::as_str);
    let mut options = true;
    let mut positional = Vec::new();
    while let Some(arg) = operands.next() {
        match arg {
            "--" if options => options = false,
            "--key" if options && matches!(command, "intent status" | "intent wait") => {
                operands.next();
            }
            "--addr" | "--timeout" if options && command == "intent wait" => {
                operands.next();
            }
            arg if options
                && matches!(command, "intent status" | "intent wait")
                && arg.starts_with("--key=") => {}
            arg if options
                && command == "intent wait"
                && (arg.starts_with("--addr=") || arg.starts_with("--timeout=")) => {}
            "--json"
                if options && (command.starts_with("intent ") || command.starts_with("auth ")) => {}
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
    let help = || config::help(command).or_else(|| local::intent_help(command));
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
            if matches!(command, "config" | "intent" | "intent wait")
                && let Some(help) = help()
            {
                if json {
                    let error = if command == "intent wait" {
                        SaiError::usage("Missing argument 'KEY'.")
                    } else {
                        SaiError::usage(help)
                    };
                    return complete(Err(error), command, true, home);
                }
                eprintln!("{help}");
                return (ExitCode::Usage, Some(SaiError::usage(help)));
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
            } else {
                ""
            }
        }
        _ => command,
    };
    if let Some(usage) = usage_error(error, usage_command, operands) {
        if !json && error.kind() == ErrorKind::InvalidValue && usage_command.is_empty() {
            eprintln!("Error: {}", usage.message);
            return (ExitCode::Usage, Some(usage));
        }
        return complete(Err(usage), usage_command, json, home);
    }
    if json {
        let message = match error.kind() {
            ErrorKind::MissingSubcommand if command.is_empty() => {
                "Pass a command when using --json."
            }
            _ => "Invalid command arguments.",
        };
        return complete(Err(SaiError::usage(message)), usage_command, true, home);
    }
    let _ = error.print();
    (ExitCode::Usage, None)
}
