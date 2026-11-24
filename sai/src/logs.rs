use std::path::PathBuf;

use clap::Subcommand;
use sai_core::{
    errors::SaiError,
    log::query,
    settings::{Environment, Flags, Key},
};
use serde_json::Value;

use crate::{load_settings, local};

#[derive(Subcommand)]
pub enum LogsCommand {
    /// Print the logs directory and, with --json, its settings and files.
    Path,
    /// Show the most recent events, oldest first.
    Tail {
        #[arg(short = 'n', default_value_t = 20, value_parser = positive, value_name = "INTEGER", allow_negative_numbers = true)]
        count: usize,
        #[arg(long, value_parser = ["cli", "mcp", "update"])]
        surface: Option<String>,
        #[arg(long, value_name = "EVENT")]
        event: Option<String>,
    },
    /// Show an intent's CLI and MCP trail and its current Keeper record.
    Intent {
        key: String,
        #[arg(long)]
        offline: bool,
        #[arg(long)]
        addr: Option<String>,
        #[arg(long)]
        before: Option<String>,
    },
    /// Show one process run or one MCP call by ID.
    Run {
        id: String,
        #[arg(long)]
        before: Option<String>,
    },
    /// Summarize Keeper latency and error codes in the retained logs.
    Stats {
        #[arg(long, value_name = "DURATION")]
        since: Option<String>,
        #[arg(long)]
        before: Option<String>,
    },
}

fn positive(raw: &str) -> Result<usize, String> {
    let count: i128 = raw.trim().parse().map_err(|_| {
        format!(
            "Invalid value for '-n': {} is not a valid integer.",
            sai_core::settings::repr(raw)
        )
    })?;
    if count < 1 {
        return Err(format!(
            "Invalid value for '-n': {count} is not in the range x>=1."
        ));
    }
    Ok(usize::try_from(count).unwrap_or(usize::MAX))
}

impl LogsCommand {
    pub fn run(
        self,
        env: &Environment,
        flags: &Flags,
        account: Option<&str>,
        json_output: bool,
    ) -> Result<(), SaiError> {
        let root: PathBuf = env.home.join("logs");
        match self {
            Self::Path => {
                if json_output {
                    return local::emit(&query::path(env), true);
                }
                println!("{}", root.display());
            }
            Self::Tail {
                count,
                surface,
                event,
            } => {
                for row in query::tail(&root, count, surface.as_deref(), event.as_deref()) {
                    if json_output {
                        println!("{}", local::ascii_json(&Value::Object(row).to_string()));
                    } else {
                        println!("{}", query::render(&row));
                    }
                }
            }
            Self::Run { id, before } => {
                let report = query::run(&env.home, &id, before.as_deref())?;
                if json_output {
                    return local::emit(&report, true);
                }
                print_trail(&report);
            }
            Self::Stats { since, before } => {
                let report = query::stats(&env.home, since.as_deref(), before.as_deref())?;
                if json_output {
                    return local::emit(&report, true);
                }
                print_stats(&report);
            }
            Self::Intent {
                key,
                offline,
                addr,
                before,
            } => {
                let settings;
                let endpoint = if offline {
                    if let Some(endpoint) = &flags.endpoint {
                        Key::KeeperEndpoint.parse_value(endpoint)?;
                    }
                    flags.endpoint.as_deref()
                } else {
                    settings = load_settings(flags, env)?;
                    Some(settings.endpoint())
                };
                let account = addr.as_deref().or(account);
                let runtime = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .map_err(|_| SaiError::internal())?;
                let report = runtime.block_on(crate::repl::interruptible(query::intent(
                    &env.home,
                    &key,
                    endpoint,
                    account,
                    offline,
                    before.as_deref(),
                )))?;
                if json_output {
                    return local::emit(&report, true);
                }
                print_trail(&report);
                if let Some(hash) = report["txHash"].as_str() {
                    println!("Request transaction {hash}");
                }
                if report["keeper"]
                    .as_object()
                    .is_some_and(|row| !row.is_empty())
                {
                    println!(
                        "Keeper {}",
                        local::ascii_json(&report["keeper"].to_string())
                    );
                } else if let Some(error) = report["keeperError"].as_object() {
                    println!(
                        "Keeper lookup failed ({}). {}",
                        error["code"].as_str().unwrap_or_default(),
                        error["message"].as_str().unwrap_or_default()
                    );
                } else if !offline {
                    println!("Keeper has no record for this intent.");
                }
            }
        }
        Ok(())
    }
}

fn rows<'a>(report: &'a Value, field: &str) -> impl Iterator<Item = &'a query::Row> {
    report[field]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_object)
}

fn print_limit(report: &Value) {
    if report["truncated"] == true {
        println!(
            "Report truncated. {}",
            report["more"].as_str().unwrap_or_default()
        );
        if let Some(before) = report["nextBefore"].as_str() {
            println!("Continue with --before {before}");
        }
    }
}

fn print_trail(report: &Value) {
    let mut all: Vec<_> = rows(report, "events").chain(rows(report, "gaps")).collect();
    all.sort_by_cached_key(|row| query::order(row));
    if all.is_empty() {
        println!("No matching log events.");
    }
    for row in all {
        let prefix = if row.get("event").and_then(Value::as_str) == Some("log.dropped") {
            "Gap  "
        } else {
            ""
        };
        println!("{prefix}{}", query::render(row));
    }
    if let Some(warning) = report["scopeWarning"].as_str() {
        println!("{warning}");
    }
    for row in rows(report, "unattributedGaps") {
        println!("Unattributed gap  {}", query::render(row));
    }
    let count = report["unattributedGapCount"].as_u64().unwrap_or_default();
    if count > rows(report, "unattributedGaps").count() as u64 {
        println!("Unattributed drop notices in scan  {count}");
    }
    print_limit(report);
}

fn print_stats(report: &Value) {
    if let Some(keeper) = report["keeper"].as_object().filter(|rows| !rows.is_empty()) {
        println!("Keeper operation  Requests  p50 (ms)  p95 (ms)");
        for (name, values) in keeper {
            println!(
                "{name:16}  {:>8}  {:>8}  {:>8}",
                values["count"].to_string(),
                values["p50Ms"].to_string(),
                values["p95Ms"].to_string()
            );
        }
    } else {
        println!("No timed Keeper requests.");
    }
    if let Some(errors) = report["errors"].as_object().filter(|rows| !rows.is_empty()) {
        println!("Errors by code");
        for (code, count) in errors {
            println!("{code}  {count}");
        }
    } else {
        println!("No coded errors.");
    }
    for row in rows(report, "gaps") {
        println!("Gap  {}", query::render(row));
    }
    print_limit(report);
}

pub fn help(command: &str) -> Option<&'static str> {
    Some(match command {
        "logs" => {
            r#"Usage: sai logs [OPTIONS] COMMAND [ARGS]...

  Read the JSON-lines logs that sai and its MCP server keep.

  sai records each command, MCP tool call, Keeper request, and intent status
  change under $SAI_HOME/logs, one file per surface (cli, mcp, update) and UTC
  day. The session token is never written. Turn logging off with `sai config
  set logs.enabled false` or SAI_LOG=0; files older than logs.keep_days are
  deleted.

Options:
  --help  Show this message and exit.

Commands:
  intent  Show an intent's CLI and MCP trail and its current Keeper record.
  path    Print the logs directory.
  run     Show one process run or one MCP call by ID.
  stats   Summarize Keeper latency and error codes in the retained logs.
  tail    Show the most recent events, oldest first."#
        }
        "logs path" => {
            r#"Usage: sai logs path [OPTIONS]

  Print the logs directory. With --json, also whether logging is on, how many
  days are kept, and the files there.

Options:
  --help  Show this message and exit."#
        }
        "logs tail" => {
            r#"Usage: sai logs tail [OPTIONS]

  Show the most recent events, oldest first.

  Plain output is one line per event: time (UTC), surface, level, event, then
  its fields. With --json, each line is a JSON object.

  Examples:
    sai logs tail
    sai logs tail -n 50 --surface mcp
    sai logs tail --event intent
    sai --json logs tail --event keeper.request

Options:
  -n INTEGER RANGE            How many events to show.  [default: 20; x>=1]
  --surface [cli|mcp|update]  Only events from the CLI, the MCP server, or the
                              updater.
  --event EVENT               Only this event, such as keeper.request, or a
                              family by its prefix: `intent` matches
                              intent.reserved and intent.status.
  --help                      Show this message and exit."#
        }
        "logs intent" => {
            r#"Usage: sai logs intent [OPTIONS] KEY

  Show an intent's CLI and MCP trail and its current Keeper record.

Options:
  --offline      Read local logs only.
  --addr TEXT    EVM address to use for the Keeper lookup.
  --before TEXT  Resume from a cursor or read before a UTC timestamp.
  --help         Show this message and exit."#
        }
        "logs run" => {
            r#"Usage: sai logs run [OPTIONS] ID

  Show one process run or one MCP call by ID.

Options:
  --before TEXT  Resume from a cursor or read before a UTC timestamp.
  --help         Show this message and exit."#
        }
        "logs stats" => {
            r#"Usage: sai logs stats [OPTIONS]

  Summarize Keeper latency and error codes in the retained logs.

Options:
  --since DURATION  Look back by a duration such as 24h.
  --before TEXT     Resume from a cursor or read before a UTC timestamp.
  --help            Show this message and exit."#
        }
        _ => return None,
    })
}
