use clap::Subcommand;
use sai_core::{
    errors::SaiError,
    keeper::{self, Keeper},
    log::EventContext,
    settings::{Environment, Flags},
};
use serde_json::{Map, Value, json};

use crate::{load_settings, local::emit};

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
    /// List positions and pending orders for ADDR.
    Positions { addr: String },
    /// Read trade history for ADDR.
    History { addr: String },
}

impl AccountCommand {
    pub(crate) fn run(
        self,
        flags: &Flags,
        env: &Environment,
        json: bool,
        events: EventContext<'_>,
    ) -> Result<(), SaiError> {
        let (query, addr) = match self {
            Self::Positions { addr } => (keeper::POSITIONS, addr),
            Self::History { addr } => (keeper::HISTORY, addr),
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
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| SaiError::internal())?;
    // Python exposes Keeper's data object directly, with its original units
    // and nullability. The intent-status projection applies to reconciled rows.
    let data = runtime.block_on(keeper.request(query, variables, None, keeper::TIMEOUT))?;
    emit(&data, json)
}
