use std::{
    future::{Future, pending},
    sync::OnceLock,
    time::{Duration, Instant},
};

use clap::{Args, Subcommand, ValueEnum};
use sai_core::{
    errors::{Code, ExitCode, SaiError},
    log::EventContext,
    settings::{Environment, Flags, Key, Settings, repr},
    storage::{Session, Storage, StorageEvent},
    trade::{
        self, MarginDirection, OpenTrade, PositionAction, PositionMode, PreviewMode, SubmitOptions,
        TradePreview, Trading, close_preview,
    },
    wait,
};
use serde_json::{Value, json};

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
        /// Wait up to 30 seconds for the intent outcome.
        #[arg(long)]
        wait: bool,
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
        /// Wait up to 30 seconds for the intent outcome.
        #[arg(long)]
        wait: bool,
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
    /// Wait up to 30 seconds for the intent outcome.
    #[arg(long)]
    wait: bool,
    /// Confirm the digest printed by --dry-run.
    #[arg(long)]
    preview_digest: Option<String>,
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

pub fn missing_open_option(option: &str) -> Option<SaiError> {
    match option {
        "--side" => Some(missing_enum_option::<Side>(option)),
        "--type" => Some(missing_enum_option::<OrderType>(option)),
        _ => None,
    }
}

fn missing_enum_option<T: ValueEnum>(option: &str) -> SaiError {
    let choices = T::value_variants()
        .iter()
        .filter_map(ValueEnum::to_possible_value)
        .map(|value| value.get_name().to_owned())
        .collect::<Vec<_>>()
        .join(",\n\t");
    SaiError::usage(format!(
        "Missing option {}. Choose from:\n\t{choices}",
        repr(option)
    ))
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

pub fn group_help(show_cancel: bool) -> String {
    let cancel = if show_cancel {
        "  cancel         Cancel a pending order.\n"
    } else {
        ""
    };
    format!(
        r#"{}

  Submit authenticated trading intents.

Options:
  --help  Show this message and exit.

Commands:
{cancel}  close          Close a position or cancel a pending order.
  leverage       Set target leverage without changing TP or SL.
  margin         Add or remove margin through a target-leverage intent.
  open           Open a market, limit, or stop trade.
  partial-close  Reduce an open USDC position by a whole percentage.
  tpsl           Set or clear take-profit and stop-loss prices."#,
        crate::usage("trade")
    )
}

// Render Click's help for the trade commands shared with Python.
pub fn help(command: &str) -> Option<String> {
    let body = match command {
        "trade close" => {
            r#"  Close a position or cancel a pending order.

Options:
  --cancel-order         Cancel a pending order instead of closing a position.
  --dry-run
  --preview-digest TEXT  Submit only if the trade matches this dry-run digest.
  --wait                 Wait up to 30 seconds for the intent outcome.
  --help                 Show this message and exit."#
        }
        "trade leverage" => {
            r#"  Set target leverage without changing TP or SL.

Options:
  --leverage DECIMAL     Target leverage as a decimal.  [required]
  --dry-run
  --preview-digest TEXT  Submit only if the trade matches this dry-run digest.
  --wait                 Wait up to 30 seconds for the intent outcome.
  --help                 Show this message and exit."#
        }
        "trade margin" => {
            r#"  Add or remove margin through a target-leverage intent.

  The amount is a quote, not a cap: collateral can change before execution.

Options:
  --direction [add|remove]  [required]
  --amount DECIMAL          USDC amount for a leverage quote, not an execution
                            cap.  [required]
  --lcd-endpoint TEXT       Nibiru REST endpoint for exact contract state.
                            Defaults to the lcd.endpoint setting.
  --dry-run
  --preview-digest TEXT     Submit only if the trade matches this dry-run
                            digest.
  --wait                    Wait up to 30 seconds for the intent outcome.
  --help                    Show this message and exit."#
        }
        "trade open" => {
            r#"  Open a market, limit, or stop trade.

Options:
  --market INTEGER            [x<=None; required]
  --side [long|short]         [required]
  --type [market|limit|stop]  [required]
  --amount DECIMAL            USDC amount in USD.  [required]
  --leverage DECIMAL          [required]
  --price DECIMAL             Trigger for limit and stop orders. For market
                              orders, pass the preview's reference price when
                              submitting with --preview-digest.
  --slippage TEXT             Maximum slippage percentage, greater than 0 and
                              at most 100.  [required]
  --take-profit DECIMAL
  --stop-loss DECIMAL
  --allow-concurrent-open     Allow another open while an earlier open is
                              unknown or accepted. Any locally pending open on
                              the same account and Keeper still blocks,
                              including a late acknowledgement.
  --dry-run
  --preview-digest TEXT       Submit only if the trade matches this dry-run
                              digest.
  --wait                      Wait up to 30 seconds for the intent outcome.
  --help                      Show this message and exit."#
        }
        "trade partial-close" => {
            r#"  Reduce an open USDC position by a whole percentage.

Options:
  --percent INTEGER      [1<=x<=99; required]
  --lcd-endpoint TEXT    Nibiru REST endpoint for exact contract state.
                         Defaults to the lcd.endpoint setting.
  --dry-run
  --preview-digest TEXT  Submit only if the trade matches this dry-run digest.
  --wait                 Wait up to 30 seconds for the intent outcome.
  --help                 Show this message and exit."#
        }
        "trade tpsl" => {
            r#"  Set or clear take-profit and stop-loss prices.

Options:
  --take-profit DECIMAL
  --stop-loss DECIMAL
  --clear-take-profit
  --clear-stop-loss
  --dry-run
  --preview-digest TEXT  Submit only if the trade matches this dry-run digest.
  --wait                 Wait up to 30 seconds for the intent outcome.
  --help                 Show this message and exit."#
        }
        _ => return None,
    };
    Some(format!("{}\n\n{body}", crate::usage(command)))
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
    let waiting = WaitTrade {
        endpoint: settings.endpoint(),
        storage: &storage,
        events,
        env,
        json,
        reservation: OnceLock::new(),
    };
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
                &waiting,
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
                &waiting,
            );
        }
        TradeCommand::Tpsl {
            trade_id,
            take_profit,
            stop_loss,
            clear_take_profit,
            clear_stop_loss,
            dry_run,
            wait,
        } => (
            trade::tpsl_preview(
                &trade_id,
                take_profit.as_deref(),
                stop_loss.as_deref(),
                clear_take_profit,
                clear_stop_loss,
            )?,
            dry_run,
            wait,
            false,
            false,
        ),
        TradeCommand::Leverage {
            trade_id,
            leverage,
            dry_run,
            wait,
        } => (
            trade::leverage_preview(&trade_id, &leverage)?,
            dry_run,
            wait,
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
    if preview.requires_market_price_refresh() && supplied_price {
        eprintln!("{}", trade::MARKET_PRICE_NOTICE);
    }
    let track = |addr: &str, key: &str| waiting.track(addr, key);
    let options = SubmitOptions {
        allow_concurrent_open,
        on_reserved: wait_for_trade.then_some(&track),
        ..SubmitOptions::default()
    };
    let submit = async {
        let (result, reference_price) = trading
            .submit_with_reference_price(
                &session,
                &preview,
                options,
                trade_interrupt(wait_for_trade),
            )
            .await?;
        let mut output = json!({
            "addr": session.addr(), "intentKey": result.intent_key, "status": result.status,
            "message": result.message,
        });
        if let Some(price) = reference_price {
            output["referencePrice"] = json!(price);
        }
        Ok(output)
    };
    if wait_for_trade {
        return runtime.block_on(waiting.finish(submit));
    }
    local::emit(&runtime.block_on(submit)?, json)?;
    Ok(ExitCode::Ok)
}

struct WaitTrade<'a> {
    endpoint: &'a str,
    storage: &'a Storage<'a>,
    events: EventContext<'a>,
    env: &'a Environment,
    json: bool,
    reservation: OnceLock<(String, String)>,
}

impl WaitTrade<'_> {
    fn track(&self, addr: &str, key: &str) {
        let _ = self.reservation.set((addr.into(), key.into()));
    }

    async fn finish(
        &self,
        submit: impl Future<Output = Result<Value, SaiError>>,
    ) -> Result<ExitCode, SaiError> {
        let recover = |interrupted, error: SaiError| {
            let Some((addr, key)) = self.reservation.get() else {
                return Err(error);
            };
            let confirmed = (error.code == Code::NotSent).then_some(wait::Outcome::NotSent);
            Ok((
                wait::recovery_result(
                    self.storage,
                    self.endpoint,
                    addr,
                    key,
                    interrupted,
                    confirmed,
                ),
                None,
                true,
            ))
        };
        let mut signal = local::wait_signal()?;
        let waited = async {
            let result = submit.await;
            let Some((addr, key)) = self.reservation.get() else {
                return result.and_then(|_| Err(SaiError::internal()));
            };
            let metadata = match result {
                Ok(Value::Object(mut metadata)) => {
                    metadata.remove("message");
                    Some(metadata)
                }
                Ok(_) => return Err(SaiError::internal()),
                Err(error) if error.code == Code::AdmissionRejected => {
                    return Ok((
                        wait::rejected_result(key, addr, &error.message),
                        None,
                        false,
                    ));
                }
                Err(error) if error.code == Code::OutcomeUnknown => None,
                Err(error) => return Err(error),
            };
            let receipt = wait::for_intent(
                self.endpoint,
                addr,
                key,
                Duration::from_secs_f64(wait::DEFAULT_TIMEOUT),
                Instant::now(),
                self.storage,
                self.events,
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
        local::emit_wait(
            receipt,
            self.endpoint,
            self.env,
            self.json,
            recovered,
            &mut signal,
        )
        .await
    }
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
        waiting: &WaitTrade<'_>,
    ) -> Result<ExitCode, SaiError> {
        if let Some(endpoint) = &self.lcd_endpoint {
            Key::LcdEndpoint.parse_value(endpoint)?;
        }
        let lcd_endpoint = self
            .lcd_endpoint
            .as_deref()
            .unwrap_or_else(|| settings.lcd_endpoint());
        let wait_for_trade = self.wait && !self.dry_run;
        let track = |addr: &str, key: &str| waiting.track(addr, key);
        let mode = if self.dry_run {
            PositionMode::Preview
        } else {
            PositionMode::Submit(SubmitOptions {
                confirmation: self
                    .preview_digest
                    .as_deref()
                    .map(trade::Confirmation::for_position_cli),
                on_reserved: wait_for_trade.then_some(&track),
                ..SubmitOptions::default()
            })
        };
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| SaiError::internal())?;
        let submit = async {
            trading
                .position_trade_with_interrupt(
                    account,
                    lcd_endpoint,
                    &self.trade_id,
                    action,
                    mode,
                    trade_interrupt(wait_for_trade),
                )
                .await
                .map_err(|mut error| {
                    if !waiting.json
                        && error.code == Code::PreviewMismatch
                        && error.details.is_some()
                    {
                        error
                            .message
                            .push_str(" Run with --dry-run to review a fresh preview.");
                    }
                    error
                })
        };
        if wait_for_trade {
            return runtime.block_on(waiting.finish(submit));
        }
        local::emit(&runtime.block_on(submit)?, waiting.json)?;
        Ok(ExitCode::Ok)
    }
}

async fn trade_interrupt(waiting_handles_interrupt: bool) {
    if waiting_handles_interrupt || tokio::signal::ctrl_c().await.is_err() {
        pending().await
    }
}

fn session_error(code: Code) -> bool {
    matches!(
        code,
        Code::SignedOut | Code::SessionExpired | Code::SessionUnavailable
    )
}
