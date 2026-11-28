mod install;
mod logs;
mod overview;
mod session;
mod stdio;
mod trade;
mod validation;

use std::{
    borrow::Cow,
    fs,
    future::pending,
    io::{self, IsTerminal},
    path::Path,
    sync::{Arc, LazyLock},
    time::{Duration, Instant},
};

use clap::Args;
use rmcp::{
    ErrorData, RoleServer, ServerHandler, ServiceExt,
    model::{
        CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, Implementation,
        ListToolsResult, PaginatedRequestParams, ProtocolVersion, ServerCapabilities, ServerConfig,
        Tool,
    },
    service::{QuitReason, RequestContext, ServerInitializeError},
};
use sai_core::{
    errors::{Code, SaiError},
    keeper::{self, Keeper, stream},
    log::{self, EventContext, Field, Level, LogWriter, Surface},
    reconcile,
    settings::{Environment, Flags, Settings},
    storage::{Storage, StorageEvent},
    wait,
};
use serde_json::{Map, Value, json};
use tokio::sync::Semaphore;
use uuid::Uuid;

use crate::reads::output;

pub(crate) use install::launcher_path;

// Python's 10-second wait stays the default. A caller can ask for the
// 60 seconds that CLI `--wait` uses, which covers Keeper's finality lag.
const MCP_WAIT_DEFAULT: Duration = Duration::from_secs(10);
const MCP_WAIT_LIMIT: Duration = Duration::from_secs(60);
const INSTRUCTIONS: &str = include_str!("mcp/instructions.txt");
// Wire metadata captured from Python main at 1c671b231b0fc968a44ec8dd520e67ea28e25b7f.
// The native server describes a longer intent_wait limit and replaces
// Python's `{result: string}` output schema with each tool's result object.
static TOOLS: LazyLock<Vec<Tool>> = LazyLock::new(|| {
    let mut tools: Vec<Map<String, Value>> =
        serde_json::from_str(include_str!("mcp/tools.json")).expect("valid Python tool inventory");
    let mut schemas: Map<String, Value> =
        serde_json::from_str(include_str!("mcp/output_schemas.json"))
            .expect("valid output schemas");
    for tool in &mut tools {
        let name = tool["name"].as_str().expect("tool name");
        let schema = schemas
            .remove(name)
            .expect("an output schema for each tool");
        assert!(tool.insert("outputSchema".into(), schema).is_none());
    }
    assert!(schemas.is_empty(), "an output schema for an unknown tool");
    tools
        .into_iter()
        .map(|tool| serde_json::from_value(Value::Object(tool)).expect("valid MCP tool"))
        .collect()
});

pub(crate) fn argument_schema(name: &str) -> Option<&'static Map<String, Value>> {
    let tool = TOOLS.iter().find(|tool| tool.name == name)?;
    tool.input_schema
        .get("properties")
        .and_then(Value::as_object)
}

#[derive(Args)]
pub(crate) struct ServeArgs {
    #[arg(long, help = "Override the Keeper GraphQL HTTP endpoint")]
    endpoint: Option<String>,
    #[arg(long, help = "Override the Keeper WebSocket endpoint")]
    ws_endpoint: Option<String>,
    #[arg(long, help = "Override the Nibiru REST endpoint")]
    lcd_endpoint: Option<String>,
}

impl ServeArgs {
    pub(crate) fn run(
        self,
        global: Flags,
        env: &Environment,
        writer: Option<Arc<LogWriter>>,
    ) -> Result<(), SaiError> {
        let flags = Flags {
            endpoint: self.endpoint.or(global.endpoint),
            ws_endpoint: self.ws_endpoint.or(global.ws_endpoint),
            lcd_endpoint: self.lcd_endpoint.or(global.lcd_endpoint),
        };
        if io::stdin().is_terminal() {
            eprintln!(
                "sai mcp serve speaks MCP over stdin and stdout; an agent client should launch it; register it with `sai mcp install`. Press Ctrl-C to stop."
            );
        }
        let settings = crate::load_settings(&flags, env).map_err(|error| {
            let mut error = SaiError::from(error);
            error.message = format!("sai mcp serve: {}", error.message);
            error
        })?;
        let server = SaiMcp {
            settings: Arc::new(settings),
            env: env.clone(),
            writer,
            install: install::Installation::detect(),
            local_ledger_slots: Arc::new(Semaphore::new(2)),
            trade_slots: Arc::new(Semaphore::new(2)),
            reconcile_slots: Arc::new(Semaphore::new(8)),
            wait_slots: Arc::new(Semaphore::new(stdio::MAX_IN_FLIGHT)),
            log_slots: Arc::new(Semaphore::new(2)),
            overview_slots: Arc::new(Semaphore::new(2)),
        };
        server.events().event(
            "mcp.server_start",
            Level::Info,
            [
                ("keeper", Field::Endpoint(server.settings.endpoint().into())),
                (
                    "websocket",
                    Field::Endpoint(server.settings.ws_endpoint().into()),
                ),
                (
                    "lcd",
                    Field::Endpoint(server.settings.lcd_endpoint().into()),
                ),
            ],
        );
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| SaiError::internal())?;
        let outcome = tokio::task::LocalSet::new().block_on(&runtime, server.serve_stdio());
        // Tokio stdin uses a blocking read that can remain open after Ctrl-C.
        runtime.shutdown_timeout(Duration::from_millis(100));
        outcome
    }
}

struct SaiMcp {
    settings: Arc<Settings>,
    env: Environment,
    writer: Option<Arc<LogWriter>>,
    install: install::Installation,
    local_ledger_slots: Arc<Semaphore>,
    trade_slots: Arc<Semaphore>,
    reconcile_slots: Arc<Semaphore>,
    wait_slots: Arc<Semaphore>,
    log_slots: Arc<Semaphore>,
    overview_slots: Arc<Semaphore>,
}

impl SaiMcp {
    async fn serve_stdio(self) -> Result<(), SaiError> {
        let failure = || SaiError::new(Code::InternalError, "Sai MCP stdio session failed");
        let run = async {
            match self.serve(stdio::Stdio::new()).await {
                Ok(service) => match service.waiting().await {
                    Ok(QuitReason::Closed | QuitReason::Cancelled) => Ok(()),
                    _ => Err(failure()),
                },
                Err(ServerInitializeError::ConnectionClosed(_)) => Ok(()),
                Err(_) => Err(failure()),
            }
        };
        tokio::select! {
            result = run => result,
            signal = tokio::signal::ctrl_c() => signal.map_err(|_| failure()),
        }
    }

    fn events(&self) -> EventContext<'_> {
        EventContext::new(self.writer.as_deref(), Surface::Mcp, Some("mcp serve"))
    }

    async fn run_tool(
        &self,
        name: &str,
        args: &Map<String, Value>,
        call_id: &str,
    ) -> Result<Value, SaiError> {
        let addr = args
            .get("addr")
            .and_then(Value::as_str)
            .filter(|addr| !addr.is_empty());
        let storage = Storage::new(&self.env.home);
        let endpoint = self.settings.endpoint();
        let events = self.events().with_tool(name, call_id);
        if let Some(action) = trade::Action::named(name) {
            return self.trade(action, args, call_id).await;
        }
        let keeper = Keeper::new(endpoint, events);
        let query = match name {
            "market_list" => keeper::MARKETS,
            "market_prices" => keeper::PRICES,
            "mcp_info" => return self.info(&storage).await,
            "intent_wait" => return self.wait_for_intent(args, call_id).await,
            "logs_intent" => return self.logs_intent(args).await,
            "account_overview" => {
                return self
                    .account_overview(args.get("addr").and_then(Value::as_str), call_id)
                    .await;
            }
            "auth_list" | "auth_status" | "auth_use" => {
                return self
                    .auth(name, args.get("addr").and_then(Value::as_str), call_id)
                    .await;
            }
            _ => {
                let Some(read) = AccountRead::named(name) else {
                    return Ok(json!({
                        "error": format!("Tool {name} is not implemented in the Rust Sai server"),
                        "code": "NOT_IMPLEMENTED",
                    }));
                };
                let mut account = session::resolve_addr(&storage, endpoint, addr)?;
                let addr = account["addr"].as_str().expect("resolved account address");
                let data = match read {
                    AccountRead::Positions | AccountRead::History => {
                        let vars = Map::from_iter([("trader".into(), json!(addr))]);
                        let query = match read {
                            AccountRead::Positions => keeper::POSITIONS,
                            _ => keeper::HISTORY,
                        };
                        let data = keeper.request(query, vars, None, keeper::TIMEOUT).await?;
                        if matches!(read, AccountRead::History) {
                            keeper::public_account_history(&self.env.home, &data)
                        } else {
                            Value::Object(data)
                        }
                    }
                    AccountRead::Balances => {
                        stream::snapshot(
                            self.settings.ws_endpoint(),
                            &stream::Subscription::Balances(addr.into()),
                            || {},
                            pending(),
                        )
                        .await?
                    }
                    AccountRead::LocalIntents => {
                        let home = self.env.home.clone();
                        let endpoint = endpoint.to_owned();
                        let addr = addr.to_owned();
                        Self::blocking_work(&self.local_ledger_slots, move || {
                            let intents = Storage::new(home).list_pending(&endpoint, &addr)?;
                            Ok(json!({"intents": intents}))
                        })
                        .await?
                    }
                    AccountRead::IntentStatus => {
                        self.intent_status(addr, args.get("key").and_then(Value::as_str), call_id)
                            .await?
                    }
                };
                if let Value::Object(data) = data {
                    account.extend(data);
                    return Ok(Value::Object(account));
                }
                return Err(SaiError::internal());
            }
        };
        Ok(Value::Object(
            keeper
                .request(query, Map::new(), None, keeper::TIMEOUT)
                .await?,
        ))
    }

    async fn auth(&self, name: &str, addr: Option<&str>, call_id: &str) -> Result<Value, SaiError> {
        let home = self.env.home.clone();
        let endpoint = self.settings.endpoint().to_owned();
        let writer = self.writer.clone();
        let (name, addr, call_id) = (name.to_owned(), addr.map(str::to_owned), call_id.to_owned());
        Self::blocking_work(&self.local_ledger_slots, move || {
            let events = EventContext::new(writer.as_deref(), Surface::Mcp, Some("mcp serve"))
                .with_tool(&name, &call_id);
            let log_storage = |event: StorageEvent<'_>| crate::local::storage_event(events, event);
            let storage = Storage::new(&home).with_event_sink(&log_storage);
            match name.as_str() {
                "auth_status" => session::auth_status(&storage, &home, &endpoint, addr.as_deref()),
                "auth_use" => session::auth_use(
                    &storage,
                    &home,
                    &endpoint,
                    addr.as_deref().ok_or_else(SaiError::internal)?,
                ),
                "auth_list" => session::auth_list(&storage, &home, &endpoint),
                _ => Err(SaiError::internal()),
            }
        })
        .await
    }

    async fn blocking_work<T: Send + 'static>(
        slots: &Arc<Semaphore>,
        work: impl FnOnce() -> Result<T, SaiError> + Send + 'static,
    ) -> Result<T, SaiError> {
        // Leave room in Tokio's blocking pool for stdio even if every query
        // is waiting on the same SQLite lock.
        let slot = Arc::clone(slots)
            .acquire_owned()
            .await
            .map_err(|_| SaiError::internal())?;
        tokio::task::spawn_blocking(move || {
            let _slot = slot;
            work()
        })
        .await
        .map_err(|_| SaiError::internal())?
    }

    async fn intent_status(
        &self,
        addr: &str,
        key: Option<&str>,
        call_id: &str,
    ) -> Result<Value, SaiError> {
        let home = self.env.home.clone();
        let endpoint = self.settings.endpoint().to_owned();
        let writer = self.writer.clone();
        let (addr, key, call_id) = (addr.to_owned(), key.map(str::to_owned), call_id.to_owned());
        let runtime = tokio::runtime::Handle::current();
        let (_cancel_on_drop, cancel) = tokio::sync::oneshot::channel::<()>();
        // Reconciliation combines Keeper requests with synchronous SQLite operations.
        Self::blocking_work(&self.reconcile_slots, move || {
            let events = EventContext::new(writer.as_deref(), Surface::Mcp, Some("mcp serve"))
                .with_tool("intent_status", &call_id);
            let log_storage = |event: StorageEvent<'_>| crate::local::storage_event(events, event);
            let storage = Storage::new(&home).with_event_sink(&log_storage);
            let data = runtime.block_on(async {
                tokio::select! {
                    biased;
                    _ = cancel => Err(SaiError::internal()),
                    result = reconcile::reconcile(
                        &endpoint, &addr, key.as_deref(), keeper::TIMEOUT, &storage, events,
                    ) => result,
                }
            })?;
            Ok(keeper::public_intent_status(&home, &data))
        })
        .await
    }

    async fn wait_for_intent(
        &self,
        args: &Map<String, Value>,
        call_id: &str,
    ) -> Result<Value, SaiError> {
        let started = Instant::now();
        let seconds = args
            .get("timeout")
            .and_then(validation::float_value)
            .unwrap_or(MCP_WAIT_DEFAULT.as_secs_f64());
        let timeout = wait::validate_timeout(seconds)?.min(MCP_WAIT_LIMIT);
        let key = args["key"]
            .as_str()
            .ok_or_else(SaiError::internal)?
            .to_owned();
        let addr = args.get("addr").and_then(Value::as_str).map(str::to_owned);
        // Waits hold their worker between polls. Size their pool to stdio admission
        // so a long wait cannot delay a short one or take an intent_status worker.
        let remaining = timeout.saturating_sub(started.elapsed());
        let slot =
            match tokio::time::timeout(remaining, self.wait_slots.clone().acquire_owned()).await {
                Ok(slot) => slot.map_err(|_| SaiError::internal())?,
                // Admission expired before the worker could resolve an account.
                Err(_) => {
                    return Ok(wait_result(Map::new(), wait::unknown_result(&key)));
                }
            };
        let home = self.env.home.clone();
        let endpoint = self.settings.endpoint().to_owned();
        let writer = self.writer.clone();
        let call_id = call_id.to_owned();
        let runtime = tokio::runtime::Handle::current();
        let (_cancel_on_drop, cancel) = tokio::sync::oneshot::channel::<()>();
        tokio::task::spawn_blocking(move || {
            let _slot = slot;
            let events = EventContext::new(writer.as_deref(), Surface::Mcp, Some("mcp serve"))
                .with_tool("intent_wait", &call_id);
            let log_storage = |event: StorageEvent<'_>| {
                crate::local::storage_event_with_endpoint(events, event, Some(&endpoint))
            };
            let storage = Storage::new(&home)
                .with_busy_timeout(Duration::ZERO)
                .with_event_sink(&log_storage);
            runtime.block_on(async {
                tokio::select! {
                    biased;
                    _ = cancel => Err(SaiError::internal()),
                    result = async {
                        let local = if addr.is_none() {
                            storage.intent_record(&endpoint, &key)
                        } else {
                            Ok(None)
                        };
                        let account = match local {
                            Ok(Some((owner, _))) => Map::from_iter([
                                ("addr".into(), json!(owner)),
                                ("addrSource".into(), json!("ledger")),
                            ]),
                            Ok(None) => session::resolve_addr(
                                &storage, &endpoint, addr.as_deref().filter(|addr| !addr.is_empty()),
                            )?,
                            Err(error) if wait::ledger_busy(&error) => {
                                return Ok(wait_result(Map::new(), wait::unknown_result(&key)));
                            }
                            Err(error) => return Err(error.into()),
                        };
                        let addr = account["addr"].as_str().expect("resolved account address");
                        let receipt = wait::for_intent(
                            &endpoint,
                            addr,
                            &key,
                            wait::WaitOptions { timeout, started, remember_fill: None },
                            &storage,
                            events,
                        )
                        .await?;
                        Ok(wait_result(account, receipt))
                    } => result,
                }
            })
        })
        .await
        .map_err(|_| SaiError::internal())?
    }

    async fn info(&self, storage: &Storage<'_>) -> Result<Value, SaiError> {
        let install_dir = self.install.directory().map(Path::to_string_lossy);
        let home = &self.env.home;
        let version = install::RUNNING_VERSION;
        let installed = self.install.version().await;
        let restart_needed = installed
            .as_deref()
            .is_some_and(|installed| installed != version);
        let mut names: Vec<_> = TOOLS.iter().map(|tool| tool.name.as_ref()).collect();
        names.sort_unstable();
        let session = session::StoredSession::load(storage, self.settings.endpoint(), None);
        Ok(json!({
            "name": "sai",
            "package": "sai-cli",
            "version": version,
            "installedVersion": installed.as_deref().unwrap_or("unknown"),
            "restartNeeded": restart_needed,
            "latestVersion": latest_version(home, version),
            "mcpVersion": Implementation::from_build_env().version,
            "provenance": {"kind": "installed", "path": install_dir},
            "endpoints": {
                "keeper": log::origin(self.settings.endpoint()),
                "websocket": log::origin(self.settings.ws_endpoint()),
                "lcd": log::origin(self.settings.lcd_endpoint()),
            },
            "configFile": self.env.path().to_string_lossy(),
            "logs": {
                "dir": home.join("logs").to_string_lossy(),
                "enabled": log::enabled(&self.env),
            },
            "tools": names,
            "trading": session.trading(),
            "session": session.summary(storage, home)?,
        }))
    }

    fn error(&self, error: SaiError) -> Value {
        let message = match error.code {
            Code::InternalError => {
                eprintln!("Unexpected error in Sai MCP tool");
                "Unexpected error in the Sai MCP server; see its stderr log".into()
            }
            _ => log::redact(&self.env.home, &error.message),
        };
        json!({"error": message, "code": error.code})
    }

    async fn checked_call(
        &self,
        name: &str,
        tool: Option<&Tool>,
        mut args: Map<String, Value>,
        call_id: &str,
        events: EventContext<'_>,
    ) -> Result<Value, String> {
        let Some(tool) = tool else {
            log_call(events, None, &args, |_| false);
            return Err(format!("Unknown tool: {name}"));
        };
        // FastMCP checks the session even for missing or malformed trade arguments.
        if name.starts_with("trade_") {
            let addr = args.get("addr").and_then(Value::as_str);
            let state = session::StoredSession::load(
                &Storage::new(&self.env.home),
                self.settings.endpoint(),
                addr,
            );
            if let Some(error) = state.refusal() {
                log_call(events, Some(tool), &args, |_| false);
                return Ok(self.error(error));
            }
        }
        let checked = validation::arguments(tool, &args);
        log_call(events, Some(tool), &args, |name| {
            checked
                .as_ref()
                .err()
                .is_none_or(|error| !error.field(name))
        });
        args.extend(checked.map_err(|error| error.message(tool))?);
        Ok(self
            .run_tool(name, &args, call_id)
            .await
            .unwrap_or_else(|error| {
                if trade::Action::named(name).is_some() {
                    trade::failure(&self.env.home, error)
                } else {
                    self.error(error)
                }
            }))
    }
}

enum AccountRead {
    Positions,
    History,
    Balances,
    LocalIntents,
    IntentStatus,
}

impl AccountRead {
    fn named(name: &str) -> Option<Self> {
        Some(match name {
            "account_positions" => Self::Positions,
            "account_history" => Self::History,
            "account_balances" => Self::Balances,
            "intent_local" => Self::LocalIntents,
            "intent_status" => Self::IntentStatus,
            _ => return None,
        })
    }
}

impl ServerHandler for SaiMcp {
    fn supported_protocol_versions(&self) -> Cow<'static, [ProtocolVersion]> {
        Cow::Borrowed(ProtocolVersion::known_up_to(&ProtocolVersion::V_2025_11_25))
    }

    fn get_info(&self) -> ServerConfig {
        let capabilities: ServerCapabilities = serde_json::from_value(json!({
            "experimental": {},
            "prompts": {"listChanged": false},
            "resources": {"subscribe": false, "listChanged": false},
            "tools": {"listChanged": false},
        }))
        .expect("valid MCP capabilities");
        ServerConfig::new(capabilities)
            .with_server_info(Implementation::new(
                "sai",
                Implementation::from_build_env().version,
            ))
            .with_instructions(INSTRUCTIONS)
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult::with_all_items(TOOLS.clone()))
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let name = request.name.as_ref();
        let args = request.arguments.unwrap_or_default();
        let tool = TOOLS.iter().find(|tool| tool.name == name);
        let started = Instant::now();
        let call_id = Uuid::new_v4().simple().to_string();
        let events = self.events().with_tool(
            if tool.is_some() { name } else { "unknown" },
            &call_id[..12],
        );
        let duration =
            || json!((started.elapsed().as_secs_f64() * 1_000.0).round_ties_even() as u64).into();
        let call = tokio::select! {
            biased;
            _ = context.ct.cancelled() => return Err(stdio::cancelled()),
            result = self.checked_call(name, tool, args, &call_id[..12], events) => result,
        };
        let result = match call {
            Ok(value) => {
                let error = value.get("error").and_then(Value::as_str);
                let level = if error.is_some() {
                    Level::Warn
                } else {
                    Level::Info
                };
                events.event(
                    "mcp.tool_result",
                    level,
                    [
                        ("ok", json!(error.is_none()).into()),
                        ("durationMs", duration()),
                        ("code", value["code"].clone().into()),
                        (
                            "error",
                            error.map_or(Field::Scalar(Value::Null), |text| {
                                Field::Untrusted(text.into())
                            }),
                        ),
                        (
                            "account",
                            value
                                .get("account")
                                .unwrap_or(&value["addr"])
                                .clone()
                                .into(),
                        ),
                    ],
                );
                tool_result(value)
            }
            Err(message) => {
                let message = log::redact(&self.env.home, &message);
                let first = tool.and_then(|_| message.lines().next());
                events.event(
                    "mcp.tool_result",
                    Level::Error,
                    [
                        ("ok", json!(false).into()),
                        ("durationMs", duration()),
                        ("code", json!(Code::InvalidRequest).into()),
                        ("errorType", Field::Text("ToolError".into())),
                        (
                            "error",
                            first.map_or(Field::Scalar(Value::Null), |text| {
                                Field::Untrusted(text.into())
                            }),
                        ),
                    ],
                );
                CallToolResult::error(vec![ContentBlock::text(message)])
            }
        };
        Ok(result.into())
    }
}

fn log_call(
    events: EventContext<'_>,
    tool: Option<&Tool>,
    args: &Map<String, Value>,
    validated: impl Fn(&str) -> bool,
) {
    if events.writer.is_none() {
        return;
    }
    let properties = tool.map(|tool| {
        tool.input_schema["properties"]
            .as_object()
            .expect("tool properties")
    });
    let mut known = Vec::new();
    let mut unknown = 0;
    let mut unknown_metadata = Vec::new();
    for (key, value) in args {
        if !properties.is_some_and(|properties| properties.contains_key(key)) {
            unknown += 1;
            if unknown_metadata.len() < 64 {
                unknown_metadata.push(argument_metadata(value));
            }
        } else if tool.is_some_and(|tool| !matches!(tool.name.as_ref(), "auth_use" | "logs_intent"))
        {
            let field = match value {
                Value::String(_) if validated(key) => Field::redacted_argument(),
                Value::Number(_) | Value::Bool(_) | Value::Null if validated(key) => {
                    Field::Scalar(value.clone())
                }
                _ => argument_metadata(value),
            };
            known.push((key.clone(), field));
        }
    }
    events.event(
        "mcp.tool_call",
        Level::Info,
        [
            ("arguments", Field::Object(known)),
            (
                "unknownArguments",
                json!((unknown > 0).then_some(unknown)).into(),
            ),
        ]
        .into_iter()
        .chain(
            (unknown > 0).then_some(("unknownArgumentMetadata", Field::Array(unknown_metadata))),
        ),
    );
}

fn wait_result(mut account: Map<String, Value>, receipt: wait::ResultReceipt) -> Value {
    let wait::ResultReceipt { outcome, mut body } = receipt;
    if outcome == wait::Outcome::Unknown {
        body["hint"] =
            json!("Call intent_wait again with the same key and address. Do not resubmit.");
    }
    let Value::Object(body) = body else {
        unreachable!("wait returns a receipt object");
    };
    account.extend(body);
    Value::Object(account)
}

fn argument_metadata(value: &Value) -> Field {
    let (kind, length) = match value {
        Value::String(text) => ("string", text.len()),
        Value::Array(items) => ("array", items.len()),
        Value::Object(items) => ("object", items.len()),
        Value::Number(_) => ("number", 1),
        Value::Bool(_) => ("boolean", 1),
        Value::Null => ("null", 1),
    };
    json!({"type": kind, "length": length}).into()
}

/// Return the JSON text that Python's server returns, plus the same value as
/// structured content that matches the tool's output schema.
fn tool_result(value: Value) -> CallToolResult {
    let text = output::inline(&value).expect("MCP tool values serialize to JSON");
    let mut result = CallToolResult::success(vec![ContentBlock::text(text)]);
    debug_assert!(value.is_object(), "MCP tool results are JSON objects");
    result.structured_content = Some(value);
    result
}

/// Read the latest release that `sai update --check` saved, with no network.
///
/// A saved release older than this stable build predates its install, so it
/// is stale. Report it as unknown rather than suggest a downgrade.
fn latest_version(home: &Path, running: &str) -> Option<String> {
    let cache: Value = serde_json::from_slice(&fs::read(home.join("update.json")).ok()?).ok()?;
    if cache["schema"] != 1 || !cache["checkedAt"].is_string() {
        return None;
    }
    let version = cache["latest"].as_str()?.strip_prefix("sai-cli/v")?;
    if !install::is_version(version) {
        return None;
    }
    let stale = semver::Version::parse(running)
        .ok()
        .filter(|running| running.pre.is_empty())
        .zip(semver::Version::parse(version).ok())
        .is_some_and(|(running, latest)| latest < running);
    (!stale).then(|| version.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn queued_server(home: &Path) -> SaiMcp {
        let env = Environment::new(home.to_owned(), |_| None);
        SaiMcp {
            settings: Arc::new(Settings::load(&Flags::default(), &env).unwrap()),
            env,
            writer: None,
            install: install::Installation::detect(),
            local_ledger_slots: Arc::new(Semaphore::new(2)),
            trade_slots: Arc::new(Semaphore::new(2)),
            reconcile_slots: Arc::new(Semaphore::new(8)),
            // No wait slot frees up, so each wait runs out its whole timeout.
            wait_slots: Arc::new(Semaphore::new(0)),
            log_slots: Arc::new(Semaphore::new(2)),
            overview_slots: Arc::new(Semaphore::new(2)),
        }
    }

    #[test]
    fn queued_timeout_omits_an_unresolved_address() {
        let home = tempfile::tempdir().unwrap();
        let server = queued_server(home.path());
        let args = json!({
            "addr": "https://keeper.invalid/?key=fixture-wait-addr",
            "key": "fixture-wait",
            "timeout": 0.01,
        });
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        let result = runtime
            .block_on(server.wait_for_intent(args.as_object().unwrap(), "fixture-call"))
            .unwrap();
        assert_eq!(result["code"], "OUTCOME_UNKNOWN");
        assert_eq!(result["intentKey"], "fixture-wait");
        assert_eq!(result["addr"], Value::Null);
    }

    #[test]
    fn intent_wait_honors_timeouts_up_to_the_cli_wait_limit() {
        let home = tempfile::tempdir().unwrap();
        let server = queued_server(home.path());
        // Tokio's paused clock skips ahead, so the waits finish at once.
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .start_paused(true)
            .build()
            .unwrap();
        for (timeout, waited) in [
            (None, 10),
            (Some(json!(45)), 45),
            (Some(json!(60)), 60),
            (Some(json!(600)), 60),
        ] {
            let mut args = Map::from_iter([("key".into(), json!("fixture-wait"))]);
            if let Some(timeout) = timeout {
                args.insert("timeout".into(), timeout);
            }
            let (result, elapsed) = runtime.block_on(async {
                let started = tokio::time::Instant::now();
                let result = server.wait_for_intent(&args, "fixture-call").await;
                (result.unwrap(), started.elapsed())
            });
            assert_eq!(result["code"], "OUTCOME_UNKNOWN");
            assert_eq!(elapsed.as_secs(), waited, "timeout {args:?}");
        }
    }

    #[test]
    fn latest_version_ignores_a_check_older_than_the_running_release() {
        let home = tempfile::tempdir().unwrap();
        let cache = |latest: &str| {
            let state =
                json!({"schema": 1, "checkedAt": "2026-11-27T22:00:00+00:00", "latest": latest});
            fs::write(home.path().join("update.json"), state.to_string()).unwrap();
        };
        cache("sai-cli/v0.1.3");
        assert_eq!(latest_version(home.path(), "0.2.0"), None);
        assert_eq!(
            latest_version(home.path(), "0.1.3").as_deref(),
            Some("0.1.3")
        );
        assert_eq!(
            latest_version(home.path(), "0.1.2").as_deref(),
            Some("0.1.3")
        );
        // A prerelease build does not know which stable release is latest.
        assert_eq!(
            latest_version(home.path(), "0.2.0-rc.1").as_deref(),
            Some("0.1.3")
        );
        cache("sai-cli/v0.2.0");
        assert_eq!(
            latest_version(home.path(), "0.2.0").as_deref(),
            Some("0.2.0")
        );
    }

    #[test]
    fn tool_results_carry_the_result_object_as_structured_content() {
        let value =
            json!({"addr": "0x1111111111111111111111111111111111111111", "perp": {"trades": []}});
        let result = tool_result(value.clone());
        let [ContentBlock::Text(text)] = result.content.as_slice() else {
            panic!("one text block");
        };
        assert_eq!(serde_json::from_str::<Value>(&text.text).unwrap(), value);
        assert_eq!(result.structured_content, Some(value));
    }

    #[test]
    fn every_tool_lists_an_object_output_schema() {
        for tool in TOOLS.iter() {
            let schema = tool.output_schema.as_deref().expect("output schema");
            assert_eq!(schema["type"], "object", "{}", tool.name);
            assert_eq!(schema["additionalProperties"], true, "{}", tool.name);
            let properties = schema["properties"].as_object().unwrap();
            assert!(properties.contains_key("error"), "{}", tool.name);
            assert!(!properties.contains_key("result"), "{}", tool.name);
        }
    }
}
