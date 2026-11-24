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
    trade::{self, OpenTrade, PreviewMode, SubmitOptions, TradePreview, Trading, close_preview},
    wait,
};
use serde_json::{Map, Value, json};

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
    /// Set or clear take-profit and stop-loss prices.
    Tpsl {
        #[arg(allow_negative_numbers = true)]
        trade_id: String,
        #[arg(long, allow_negative_numbers = true)]
        take_profit: Option<String>,
        #[arg(long, allow_negative_numbers = true)]
        stop_loss: Option<String>,
        #[arg(long)]
        clear_take_profit: bool,
        #[arg(long)]
        clear_stop_loss: bool,
        #[arg(long)]
        dry_run: bool,
    },
    /// Set target leverage without changing TP or SL.
    Leverage {
        #[arg(allow_negative_numbers = true)]
        trade_id: String,
        /// Target leverage as a decimal.
        #[arg(long, allow_negative_numbers = true)]
        leverage: String,
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
    let (preview, dry_run, wait_for_trade, allow_concurrent_open, supplied_price) = match command {
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
        TradeCommand::Tpsl {
            trade_id,
            take_profit,
            stop_loss,
            clear_take_profit,
            clear_stop_loss,
            dry_run,
        } => (
            trade::tpsl_preview(
                &trade_id,
                take_profit.as_deref(),
                stop_loss.as_deref(),
                clear_take_profit,
                clear_stop_loss,
            )?,
            dry_run,
            false,
            false,
            false,
        ),
        TradeCommand::Leverage {
            trade_id,
            leverage,
            dry_run,
        } => (
            trade::leverage_preview(&trade_id, &leverage)?,
            dry_run,
            false,
            false,
            false,
        ),
    };
    let session = match trading.load_session(account) {
        Ok(session) => Some(session),
        Err(error) if dry_run && !cancelling && account.is_none() && session_error(error.code) => {
            None
        }
        Err(error) => return Err(error),
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| SaiError::internal())?;
    if dry_run {
        if preview.is_standalone_position_update() {
            let output = runtime.block_on(trading.position_update_preview(
                preview,
                session.as_ref(),
                false,
            ))?;
            local::emit(&output, json)?;
            return Ok(ExitCode::Ok);
        }
        let preview = runtime.block_on(trading.prepare_preview(
            preview,
            session.as_ref(),
            PreviewMode::Cli,
        ))?;
        local::emit(&preview.output(session.as_ref().map(Session::addr)), json)?;
        return Ok(ExitCode::Ok);
    }
    let session = session.ok_or_else(SaiError::internal)?;
    let endpoint = settings.endpoint();
    let addr = session.addr();
    let reservation = OnceLock::<String>::new();
    let track = |key: &str| {
        let _ = reservation.set(key.to_owned());
    };
    if preview.requires_market_price_refresh() && supplied_price {
        eprintln!("{}", trade::MARKET_PRICE_NOTICE);
    }
    let options = SubmitOptions {
        allow_concurrent_open,
        on_reserved: wait_for_trade.then_some(&track as &(dyn Fn(&str) + Sync)),
        ..SubmitOptions::default()
    };
    if wait_for_trade {
        let recover = |interrupted, error: SaiError| {
            let Some(key) = reservation.get() else {
                return Err(error);
            };
            let confirmed = (error.code == Code::NotSent).then_some(wait::Outcome::NotSent);
            Ok((
                wait::recovery_result(&storage, endpoint, addr, key, interrupted, confirmed),
                None,
                true,
            ))
        };
        return runtime.block_on(async {
            let mut signal = local::wait_signal()?;
            let waited = async {
                let submission = trading
                    .submit_with_reference_price(
                        &session,
                        &preview,
                        options,
                        std::future::pending(),
                    )
                    .await;
                let (key, metadata) = match submission {
                    Ok((accepted, price)) => {
                        let mut metadata = Map::from_iter([
                            ("intentKey".into(), json!(accepted.intent_key)),
                            ("status".into(), json!(accepted.status)),
                        ]);
                        if let Some(price) = price {
                            metadata.insert("referencePrice".into(), json!(price));
                        }
                        (accepted.intent_key, Some(metadata))
                    }
                    Err(error) if error.code == Code::AdmissionRejected => {
                        let Some(key) = reservation.get() else {
                            return Err(error);
                        };
                        return Ok((
                            wait::rejected_result(key, addr, &error.message),
                            None,
                            false,
                        ));
                    }
                    Err(error) if error.code == Code::OutcomeUnknown => {
                        (reservation.get().ok_or(error)?.clone(), None)
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
                Ok((receipt, metadata, false))
            };
            let (mut receipt, metadata, recovered) = tokio::select! {
                biased;
                _ = signal.recv() => recover(true, local::wait_aborted()),
                result = waited => result.or_else(|error| recover(false, error)),
            }?;
            if let Some(mut metadata) = metadata {
                let Value::Object(body) = receipt.body else {
                    return Err(SaiError::internal());
                };
                metadata.extend(body);
                receipt.body = Value::Object(metadata);
            }
            if !recovered {
                receipt.body["status"] = json!(receipt.outcome);
            }
            local::emit_wait(receipt, endpoint, env, json, recovered, &mut signal).await
        });
    }
    let (result, reference_price) = runtime.block_on(trading.submit_with_reference_price(
        &session,
        &preview,
        options,
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
    local::emit(&output, json)?;
    Ok(ExitCode::Ok)
}

fn session_error(code: Code) -> bool {
    matches!(
        code,
        Code::SignedOut | Code::SessionExpired | Code::SessionUnavailable
    )
}
