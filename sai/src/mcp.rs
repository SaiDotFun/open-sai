mod install;
mod session;
mod stdio;
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
};
use serde_json::{Map, Value, json};
use tokio::sync::Semaphore;
use uuid::Uuid;

use crate::reads::output;

const INSTRUCTIONS: &str = include_str!("mcp/instructions.txt");
// Wire metadata captured from Python main at ea09f0d417a53a71079bb8c5e426005fa29051a4.
static TOOLS: LazyLock<Vec<Tool>> = LazyLock::new(|| {
    serde_json::from_str(include_str!("mcp/tools.json")).expect("valid Python tool inventory")
});

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
            let message = format!("sai mcp serve: {error}");
            if error.is_invalid_endpoint() {
                SaiError::usage(message)
            } else {
                SaiError::new(Code::InvalidRequest, message)
            }
        })?;
        let server = SaiMcp {
            settings,
            env: env.clone(),
            writer,
            install: install::Installation::detect(),
            local_ledger_slots: Arc::new(Semaphore::new(2)),
            reconcile_slots: Arc::new(Semaphore::new(8)),
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
    settings: Settings,
    env: Environment,
    writer: Option<Arc<LogWriter>>,
    install: install::Installation,
    local_ledger_slots: Arc<Semaphore>,
    reconcile_slots: Arc<Semaphore>,
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
        let keeper = Keeper::new(endpoint, events);
        let query = match name {
            "market_list" => keeper::MARKETS,
            "market_prices" => keeper::PRICES,
            "mcp_info" => return Ok(self.info(&storage).await),
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
                        Self::ledger_work(&self.local_ledger_slots, move || {
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

    async fn ledger_work<T: Send + 'static>(
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
        Self::ledger_work(&self.reconcile_slots, move || {
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

    async fn info(&self, storage: &Storage<'_>) -> Value {
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
        json!({
            "name": "sai",
            "package": "sai-cli",
            "version": version,
            "installedVersion": installed.as_deref().unwrap_or("unknown"),
            "restartNeeded": restart_needed,
            "latestVersion": latest_version(home),
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
            "session": session.summary(storage),
        })
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
        args: Map<String, Value>,
        call_id: &str,
    ) -> Result<Value, String> {
        let tool = tool.ok_or_else(|| format!("Unknown tool: {name}"))?;
        // FastMCP checks the session even for missing or malformed trade arguments.
        if name.starts_with("trade_") {
            let addr = args.get("addr").and_then(Value::as_str);
            let state = session::StoredSession::load(
                &Storage::new(&self.env.home),
                self.settings.endpoint(),
                addr,
            );
            if let Some(error) = state.refusal() {
                return Ok(self.error(error));
            }
        }
        let args = validation::arguments(tool, args)?;
        Ok(self
            .run_tool(name, &args, call_id)
            .await
            .unwrap_or_else(|error| self.error(error)))
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
        if self.writer.is_some() {
            log_call(events, tool, &args);
        }
        let duration =
            || json!((started.elapsed().as_secs_f64() * 1_000.0).round_ties_even() as u64).into();
        let call = tokio::select! {
            biased;
            _ = context.ct.cancelled() => return Err(stdio::cancelled()),
            result = self.checked_call(name, tool, args, &call_id[..12]) => result,
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

fn log_call(events: EventContext<'_>, tool: Option<&Tool>, args: &Map<String, Value>) {
    let mut known = Vec::new();
    let mut unknown = 0;
    if let Some(tool) = tool {
        let properties = tool.input_schema["properties"]
            .as_object()
            .expect("tool properties");
        unknown = args
            .keys()
            .filter(|key| !properties.contains_key(*key))
            .count();
        if tool.name != "logs_intent" {
            known.extend(properties.keys().filter_map(|key| {
                let field = match args.get(key)? {
                    Value::String(text) => Field::Arg(text.clone()),
                    Value::Array(items) => Field::Text(format!("[list of {}]", items.len())),
                    Value::Object(items) => Field::Text(format!("[dict of {}]", items.len())),
                    value => Field::Scalar(value.clone()),
                };
                Some((key.clone(), field))
            }));
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
        ],
    );
}

fn tool_result(value: Value) -> CallToolResult {
    let text = output::inline(&value).expect("MCP tool values serialize to JSON");
    let mut result = CallToolResult::success(vec![ContentBlock::text(text.clone())]);
    result.structured_content = Some(json!({"result": text}));
    result
}

fn latest_version(home: &Path) -> Option<String> {
    let cache: Value = serde_json::from_slice(&fs::read(home.join("update.json")).ok()?).ok()?;
    if cache["schema"] != 1 || !cache["checkedAt"].is_string() {
        return None;
    }
    let version = cache["latest"].as_str()?.strip_prefix("sai-cli/v")?;
    install::is_version(version).then(|| version.to_owned())
}
