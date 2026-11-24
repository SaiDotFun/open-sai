mod config;

use std::{env, process::ExitCode, time::Instant};

use clap::{
    Parser, Subcommand,
    error::{ContextKind, ContextValue, ErrorKind},
};
use sai_core::log::{EXIT_FLUSH, Field, Level, LogWriter, Surface};
use sai_core::settings::{Environment, Flags, repr};
use serde_json::{Value, json};

use config::{ConfigCommand, Error};

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
    /// Show and change settings in $SAI_HOME/config.toml.
    Config {
        #[command(subcommand)]
        command: ConfigCommand,
    },
}

fn main() -> ExitCode {
    let args: Vec<_> = env::args_os().collect();
    let argv: Vec<_> = args
        .iter()
        .skip(1)
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    let command = config_command(&argv);
    let environment = Environment::from_process();
    let started = Instant::now();
    let writer = if command == "logs"
        || argv.iter().any(|flag| {
            matches!(flag.as_str(), "--help" | "--version" | "--skill")
        }) {
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
    let result = match Cli::try_parse_from(args) {
        Ok(cli) => environment
            .as_ref()
            .map_err(|error| Error {
                code: 1,
                message: error.to_string(),
            })
            .and_then(|env| {
                let flags = Flags {
                    endpoint: cli.endpoint,
                    ws_endpoint: cli.ws_endpoint,
                    lcd_endpoint: cli.lcd_endpoint,
                };
                match cli.command {
                    Command::Config { command } => {
                        command.run(&flags, env, cli.json)
                    }
                }
            }),
        Err(error) => {
            let context = if command == "logs" { "" } else { command };
            return finish(
                writer,
                command_field(),
                started,
                print_parse_error(&error, context, &argv),
            );
        }
    };
    finish(writer, command_field(), started, complete(result, command))
}

fn complete(result: Result<(), Error>, command: &str) -> (u8, Option<String>) {
    match result {
        Ok(()) => (0, None),
        Err(error) => {
            error.print(command);
            (error.code, Some(error.message))
        }
    }
}

fn finish(
    writer: Option<LogWriter>,
    command: Field,
    started: Instant,
    (code, error): (u8, Option<String>),
) -> ExitCode {
    if let Some(writer) = writer {
        let duration_ms =
            (started.elapsed().as_secs_f64() * 1_000.0).round() as u64;
        let level = if code == 0 { Level::Info } else { Level::Warn };
        let mut fields = vec![
            ("command", command),
            ("durationMs", json!(duration_ms).into()),
            ("exitCode", json!(code).into()),
        ];
        if let Some(error) = error {
            fields.push(("error", Field::Untrusted(error)));
        }
        writer.event(Surface::Cli, "command.end", level, fields);
        writer.finish(EXIT_FLUSH);
    }
    code.into()
}

fn config_command(args: &[String]) -> &'static str {
    let mut skip = false;
    for (index, arg) in args.iter().enumerate() {
        if skip {
            skip = false;
            continue;
        }
        match arg.as_str() {
            "--endpoint" | "--ws-endpoint" | "--lcd-endpoint" | "--as" => {
                skip = true
            }
            "logs" => return "logs",
            "config" => {
                return match args
                    .iter()
                    .skip(index + 1)
                    .find(|arg| !arg.starts_with('-'))
                    .map(String::as_str)
                {
                    Some("get") => "config get",
                    Some("list") => "config list",
                    Some("set") => "config set",
                    Some("unset") => "config unset",
                    _ => "config",
                };
            }
            _ if arg.starts_with('-') => {}
            _ => break,
        }
    }
    ""
}

fn context_text(error: &clap::Error, kind: ContextKind) -> Option<&str> {
    if let Some(ContextValue::String(value)) = error.get(kind) {
        Some(value)
    } else {
        None
    }
}

fn usage_error(
    error: &clap::Error,
    command: &str,
    argv: &[String],
) -> Option<Error> {
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
                let leaf = command.split_whitespace().last();
                let extra = argv
                    .iter()
                    .skip_while(|arg| leaf.is_some_and(|leaf| *arg != leaf))
                    .skip_while(|arg| *arg != argument)
                    .cloned()
                    .collect::<Vec<_>>();
                if extra.len() > 1 {
                    format!(
                        "Got unexpected extra arguments ({})",
                        extra.join(" ")
                    )
                } else {
                    format!("Got unexpected extra argument ({argument})")
                }
            }
        }
        ErrorKind::MissingRequiredArgument => {
            let Some(ContextValue::Strings(args)) =
                error.get(ContextKind::InvalidArg)
            else {
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
    Some(Error::usage(message))
}

fn print_parse_error(
    error: &clap::Error,
    command: &str,
    argv: &[String],
) -> (u8, Option<String>) {
    match error.kind() {
        ErrorKind::DisplayHelp if config::print_help(command, false) => {
            (0, None)
        }
        ErrorKind::DisplayHelpOnMissingArgumentOrSubcommand
        | ErrorKind::MissingSubcommand
            if command == "config" =>
        {
            config::print_help(command, true);
            (2, None)
        }
        _ => {
            let usage_command = match error.get(ContextKind::Usage) {
                Some(ContextValue::StyledStr(usage)) => {
                    let usage = usage.to_string();
                    let path = usage.trim_start_matches("Usage: sai").trim();
                    if path == "config" || path.starts_with("config ") {
                        if path == command
                            || path.starts_with(&format!("{command} "))
                        {
                            command
                        } else {
                            "config"
                        }
                    } else {
                        ""
                    }
                }
                _ => command,
            };
            if let Some(usage) = usage_error(error, usage_command, argv) {
                if error.kind() == ErrorKind::InvalidValue
                    && usage_command.is_empty()
                {
                    eprintln!("Error: {}", usage.message);
                    return (usage.code, None);
                }
                return complete(Err(usage), usage_command);
            }
            let _ = error.print();
            (error.exit_code() as u8, None)
        }
    }
}
