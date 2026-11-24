mod files;
mod intents;

use std::{
    env, io,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use chrono::{DateTime, SecondsFormat, Utc};
use clap::Args;
use sai_core::{
    errors::{Code, ExitCode, SaiError},
    keeper,
    log::{self, EventContext, Field, Level},
    session::parse_expiry,
    settings::{Environment, Flags, KEYS, Key, Settings, Source},
    storage::{Session, Storage},
};
use serde::Serialize;
use serde_json::{Map, Value, json};

use crate::mcp_registration as mcp;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const DEADLINE: Duration = Duration::from_secs(7);
const TIMEOUT: Duration = Duration::from_secs(5);
const SIGN_IN: &str = "sai auth login NAME, or sai auth import";
const REINSTALL: &str =
    "uv run https://raw.githubusercontent.com/SaiDotFun/open-sai/main/scripts/install.py";
const NETWORK: [&str; 3] = ["keeper", "websocket", "lcd"];
const STATUSES: [&str; 5] = ["pass", "fixed", "warn", "fail", "skipped"];

#[derive(Args)]
pub(crate) struct DoctorArgs {
    /// Skip the Keeper, WebSocket, and LCD checks.
    #[arg(long)]
    offline: bool,
    /// Report problems without fixing any.
    #[arg(long)]
    no_fix: bool,
    /// Keeper WebSocket endpoint.
    #[arg(long)]
    ws_endpoint: Option<String>,
    /// Nibiru REST endpoint.
    #[arg(long)]
    lcd_endpoint: Option<String>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Check {
    id: String,
    status: &'static str,
    summary: String,
    fix: Option<String>,
    changes: Vec<String>,
    details: Value,
    duration_ms: u64,
}

impl Check {
    fn new(id: impl Into<String>, status: &'static str, summary: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            status,
            summary: summary.into(),
            fix: None,
            changes: Vec::new(),
            details: json!({}),
            duration_ms: 0,
        }
    }

    fn fix(mut self, hint: impl Into<String>) -> Self {
        self.fix = Some(hint.into());
        self
    }

    fn details(mut self, details: Value) -> Self {
        self.details = details;
        self
    }

    fn changes(mut self, changes: Vec<String>) -> Self {
        self.changes = changes;
        self
    }

    fn elapsed(mut self, start: Instant) -> Self {
        self.duration_ms = elapsed_ms(start);
        self
    }

    fn event(&self) -> Value {
        let mut value =
            json!({"id": self.id, "status": self.status, "durationMs": self.duration_ms});
        if !matches!(self.status, "pass" | "skipped") {
            value["summary"] = json!(self.summary);
        }
        if !self.changes.is_empty() {
            value["changes"] = json!(self.changes);
        }
        value
    }
}

fn elapsed_ms(start: Instant) -> u64 {
    (start.elapsed().as_secs_f64() * 1000.).round_ties_even() as u64
}

fn timed(check: impl FnOnce() -> Check) -> Check {
    let start = Instant::now();
    check().elapsed(start)
}

fn crashed(id: &str, class: &str) -> Check {
    Check::new(id, "fail", format!("Check crashed with {class}"))
        .fix("Report this at https://github.com/SaiDotFun/open-sai/issues")
}

fn plural(count: usize, word: &str) -> String {
    let suffix = if count == 1 { "" } else { "s" };
    format!("{count} {word}{suffix}")
}

fn short(addr: &str) -> String {
    addr.char_indices()
        .nth(6)
        .map_or_else(|| addr.into(), |(index, _)| format!("{}…", &addr[..index]))
}

pub(crate) fn safe_log_path(env: &Environment) -> bool {
    !env.home.is_symlink() && !env.home.join("logs").is_symlink()
}

impl DoctorArgs {
    pub(crate) fn run(
        self,
        flags: Flags,
        env: &Environment,
        json_output: bool,
        events: EventContext<'_>,
        start_log: impl FnOnce(),
    ) -> Result<(), SaiError> {
        let flags = Flags {
            ws_endpoint: self.ws_endpoint.or(flags.ws_endpoint),
            lcd_endpoint: self.lcd_endpoint.or(flags.lcd_endpoint),
            ..flags
        };
        let fix = !self.no_fix;
        let (config, settings, invalid_endpoint) = config_check(&flags, env);
        let code = if config.status == "fail" {
            Code::InvalidRequest
        } else {
            Code::InternalError
        };
        let (install, provenance, native) = install_info();
        let mut checks = vec![
            config,
            install,
            timed(|| version_check(&env.home, native)),
            timed(|| files::home_check(&env.home, fix)),
        ];
        start_log();
        if let Some(writer) = events.writer {
            writer.flush(Duration::from_secs(1));
        }
        checks.push(timed(|| files::logs_check(env, fix)));

        let started = Instant::now();
        match mcp::check_registrations(fix, DEADLINE) {
            Ok(results) => checks.extend(results.into_iter().map(|report| {
                let mut check = Check::new(
                    format!("mcp.{}", report.client),
                    report.status,
                    report.summary,
                )
                .changes(report.changes)
                .elapsed(started);
                check.fix = report.fix;
                check
            })),
            Err(_) => checks.push(crashed("mcp", "OSError").elapsed(started)),
        }

        let skipped = |id| Check::new(id, "skipped", "Settings could not be loaded");
        if let Some(settings) = &settings {
            let now = Utc::now();
            let started = Instant::now();
            let (check, session) = session_check(&env.home, settings, now);
            checks.push(check.elapsed(started));
            checks.push(timed(|| {
                intents::check(
                    &env.home,
                    settings,
                    session.as_ref(),
                    fix,
                    self.offline,
                    events,
                )
            }));
        } else {
            checks.extend([skipped("session"), skipped("intents")]);
        }
        if self.offline {
            checks.extend(NETWORK.map(|id| Check::new(id, "skipped", "Skipped with --offline")));
        } else if let Some(settings) = &settings {
            checks.extend(network_checks(
                settings,
                EventContext {
                    command: None,
                    ..events
                },
            ));
        } else {
            checks.extend(NETWORK.map(skipped));
        }

        let overall = if checks.iter().any(|check| check.status == "fail") {
            "fail"
        } else if checks.iter().any(|check| check.status == "warn") {
            "warn"
        } else {
            "pass"
        };
        events.event(
            "doctor.result",
            if overall == "pass" {
                Level::Info
            } else {
                Level::Warn
            },
            [
                ("overall", json!(overall).into()),
                ("fix", json!(fix).into()),
                ("offline", json!(self.offline).into()),
                (
                    "checks",
                    Field::from(json!(checks.iter().map(Check::event).collect::<Vec<_>>())),
                ),
                ("code", json!((overall == "fail").then_some(code)).into()),
            ],
        );

        let report = || {
            json!({
                "schemaVersion": 1,
                "generatedAt": Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false),
                "sai": {"version": VERSION, "install": provenance},
                "overall": overall,
                "checks": checks,
            })
        };
        if overall == "fail" {
            let message = if json_output {
                checks
                    .iter()
                    .filter(|check| check.status == "fail")
                    .map(|check| {
                        let fix = check
                            .fix
                            .as_ref()
                            .map_or_else(String::new, |fix| format!(" Fix: {fix}"));
                        format!("{}: {}{fix}", check.id, check.summary)
                    })
                    .collect::<Vec<_>>()
                    .join("\n")
            } else {
                render_text(&checks, overall)
            };
            let mut error = SaiError::new(code, message);
            if invalid_endpoint {
                error.exit_code = ExitCode::Usage;
            }
            if json_output {
                error.details = report().as_object().cloned().map(Box::new);
            }
            return Err(error);
        }
        let redact = |text: &str| log::redact(&env.home, text);
        let text = if json_output {
            let report = crate::redact_details(&report(), &redact);
            crate::local::ascii_json(&report.to_string()).into_owned()
        } else {
            redact(&render_text(&checks, overall))
        };
        println!("{text}");
        Ok(())
    }
}

fn config_check(flags: &Flags, env: &Environment) -> (Check, Option<Settings>, bool) {
    let edit = "Edit config.toml in the Sai home directory and rerun `sai doctor`";
    let (resolved, invalid) = match Settings::for_doctor(flags, env) {
        Ok(result) => result,
        Err(error) => {
            let message = error.to_string();
            let message = if error.is_invalid_endpoint() {
                message
                    .strip_prefix(&format!("{}: ", env.path().display()))
                    .unwrap_or(&message)
            } else {
                &message
            };
            return (
                Check::new("config", "fail", message).fix(edit),
                None,
                error.is_invalid_endpoint(),
            );
        }
    };
    let settings = match resolved {
        Ok(settings) => settings,
        Err(error) => {
            return (
                Check::new("config", "fail", error.to_string())
                    .fix("Check the settings listed by `sai config --help`"),
                None,
                error.is_invalid_endpoint(),
            );
        }
    };
    let invalid_env = KEYS
        .into_iter()
        .filter(|key| settings.source(*key) == Source::Invalid && env.is_set(*key))
        .map(|key| format!("{} ({})", key.spec().env, key.spec().name))
        .collect::<Vec<_>>();
    let check = if !invalid.is_empty() {
        Check::new(
            "config",
            "fail",
            format!("Invalid config.toml settings: {}", invalid.join(", ")),
        )
        .fix(edit)
    } else if !invalid_env.is_empty() {
        Check::new(
            "config",
            "warn",
            format!("Invalid environment settings: {}", invalid_env.join(", ")),
        )
        .fix("Correct or unset the listed environment variables")
    } else if settings
        .warnings
        .iter()
        .any(|message| message.contains("ignoring unknown setting"))
    {
        Check::new("config", "warn", "config.toml contains unknown settings").fix(edit)
    } else {
        Check::new("config", "pass", "Config file settings are valid")
    };
    (check, Some(settings), false)
}

fn install_info() -> (Check, Value, bool) {
    let start = Instant::now();
    let executable = env::current_exe().unwrap_or_else(|_| PathBuf::from("sai"));
    let checkout = mcp::checkout_root(&executable);
    let provenance = if let Some(root) = checkout {
        json!({"kind": "source-checkout", "path": root, "git": mcp::checkout_state(root)})
    } else {
        json!({"kind": "installed", "path": executable})
    };
    let launchers = mcp::launchers_on_path();
    let mut details = json!({"version": VERSION, "provenance": provenance, "onPath": launchers});
    let cached = env::home_dir().is_some_and(|home| mcp::in_uv_cache(&executable, &home));
    let native = checkout.is_none() && !cached;
    let check = if launchers.len() > 1 {
        Check::new(
            "install",
            "warn",
            format!(
                "{} sai installs on PATH; {} runs",
                launchers.len(),
                launchers[0].display()
            ),
        )
        .fix("Keep one; `which -a sai` lists them")
    } else if cached {
        details["kind"] = json!("uvx");
        Check::new(
            "install",
            "warn",
            format!(
                "sai {VERSION} runs from a uvx cache, which has no stable path and does not update"
            ),
        )
        .fix(format!("Install it: {REINSTALL}"))
    } else if let Some(root) = checkout {
        details["kind"] = json!("checkout");
        Check::new(
            "install",
            "warn",
            format!(
                "sai {VERSION} runs from a source checkout at {}",
                root.display()
            ),
        )
        .fix(format!("For a regular install: {REINSTALL}"))
    } else {
        details["kind"] = json!("native");
        Check::new("install", "pass", format!("sai {VERSION} installed"))
    };
    (check.details(details).elapsed(start), provenance, native)
}

fn version_parts(version: &str) -> Option<[u64; 3]> {
    let fields = version
        .trim_end_matches('\n')
        .split('.')
        .map(|field| {
            if field.is_empty()
                || field.len() > 1 && field.starts_with('0')
                || !field.bytes().all(|b| b.is_ascii_digit())
            {
                return None;
            }
            field.parse().ok()
        })
        .collect::<Option<Vec<_>>>()?;
    fields.try_into().ok()
}

fn version_check(home: &Path, native: bool) -> Check {
    let state = files::open_regular(&home.join("update.json"))
        .ok()
        .and_then(|file| serde_json::from_reader::<_, Value>(file).ok())
        .filter(|state| state["schema"] == 1)
        .unwrap_or_default();
    let latest = state["latest"]
        .as_str()
        .map(|tag| tag.strip_prefix("sai-cli/v").unwrap_or(tag));
    let result = state["result"].as_object();
    let details = json!({"installed": VERSION, "latest": latest, "checkedAt": state["checkedAt"], "lastUpdate": result});
    let behind = |release: &str| {
        version_parts(release)
            .zip(version_parts(VERSION))
            .is_some_and(|(release, running)| release > running)
    };
    let how = if native { "`sai update`" } else { REINSTALL };
    let check = if state["result"]["status"] == "failed"
        && state["result"]["to"].as_str().is_some_and(behind)
    {
        let reason = state["result"]["error"].as_str().unwrap_or("None");
        Check::new(
            "version",
            "warn",
            format!("The last update failed: {reason}"),
        )
        .fix(format!("Retry: {how}"))
    } else if let Some(latest) = latest {
        if state["latest"]
            .as_str()
            .is_some_and(|tag| tag.starts_with("sai-cli/v"))
            && behind(latest)
        {
            Check::new(
                "version",
                "warn",
                format!("sai {VERSION} is behind the latest release {latest}"),
            )
            .fix(format!("Update: {how}"))
        } else {
            Check::new(
                "version",
                "pass",
                format!("sai {VERSION} is up to date with the latest release"),
            )
        }
    } else {
        Check::new(
            "version",
            "pass",
            format!("sai {VERSION}; no release check is recorded yet"),
        )
    };
    check.details(details)
}

fn session_check(home: &Path, settings: &Settings, now: DateTime<Utc>) -> (Check, Option<Session>) {
    let storage = Storage::new(home);
    let signed_out = || {
        Check::new(
            "session",
            "warn",
            "Signed out; MCP trading tools are unavailable",
        )
        .fix(SIGN_IN)
        .details(json!({"signedIn": false}))
    };
    if home.is_symlink() || home.join("trader").is_symlink() || !storage.session_exists() {
        return (signed_out(), None);
    }
    let bad = |reason: String| {
        Check::new("session", "fail", reason)
            .fix(SIGN_IN)
            .details(json!({"signedIn": false}))
    };
    let session = match storage.load_session(None) {
        Ok(session) => session,
        Err(error) => return (bad(error.to_string()), None),
    };
    let Some(expiry) = parse_expiry(session.expires_at()) else {
        if parse_expiry(&format!("{}Z", session.expires_at())).is_some() {
            return (
                bad("Stored session is invalid: expiry has no time zone".into()),
                None,
            );
        }
        return (bad("Stored session is invalid (ValueError); sign in again with `sai auth login NAME` or `sai auth import`".into()), None);
    };
    let addr = short(session.addr());
    let endpoint = session.endpoint().map(log::origin);
    let matches = session.endpoint() == Some(settings.endpoint());
    let details = json!({"addr": session.addr(), "expiresAt": session.expires_at(), "endpoint": endpoint, "endpointMatches": matches});
    let check = if expiry <= now {
        Check::new(
            "session",
            "fail",
            format!("Session for {addr} expired at {}", session.expires_at()),
        )
        .fix(renewal_hint(home, &session))
    } else if !matches {
        let login = keeper_hint(home, settings.endpoint(), &["auth", "login", "NAME"]);
        match endpoint {
            Some(origin) => Check::new("session", "fail", format!("Session for {addr} belongs to a different Keeper endpoint. Stored origin {origin}, configured origin {}", log::origin(settings.endpoint())))
                .fix(format!("{login}, or restore the Keeper URL used to sign in")),
            None => Check::new("session", "fail", format!("Session for {addr} has no stored Keeper endpoint")).fix(login),
        }
    } else if expiry - now < chrono::TimeDelta::days(3) {
        let minutes = (expiry - now).num_minutes();
        let when = if minutes == 0 {
            "under 1 min".into()
        } else if minutes < 120 {
            format!("{minutes} min")
        } else {
            format!("{} h", minutes / 60)
        };
        Check::new(
            "session",
            "warn",
            format!("Sai session for {} expires in {when}.", session.addr()),
        )
        .fix(renewal_hint(home, &session))
    } else {
        Check::new(
            "session",
            "pass",
            format!("Signed in as {addr} until {}", session.expires_at()),
        )
    };
    (check.details(details), Some(session))
}

fn renewal_hint(home: &Path, session: &Session) -> String {
    let endpoint = session.endpoint();
    let origin = endpoint.map(log::origin);
    let suffix = if endpoint.is_none() || endpoint != origin.as_deref() {
        let at = origin
            .filter(|origin| origin != "[redacted url]")
            .map_or_else(String::new, |origin| format!(" at {origin}"));
        format!(" (reuse the Keeper endpoint{at} configured when this session was stored)")
    } else {
        String::new()
    };
    let command = |args: &[&str]| match endpoint {
        Some(endpoint) => keeper_hint(home, endpoint, args),
        None => format!(
            "sai {}",
            args.iter()
                .map(|arg| shell_quote(arg))
                .collect::<Vec<_>>()
                .join(" ")
        ),
    };
    let next = if session.has_named_signer() {
        command(&["auth", "renew", session.addr()])
    } else if session.is_imported() {
        command(&["auth", "import"])
    } else {
        format!(
            "{} or {}",
            command(&["auth", "login", "NAME"]),
            command(&["auth", "import"])
        )
    };
    format!("{next}{suffix}")
}

fn shell_quote(arg: &str) -> String {
    if !arg.is_empty()
        && arg
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&b))
    {
        return arg.to_owned();
    }
    format!("'{}'", arg.replace('\'', "'\"'\"'"))
}

fn keeper_hint(home: &Path, endpoint: &str, args: &[&str]) -> String {
    let flag = if log::origin(endpoint) == endpoint && log::redact(home, endpoint) == endpoint {
        shell_quote(endpoint)
    } else {
        r#""${SAI_RECONCILE_ENDPOINT:?Set SAI_RECONCILE_ENDPOINT to the original Keeper URL}""#
            .into()
    };
    let args = args
        .iter()
        .map(|arg| shell_quote(arg))
        .collect::<Vec<_>>()
        .join(" ");
    format!("sai --endpoint {flag} {args}")
}

fn render_text(checks: &[Check], overall: &str) -> String {
    let width = checks
        .iter()
        .map(|check| check.id.len())
        .max()
        .unwrap_or(0)
        .max(9)
        + 2;
    let mut lines = Vec::new();
    for check in checks {
        lines.push(format!(
            "{:<8}{:<width$}{}",
            check.status, check.id, check.summary
        ));
        let indent = width + 8;
        lines.extend(
            check
                .changes
                .iter()
                .map(|change| format!("{:indent$}did: {change}", "")),
        );
        if let Some(fix) = &check.fix
            && !fix.is_empty()
        {
            lines.push(format!("{:indent$}fix: {fix}", ""));
        }
    }
    let counts = STATUSES
        .iter()
        .filter_map(|status| {
            let count = checks
                .iter()
                .filter(|check| check.status == *status)
                .count();
            (count != 0).then(|| format!("{count} {status}"))
        })
        .collect::<Vec<_>>();
    lines.push(format!("\noverall: {overall} ({})", counts.join(", ")));
    lines.join("\n")
}

fn runtime() -> io::Result<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
}

fn network_checks(settings: &Settings, events: EventContext<'_>) -> [Check; 3] {
    let Ok(rt) = runtime() else {
        return NETWORK.map(|id| crashed(id, "OSError"));
    };
    let mut checks = rt.block_on(async {
        let (keeper, websocket, lcd) = tokio::join!(
            bounded("keeper", keeper_check(settings, events)),
            bounded("websocket", ws_check(settings)),
            bounded("lcd", lcd_check(settings)),
        );
        [keeper, websocket, lcd]
    });
    rt.shutdown_background();
    let [keeper, _, lcd] = &mut checks;
    if lcd.status == "pass"
        && let Some(chain) = keeper.details["chainId"].as_str()
        && !chain.is_empty()
        && lcd.details["network"] != chain
    {
        lcd.status = "warn";
        lcd.summary.push_str(&format!("; Keeper trades on {chain}"));
        lcd.fix = Some("Pass the --lcd-endpoint for Keeper's chain".into());
    }
    checks
}

async fn bounded(id: &'static str, probe: impl Future<Output = Check>) -> Check {
    let start = Instant::now();
    tokio::time::timeout(DEADLINE, probe)
        .await
        .unwrap_or_else(|_| {
            Check::new(id, "fail", "No answer within 7 s")
                .fix("Check the network and the endpoint options")
        })
        .elapsed(start)
}

async fn keeper_check(settings: &Settings, events: EventContext<'_>) -> Check {
    let start = Instant::now();
    let endpoint = settings.endpoint();
    let origin = log::origin(endpoint);
    let fail = |reason: &str| {
        Check::new(
            "keeper",
            "fail",
            format!("{origin} did not answer: {reason}"),
        )
        .fix("Check the network and --endpoint")
    };
    let data = match keeper::Keeper::new(endpoint, events)
        .request(keeper::ROUTE, Map::new(), None, TIMEOUT)
        .await
    {
        Ok(data) => data,
        Err(error) => return fail(&error.message),
    };
    let Some(route) = data.get("oneClickTrading") else {
        return fail("'oneClickTrading'");
    };
    let Some(route) = route.as_object() else {
        return fail("'NoneType' object is not subscriptable");
    };
    let Some(enabled) = route.get("enabled") else {
        return fail("'enabled'");
    };
    let enabled = match enabled {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(num) => num.as_f64() != Some(0.),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    };
    let details =
        json!({"endpoint": origin, "tradingEnabled": enabled, "chainId": route.get("chainId")});
    let answered = format!("{origin} answered in {} ms", elapsed_ms(start));
    if enabled {
        Check::new("keeper", "pass", answered).details(details)
    } else {
        Check::new(
            "keeper",
            "warn",
            format!("{answered}, but trading is disabled"),
        )
        .fix("Wait for Keeper to enable trading; reads still work")
        .details(details)
    }
}

async fn ws_check(settings: &Settings) -> Check {
    let start = Instant::now();
    let endpoint = settings.ws_endpoint();
    let origin = log::origin(endpoint);
    let check = match keeper::websocket_handshake(endpoint, TIMEOUT).await {
        Ok(()) => Check::new(
            "websocket",
            "pass",
            format!("{origin} acknowledged in {} ms", elapsed_ms(start)),
        ),
        Err(reason) => Check::new(
            "websocket",
            "fail",
            format!("{origin} sent no connection_ack: {reason}"),
        )
        .fix("Check the network and --ws-endpoint"),
    };
    check.details(json!({"endpoint": origin}))
}

async fn lcd_check(settings: &Settings) -> Check {
    let start = Instant::now();
    let endpoint = settings.lcd_endpoint();
    let display = Key::LcdEndpoint
        .public_value(&json!(endpoint))
        .as_str()
        .unwrap_or_default()
        .to_owned();
    let mut details = json!({"endpoint": display});
    let check = match keeper::lcd_network(endpoint, TIMEOUT).await {
        Ok(Some(network)) => {
            let check = Check::new(
                "lcd",
                "pass",
                format!(
                    "{display} answered in {} ms (network {network})",
                    elapsed_ms(start)
                ),
            );
            details["network"] = json!(network);
            check
        }
        Ok(None) => Check::new(
            "lcd",
            "fail",
            format!("{display} answered without a chain network in node_info"),
        )
        .fix("Check --lcd-endpoint points at a Nibiru REST (LCD) service"),
        Err(reason) => Check::new("lcd", "fail", format!("{display} did not answer: {reason}"))
            .fix("Check the network and --lcd-endpoint"),
    };
    check.details(details)
}
