pub(crate) mod output;
mod overview;

use std::{
    fmt::Write as _,
    io::{self, Write as _},
    time::Duration,
};

use clap::{CommandFactory, Subcommand};
use sai_core::{
    errors::{Code, SaiError},
    keeper::{
        self, Keeper,
        stream::{self, Connection, Failure, Subscription},
    },
    log::{EventContext, Field, Level},
    settings::{Environment, Flags},
};
use serde_json::{Map, Number, Value, json};

use crate::{Cli, load_settings, usage};

pub(crate) fn help(command: &str) -> Option<String> {
    let mut names = command.split(' ');
    let group = names.next()?;
    if !matches!(group, "market" | "account" | "watch") {
        return None;
    }
    let root = Cli::command();
    let mut entry = root.find_subcommand(group)?;
    if let Some(leaf) = names.next() {
        entry = entry.find_subcommand(leaf)?;
    }
    let description = entry.get_about()?;
    // Clap strips the final period from each doc comment's short help.
    let mut text = format!("{}\n\n  {description}.\n\nOptions:\n", usage(command));
    let help_column = if command == "account overview" {
        text.push_str("  --as ADDR  Read this EVM account.\n  --json     Print compact JSON.\n");
        9
    } else if let Some(arg) = entry
        .get_arguments()
        .find(|arg| arg.get_long() == Some("ws-endpoint"))
    {
        text.push_str("  --ws-endpoint TEXT");
        if let Some(help) = arg.get_help() {
            write!(text, "  {help}.").ok()?;
        }
        text.push('\n');
        18
    } else {
        6
    };
    write!(
        text,
        "  {:<help_column$}  Show this message and exit.",
        "--help"
    )
    .ok()?;
    let mut children: Vec<_> = entry.get_subcommands().collect();
    children.sort_unstable_by_key(|child| child.get_name());
    if let Some(width) = children.iter().map(|child| child.get_name().len()).max() {
        text.push_str("\n\nCommands:");
        for child in children {
            write!(
                text,
                "\n  {:<width$}  {}.",
                child.get_name(),
                if child.get_name() == "overview" {
                    "Show Sai funds, positions, orders, local blockers, and..".into()
                } else {
                    child.get_about()?.to_string()
                },
            )
            .ok()?;
        }
    }
    Some(text)
}

#[derive(Subcommand)]
pub(crate) enum MarketCommand {
    /// List Keeper borrowing markets.
    List,
    /// List current indexed prices.
    Prices,
}

impl MarketCommand {
    pub(crate) fn run(
        self,
        flags: &Flags,
        env: &Environment,
        json: bool,
        events: EventContext<'_>,
    ) -> Result<(), SaiError> {
        let query = match self {
            Self::List => keeper::MARKETS,
            Self::Prices => keeper::PRICES,
        };
        read(query, Map::new(), flags, env, json, events)
    }
}

#[derive(Subcommand)]
pub(crate) enum AccountCommand {
    /// Show Sai funds, positions, orders, local blockers, and session expiry.
    Overview {
        /// Read this EVM account.
        #[arg(long = "as", value_name = "ADDR")]
        addr: Option<String>,
        /// Print compact JSON.
        #[arg(long)]
        json: bool,
    },
    /// List positions and pending orders for ADDR.
    Positions { addr: String },
    /// Read trade history for ADDR.
    History { addr: String },
    /// Read one account balance snapshot from Keeper's subscription.
    Balances {
        addr: String,
        /// Override the keeper.ws_endpoint setting.
        #[arg(long, overrides_with = "ws_endpoint")]
        ws_endpoint: Option<String>,
    },
}

impl AccountCommand {
    pub(crate) fn run(
        self,
        account: Option<&str>,
        flags: &Flags,
        env: &Environment,
        json: bool,
        events: EventContext<'_>,
    ) -> Result<(), SaiError> {
        let (query, addr) = match self {
            Self::Overview {
                addr,
                json: overview_json,
            } => {
                let addr = addr.as_deref().filter(|addr| !addr.is_empty()).or(account);
                let settings = load_settings(flags, env)?;
                let runtime = runtime()?;
                let value = runtime.block_on(sai_core::overview::read(
                    &settings,
                    &env.home,
                    addr,
                    events,
                    interrupted(),
                ));
                // A cancelled system DNS job can outlive the overview's deadline.
                // Let the CLI report its result without waiting for that blocking job.
                runtime.shutdown_background();
                let value = value?;
                return if json || overview_json {
                    output::read(&value, true)
                } else {
                    overview::show(&value)
                };
            }
            Self::Positions { addr } => (keeper::POSITIONS, addr),
            Self::History { addr } => (keeper::HISTORY, addr),
            Self::Balances { addr, ws_endpoint } => {
                let endpoint = stream_endpoint(ws_endpoint, flags, env)?;
                let payload = runtime()?.block_on(stream::snapshot(
                    &endpoint,
                    &Subscription::Balances(addr),
                    || eprintln!("{}", stream::CONNECTED),
                    interrupted(),
                ))?;
                return output::read(&payload, json);
            }
        };
        read(
            query,
            Map::from_iter([("trader".into(), json!(addr))]),
            flags,
            env,
            json,
            events,
        )
    }
}

fn read(
    query: &str,
    variables: Map<String, Value>,
    flags: &Flags,
    env: &Environment,
    json: bool,
    events: EventContext<'_>,
) -> Result<(), SaiError> {
    let settings = load_settings(flags, env)?;
    let keeper = Keeper::new(settings.endpoint(), events);
    let data =
        runtime()?.block_on(keeper.read_until(query, variables, keeper::TIMEOUT, interrupted()))?;
    if query == keeper::HISTORY {
        output::read(&keeper::public_account_history(&env.home, &data), json)
    } else {
        output::read(&data, json)
    }
}

#[derive(Subcommand)]
pub(crate) enum WatchCommand {
    /// Follow position snapshots for ADDR until interrupted.
    Positions {
        addr: String,
        #[arg(long, overrides_with = "ws_endpoint")]
        ws_endpoint: Option<String>,
    },
    /// Follow balances, preserving Sai deposit and credit labels.
    Balances {
        addr: String,
        #[arg(long, overrides_with = "ws_endpoint")]
        ws_endpoint: Option<String>,
    },
    /// Follow the current price of TOKEN_ID.
    Price {
        token_id: String,
        #[arg(long, overrides_with = "ws_endpoint")]
        ws_endpoint: Option<String>,
    },
}

impl WatchCommand {
    pub(crate) fn run(
        self,
        flags: &Flags,
        env: &Environment,
        events: EventContext<'_>,
    ) -> Result<(), SaiError> {
        let (subscribe, override_endpoint) = match self {
            Self::Positions { addr, ws_endpoint } => (Subscription::Positions(addr), ws_endpoint),
            Self::Balances { addr, ws_endpoint } => (Subscription::Balances(addr), ws_endpoint),
            Self::Price {
                token_id,
                ws_endpoint,
            } => (Subscription::Price(parse_token_id(&token_id)?), ws_endpoint),
        };
        let endpoint = stream_endpoint(override_endpoint, flags, env)?;
        stream::check_endpoint(&endpoint)?;
        runtime()?.block_on(watch(&endpoint, &subscribe, events))
    }
}

fn parse_token_id(input: &str) -> Result<Number, SaiError> {
    let invalid = || SaiError::usage("Invalid value for 'TOKEN_ID': expected an integer.");
    let input = input.trim();
    let digits = input.strip_prefix(['+', '-']).unwrap_or(input);
    if digits.is_empty()
        || !digits
            .split('_')
            .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
    {
        return Err(invalid());
    }
    let digits = digits.replace('_', "");
    let digits = digits.trim_start_matches('0');
    let normalized = if digits.is_empty() {
        "0".to_owned()
    } else if input.starts_with('-') {
        format!("-{digits}")
    } else {
        digits.to_owned()
    };
    normalized.parse().map_err(|_| invalid())
}

fn stream_endpoint(
    override_endpoint: Option<String>,
    flags: &Flags,
    env: &Environment,
) -> Result<String, SaiError> {
    if let Some(endpoint) = override_endpoint.filter(|endpoint| !endpoint.is_empty()) {
        return Ok(endpoint);
    }
    Ok(load_settings(flags, env)?.ws_endpoint().to_owned())
}

fn runtime() -> Result<tokio::runtime::Runtime, SaiError> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| SaiError::internal())
}

async fn interrupted() -> SaiError {
    match tokio::signal::ctrl_c().await {
        Ok(()) => SaiError::new(Code::InvalidRequest, "Aborted"),
        Err(_) => SaiError::internal(),
    }
}

async fn watch(
    endpoint: &str,
    subscribe: &Subscription,
    events: EventContext<'_>,
) -> Result<(), SaiError> {
    let signal = interrupted();
    tokio::pin!(signal);
    loop {
        let attempt = Connection::open(endpoint, subscribe, signal.as_mut()).await;
        let failure = match attempt {
            Err(failure) => failure,
            Ok(mut connection) => {
                eprintln!("{}", stream::CONNECTED);
                let result = tokio::select! {
                    biased;
                    error = &mut signal => {
                        connection.close().await;
                        return Err(error);
                    }
                    result = print_snapshots(&mut connection) => result,
                };
                tokio::select! {
                    biased;
                    () = connection.close() => {}
                    error = &mut signal => return Err(error),
                }
                result?
            }
        };
        let reason = match failure {
            Failure::Disconnected(reason) => reason,
            Failure::Protocol(error) => return Err(error),
        };
        events.event(
            "watch.disconnected",
            Level::Warn,
            [
                ("code", json!(Code::StreamError).into()),
                ("errorType", Field::Text(reason.into())),
                ("endpoint", Field::Endpoint(endpoint.into())),
            ],
        );
        eprintln!("Disconnected ({reason}); data may be stale; reconnecting in 2s");
        tokio::select! {
            biased;
            error = &mut signal => return Err(error),
            _ = tokio::time::sleep(Duration::from_secs(2)) => {}
        }
    }
}

async fn print_snapshots(connection: &mut Connection) -> Result<Failure, SaiError> {
    loop {
        let payload = match connection.next(None).await {
            Ok(payload) => payload,
            Err(failure) => return Ok(failure),
        };
        writeln!(io::stdout().lock(), "{}", output::line(&payload)?)
            .map_err(|_| SaiError::internal())?;
    }
}
