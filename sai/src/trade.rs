use clap::{Args, Subcommand, ValueEnum};
use sai_core::{
    errors::{Code, SaiError},
    log::EventContext,
    settings::{Environment, Flags},
    storage::{Session, Storage, StorageEvent},
    trade::{self, OpenTrade, SubmitOptions, TradePreview, Trading, close_preview},
};
use serde_json::json;

use crate::{load_settings, local};

#[derive(Subcommand)]
pub enum TradeCommand {
    /// Open a market, limit, or stop trade.
    Open(Open),
    /// Close a position or cancel a pending order.
    Close {
        trade_id: String,
        /// Cancel a pending order instead of closing a position.
        #[arg(long)]
        cancel_order: bool,
        #[arg(long)]
        dry_run: bool,
    },
    /// Cancel a pending order.
    Cancel {
        trade_id: String,
        #[arg(long)]
        dry_run: bool,
    },
}

#[derive(ValueEnum, Clone, Copy)]
enum Side {
    Long,
    Short,
}

#[derive(ValueEnum, Clone, Copy)]
enum OrderType {
    Market,
    Limit,
    Stop,
}

#[derive(Args)]
pub struct Open {
    #[arg(long, allow_negative_numbers = true)]
    market: String,
    #[arg(long)]
    side: Side,
    #[arg(long = "type")]
    order_type: OrderType,
    /// USDC amount in USD.
    #[arg(long, allow_negative_numbers = true)]
    amount: String,
    #[arg(long, allow_negative_numbers = true)]
    leverage: String,
    /// Required trigger for limit and stop. Live market orders use a refreshed price.
    #[arg(long, allow_negative_numbers = true)]
    price: Option<String>,
    /// Maximum slippage percentage.
    #[arg(long, allow_negative_numbers = true)]
    slippage: String,
    #[arg(long, allow_negative_numbers = true)]
    take_profit: Option<String>,
    #[arg(long, allow_negative_numbers = true)]
    stop_loss: Option<String>,
    /// Allow another open while an earlier open is unknown or accepted.
    #[arg(long)]
    allow_concurrent_open: bool,
    #[arg(long)]
    dry_run: bool,
}

impl Open {
    fn preview(&self) -> Result<TradePreview, SaiError> {
        OpenTrade {
            market: &self.market,
            side: match self.side {
                Side::Long => trade::Side::Long,
                Side::Short => trade::Side::Short,
            },
            order_type: match self.order_type {
                OrderType::Market => trade::OrderType::Market,
                OrderType::Limit => trade::OrderType::Limit,
                OrderType::Stop => trade::OrderType::Stop,
            },
            amount: &self.amount,
            leverage: &self.leverage,
            price: self.price.as_deref(),
            slippage: &self.slippage,
            take_profit: self.take_profit.as_deref(),
            stop_loss: self.stop_loss.as_deref(),
        }
        .preview()
    }
}

pub fn run(
    command: TradeCommand,
    account: Option<&str>,
    flags: &Flags,
    env: &Environment,
    json: bool,
    events: EventContext<'_>,
) -> Result<(), SaiError> {
    let settings = load_settings(flags, env)?;
    let log_storage = |event: StorageEvent<'_>| {
        local::storage_event_with_endpoint(events, event, Some(settings.endpoint()))
    };
    let storage = Storage::new(&env.home).with_event_sink(&log_storage);
    let trading = Trading::new(settings.endpoint(), &storage, events)
        .with_lcd_endpoint(settings.lcd_endpoint());
    let cancelling = matches!(
        command,
        TradeCommand::Cancel { .. }
            | TradeCommand::Close {
                cancel_order: true,
                ..
            }
    );
    let (preview, dry_run, allow_concurrent_open, supplied_price) = match command {
        TradeCommand::Open(open) => (
            open.preview()?,
            open.dry_run,
            open.allow_concurrent_open,
            open.price.is_some(),
        ),
        TradeCommand::Close {
            trade_id,
            cancel_order,
            dry_run,
        } => (
            close_preview(&trade_id, cancel_order)?,
            dry_run,
            false,
            false,
        ),
        TradeCommand::Cancel { trade_id, dry_run } => {
            (close_preview(&trade_id, true)?, dry_run, false, false)
        }
    };
    let session = match trading.load_session(account) {
        Ok(session) => Some(session),
        Err(error) if dry_run && !cancelling && account.is_none() && session_error(error.code) => {
            None
        }
        Err(error) => return Err(error),
    };
    if dry_run && !cancelling {
        return local::emit(&preview.output(session.as_ref().map(Session::addr)), json);
    }
    let session = session.ok_or_else(SaiError::internal)?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| SaiError::internal())?;
    if dry_run {
        return local::emit(
            &runtime.block_on(trading.cancel_preview(&session, &preview))?,
            json,
        );
    }
    if preview.requires_market_price_refresh() && supplied_price {
        eprintln!("{}", trade::MARKET_PRICE_NOTICE);
    }
    let (result, reference_price) = runtime.block_on(trading.submit_with_reference_price(
        &session,
        &preview,
        SubmitOptions {
            allow_concurrent_open,
            ..SubmitOptions::default()
        },
        async {
            if tokio::signal::ctrl_c().await.is_err() {
                std::future::pending().await
            }
        },
    ))?;
    let mut output = json!({
        "addr": session.addr(), "intentKey": result.intent_key, "status": result.status,
        "message": result.message,
    });
    if let Some(price) = reference_price {
        output["referencePrice"] = json!(price);
    }
    local::emit(&output, json)
}

fn session_error(code: Code) -> bool {
    matches!(
        code,
        Code::SignedOut | Code::SessionExpired | Code::SessionUnavailable
    )
}
