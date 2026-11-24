use clap::{Args, Subcommand, ValueEnum};
use sai_core::{
    errors::{Code, SaiError},
    log::EventContext,
    settings::{Environment, Flags, Key, Settings, repr},
    storage::{Session, Storage, StorageEvent},
    trade::{
        self, MarginDirection, OpenTrade, PositionAction, PositionMode, SubmitOptions,
        TradePreview, Trading, close_preview,
    },
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
    /// Add or remove margin through a target-leverage intent.
    Margin {
        #[command(flatten)]
        position: Position,
        /// Direction of the margin change, add or remove.
        #[arg(long)]
        direction: String,
        /// USDC amount for a leverage quote, not an execution cap.
        #[arg(long, allow_negative_numbers = true)]
        amount: String,
    },
    /// Reduce an open USDC position by a whole percentage.
    PartialClose {
        #[command(flatten)]
        position: Position,
        /// Whole percentage to close, from 1 to 99.
        #[arg(long, allow_negative_numbers = true)]
        percent: String,
    },
}

#[derive(Args)]
pub struct Position {
    #[arg(allow_negative_numbers = true)]
    trade_id: String,
    /// Nibiru REST endpoint for exact contract state. Defaults to lcd.endpoint.
    #[arg(long)]
    lcd_endpoint: Option<String>,
    #[arg(long)]
    dry_run: bool,
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
    let log_storage = |event: StorageEvent<'_>| local::storage_event(events, event);
    let storage = Storage::new(&env.home).with_event_sink(&log_storage);
    let trading = Trading::new(settings.endpoint(), &storage, events);
    let (mut preview, dry_run, allow_concurrent_open, supplied_price) = match command {
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
        TradeCommand::Margin {
            position,
            direction,
            amount,
        } => {
            let direction = match direction.as_str() {
                "add" => MarginDirection::Add,
                "remove" => MarginDirection::Remove,
                _ => {
                    return Err(SaiError::usage(format!(
                        "Invalid value for '--direction': {} is not one of 'add', 'remove'.",
                        repr(&direction),
                    )));
                }
            };
            return position.run(
                &trading,
                &settings,
                account,
                PositionAction::Margin {
                    direction,
                    amount: &amount,
                },
                json,
            );
        }
        TradeCommand::PartialClose { position, percent } => {
            return position.run(
                &trading,
                &settings,
                account,
                PositionAction::PartialClose {
                    percent: trade::parse_partial_close_percent(&percent)?,
                },
                json,
            );
        }
    };
    let session = match trading.load_session(account) {
        Ok(session) => Some(session),
        Err(error) if dry_run && account.is_none() && session_error(error.code) => None,
        Err(error) => return Err(error),
    };
    if dry_run {
        return local::emit(&preview.output(session.as_ref().map(Session::addr)), json);
    }
    let session = session.ok_or_else(SaiError::internal)?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| SaiError::internal())?;
    let result = runtime.block_on(async {
        if preview.requires_market_price_refresh() {
            if supplied_price {
                eprintln!("{}", trade::MARKET_PRICE_NOTICE);
            }
            trading.refresh_market_open(&mut preview).await?;
        }
        trading
            .submit(
                &session,
                &preview,
                SubmitOptions {
                    allow_concurrent_open,
                    ..SubmitOptions::default()
                },
            )
            .await
    })?;
    let mut output = json!({
        "addr": session.addr(), "intentKey": result.intent_key, "status": result.status,
        "message": result.message,
    });
    if let Some(price) = preview.reference_price {
        output["referencePrice"] = json!(price);
    }
    local::emit(&output, json)
}

pub fn missing_position_option(argument: &str) -> Option<SaiError> {
    let option = argument.split_whitespace().next()?;
    let message = match option {
        "--direction" => "Missing option '--direction'. Choose from:\n\tadd,\n\tremove".into(),
        "--amount" | "--percent" => format!("Missing option {}.", repr(option)),
        _ => return None,
    };
    Some(SaiError::usage(message))
}

impl Position {
    fn run(
        self,
        trading: &Trading<'_>,
        settings: &Settings,
        account: Option<&str>,
        action: PositionAction<'_>,
        json: bool,
    ) -> Result<(), SaiError> {
        if let Some(endpoint) = &self.lcd_endpoint {
            Key::LcdEndpoint.parse_value(endpoint)?;
        }
        let lcd_endpoint = self
            .lcd_endpoint
            .as_deref()
            .unwrap_or_else(|| settings.lcd_endpoint());
        let mode = if self.dry_run {
            PositionMode::DryRun
        } else {
            PositionMode::Submit(None)
        };
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| SaiError::internal())?;
        let result = runtime.block_on(trading.position_trade(
            account,
            lcd_endpoint,
            &self.trade_id,
            action,
            mode,
        ))?;
        local::emit(&result, json)
    }
}

fn session_error(code: Code) -> bool {
    matches!(
        code,
        Code::SignedOut | Code::SessionExpired | Code::SessionUnavailable
    )
}
