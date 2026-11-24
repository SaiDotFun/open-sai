use std::{
    sync::OnceLock,
    time::{Duration, Instant},
};

use clap::{Args, Subcommand, ValueEnum};
use sai_core::{
    errors::{Code, ExitCode, SaiError},
    log::EventContext,
    settings::{Environment, Flags},
    storage::{Session, Storage, StorageEvent},
    trade::{self, OpenTrade, SubmitOptions, TradePreview, Trading, close_preview},
    wait,
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
        /// Wait up to 30 seconds for the intent outcome.
        #[arg(long)]
        wait: bool,
    },
    /// Cancel a pending order.
    Cancel {
        trade_id: String,
        #[arg(long)]
        dry_run: bool,
        /// Wait up to 30 seconds for the intent outcome.
        #[arg(long)]
        wait: bool,
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
    /// Wait up to 30 seconds for the intent outcome.
    #[arg(long)]
    wait: bool,
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
) -> Result<ExitCode, SaiError> {
    let settings = load_settings(flags, env)?;
    let log_storage = |event: StorageEvent<'_>| local::storage_event(events, event);
    let storage = Storage::new(&env.home).with_event_sink(&log_storage);
    let trading = Trading::new(settings.endpoint(), &storage, events);
    let (mut preview, dry_run, wait_for_trade, allow_concurrent_open, supplied_price) =
        match command {
            TradeCommand::Open(open) => (
                open.preview()?,
                open.dry_run,
                open.wait,
                open.allow_concurrent_open,
                open.price.is_some(),
            ),
            TradeCommand::Close {
                trade_id,
                cancel_order,
                dry_run,
                wait,
            } => (
                close_preview(&trade_id, cancel_order)?,
                dry_run,
                wait,
                false,
                false,
            ),
            TradeCommand::Cancel {
                trade_id,
                dry_run,
                wait,
            } => (close_preview(&trade_id, true)?, dry_run, wait, false, false),
        };
    let session = match trading.load_session(account) {
        Ok(session) => Some(session),
        Err(error) if dry_run && account.is_none() && session_error(error.code) => None,
        Err(error) => return Err(error),
    };
    if dry_run {
        local::emit(&preview.output(session.as_ref().map(Session::addr)), json)?;
        return Ok(ExitCode::Ok);
    }
    let session = session.ok_or_else(SaiError::internal)?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| SaiError::internal())?;
    let endpoint = settings.endpoint();
    let addr = session.addr();
    let reservation = OnceLock::<String>::new();
    let track = |key: &str| {
        let _ = reservation.set(key.to_owned());
    };
    let submit = async {
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
                    on_reserved: wait_for_trade.then_some(&track as &(dyn Fn(&str) + Sync)),
                    ..SubmitOptions::default()
                },
            )
            .await
    };
    if wait_for_trade {
        let recover = |interrupted, error: SaiError| {
            let Some(key) = reservation.get() else {
                return Err(error);
            };
            let confirmed = (error.code == Code::NotSent).then_some(wait::Outcome::NotSent);
            Ok((
                wait::recovery_result(&storage, endpoint, addr, key, interrupted, confirmed),
                false,
                true,
            ))
        };
        let (mut receipt, keep_quote, recovered) = runtime.block_on(async {
            let waited = async {
                let mut keep_quote = false;
                let key = match submit.await {
                    Ok(accepted) => {
                        keep_quote = true;
                        accepted.intent_key
                    }
                    Err(error) if error.code == Code::AdmissionRejected => {
                        let Some(key) = reservation.get() else {
                            return Err(error);
                        };
                        return Ok((
                            wait::rejected_result(key, addr, &error.message),
                            false,
                            false,
                        ));
                    }
                    Err(error) if error.code == Code::OutcomeUnknown => {
                        reservation.get().ok_or(error)?.clone()
                    }
                    Err(error) => return Err(error),
                };
                let receipt = wait::for_intent(
                    endpoint,
                    addr,
                    &key,
                    Duration::from_secs_f64(wait::DEFAULT_TIMEOUT),
                    Instant::now(),
                    &storage,
                    events,
                )
                .await?;
                Ok((receipt, keep_quote, false))
            };
            tokio::select! {
                biased;
                signal = tokio::signal::ctrl_c() => recover(
                    signal.is_ok(), SaiError::new(Code::InvalidRequest, "Aborted"),
                ),
                result = waited => result.or_else(|error| recover(false, error)),
            }
        })?;
        if !recovered {
            receipt.body["status"] = json!(receipt.outcome);
        }
        if keep_quote && let Some(price) = preview.reference_price {
            receipt.body["referencePrice"] = json!(price);
        }
        return local::emit_wait(receipt, endpoint, env, json, recovered);
    }
    let result = runtime.block_on(submit)?;
    let mut output = json!({
        "addr": session.addr(), "intentKey": result.intent_key, "status": result.status,
        "message": result.message,
    });
    if let Some(price) = preview.reference_price {
        output["referencePrice"] = json!(price);
    }
    local::emit(&output, json)?;
    Ok(ExitCode::Ok)
}

fn session_error(code: Code) -> bool {
    matches!(
        code,
        Code::SignedOut | Code::SessionExpired | Code::SessionUnavailable
    )
}
