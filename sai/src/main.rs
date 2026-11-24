use std::process::ExitCode;

use clap::{Parser, Subcommand};
use sai_core::config::list_config;
use serde_json::json;

mod local;

use local::{AuthCommand, IntentCommand, SettingsFlags};

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    #[arg(long, global = true, help = "Print machine-readable JSON")]
    json: bool,
    /// Override the Keeper GraphQL HTTP endpoint.
    #[arg(long)]
    endpoint: Option<String>,
    /// Override the Keeper WebSocket endpoint.
    #[arg(long)]
    ws_endpoint: Option<String>,
    /// Override the Nibiru REST endpoint.
    #[arg(long)]
    lcd_endpoint: Option<String>,
    /// Run as another connected account instead of the active account.
    #[arg(long = "as", value_name = "ADDR")]
    as_addr: Option<String>,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Manage local Sai sessions.
    Auth {
        #[command(subcommand)]
        command: AuthCommand,
    },
    /// Inspect trading request outcomes.
    Intent {
        #[command(subcommand)]
        command: IntentCommand,
    },
    /// Inspect the Sai config file.
    Config {
        #[command(subcommand)]
        command: ConfigCommand,
    },
}

#[derive(Subcommand)]
enum ConfigCommand {
    /// List the recognized keys present in config.toml.
    List,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let settings_flags = SettingsFlags {
        endpoint: cli.endpoint.as_deref(),
        ws_endpoint: cli.ws_endpoint.as_deref(),
        lcd_endpoint: cli.lcd_endpoint.as_deref(),
    };
    match cli.command {
        Command::Auth { command } => local_result(local::auth(
            command,
            cli.as_addr.as_deref(),
            settings_flags,
            cli.json,
        )),
        Command::Intent { command } => {
            local_result(local::intent(command, settings_flags, cli.json))
        }
        Command::Config {
            command: ConfigCommand::List,
        } => match list_config() {
            Ok(listing) => {
                if cli.json {
                    println!(
                        "{}",
                        json!({
                            "path": listing.path.to_string_lossy(),
                            "configuredKeys": listing.configured_keys,
                        })
                    );
                } else {
                    println!("# {}", listing.path.display());
                    for key in listing.configured_keys {
                        println!("{key}");
                    }
                }
                ExitCode::SUCCESS
            }
            Err(error) => {
                eprintln!("Error: {error}");
                ExitCode::FAILURE
            }
        },
    }
}

fn local_result(result: Result<(), Box<dyn std::error::Error>>) -> ExitCode {
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("Error: {error}");
            ExitCode::FAILURE
        }
    }
}
