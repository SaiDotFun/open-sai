use std::process::ExitCode;

use clap::{Parser, Subcommand};
use sai_core::config::list_config;
use serde_json::json;

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    #[arg(long, global = true, help = "Print machine-readable JSON")]
    json: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
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
    match cli.command {
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
