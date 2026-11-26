mod client;
mod uninstall;
pub(crate) mod update_repair;

use std::{
    env,
    ffi::OsStr,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

use clap::{Args, Subcommand, ValueEnum};
use sai_core::{
    errors::{Code, ExitCode, SaiError},
    log::LogWriter,
    settings::{Environment, Flags, KEYS},
};
use serde_json::{Value, json};

use crate::doctor::shell_quote;

use client::{CodexLookup, Result as ClientResult, absolute, basename, resolved, which};

const RETIRED_NAME: &str = "sai-mcp";
const NAMES: [&str; 2] = ["sai", RETIRED_NAME];
const CLIENTS: [Client; 3] = [Client::Claude, Client::Codex, Client::Cursor];
const INSTALL: &str = "uv tool install git+https://github.com/SaiDotFun/open-sai";
const CLIENT_TIMEOUT: Duration = Duration::from_secs(60);
const CODEX_PROJECT_ENTRY: &str = "Codex uses a project-level sai entry in this directory that overrides the user entry; edit or remove it in the project's .codex/config.toml, then run this again";
const CODEX_STILL_ACTIVE: &str = "a project .codex/config.toml entry is still active";

#[derive(Args)]
pub(crate) struct McpArgs {
    #[command(subcommand)]
    pub(crate) command: Option<McpCommand>,
}

#[derive(Subcommand)]
pub(crate) enum McpCommand {
    /// Show detected agent clients and where Sai is registered.
    List,
    /// Register sai mcp serve with Claude Code, Codex, or Cursor.
    Install(InstallArgs),
    /// Remove Sai from Claude Code, Codex, or Cursor.
    Uninstall(uninstall::UninstallArgs),
    /// Serve Sai tools to an agent client over stdio.
    Serve(crate::mcp::ServeArgs),
}

#[derive(Args)]
pub(crate) struct InstallArgs {
    clients: Vec<String>,
    #[arg(long = "client")]
    client_options: Vec<String>,
    #[arg(long, value_parser = NAMES)]
    repair_existing: Option<String>,
    #[arg(long)]
    reset_args: bool,
    #[arg(long = "all")]
    all_clients: bool,
    #[arg(long)]
    dry_run: bool,
    #[arg(long, value_enum, default_value_t = Scope::User)]
    scope: Scope,
    #[arg(last = true, value_name = "SERVER_ARGV")]
    server_argv: Vec<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
enum Scope {
    User,
    Project,
    Local,
}

impl Scope {
    const fn name(self) -> &'static str {
        match self {
            Self::Local => "local",
            Self::Project => "project",
            Self::User => "user",
        }
    }

    const fn rank(self) -> u8 {
        match self {
            Self::Local => 0,
            Self::Project => 1,
            Self::User => 2,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Client {
    Claude,
    Codex,
    Cursor,
}

impl Client {
    const fn name(self) -> &'static str {
        match self {
            Self::Claude => "claude",
            Self::Codex => "codex",
            Self::Cursor => "cursor",
        }
    }
}

#[derive(Clone, Copy)]
enum TargetSource {
    Generated,
    UserSupplied,
    Saved,
}

#[derive(Clone)]
struct Target {
    argv: Vec<String>,
    profile: Option<String>,
    source: TargetSource,
}

impl Target {
    fn generated(launcher: String, profile: Option<String>) -> Self {
        Self {
            argv: vec![launcher, "mcp".into(), "serve".into()],
            profile,
            source: TargetSource::Generated,
        }
    }

    fn explicit(&self) -> bool {
        matches!(self.source, TargetSource::UserSupplied)
    }

    fn reported_argv(&self) -> Vec<String> {
        if matches!(self.source, TargetSource::Generated) {
            return self.argv.clone();
        }
        let (launcher, args) = self.argv.split_first().expect("target has a launcher");
        std::iter::once(launcher.clone())
            .chain(
                (1..=args.len())
                    .map(|index| format!("<argument hidden {index} of {}>", args.len())),
            )
            .collect()
    }

    fn repair(&self, args: Vec<String>, normalized: bool) -> Self {
        let launcher = if normalized {
            normalize_launcher(&self.argv[0])
        } else {
            self.argv[0].clone()
        };
        Self {
            argv: std::iter::once(launcher).chain(args).collect(),
            profile: None,
            source: TargetSource::Saved,
        }
    }
}

struct Registration {
    // Codex compares the complete replies, ignoring key order. JSON
    // adapters use the original entry to detect an edit during repair.
    raw: Value,
    argv: Vec<String>,
    scope: Scope,
    profile: Option<String>,
    invalid_profile: bool,
    stale_env: Vec<String>,
    disabled: bool,
    extra: Vec<&'static str>,
    stdio: bool,
}

impl Registration {
    fn new(
        raw: Value,
        entry: &Value,
        scope: Scope,
        disabled: bool,
        extra: Vec<&'static str>,
    ) -> Option<Self> {
        if !entry.is_object() {
            return None;
        }
        let profile = entry["env"]["SAI_HOME"].as_str().map(str::to_owned);
        let invalid_profile = profile.is_none() && entry["env"].get("SAI_HOME").is_some();
        let mut stale_env = std::collections::BTreeSet::new();
        if let Some(env) = entry["env"].as_object() {
            stale_env.extend(env.keys().filter(|key| key.as_str() != "SAI_HOME").cloned());
        }
        if let Some(items) = entry["env_vars"].as_array() {
            stale_env.extend(items.iter().filter_map(|item| {
                item.as_str()
                    .or_else(|| item["name"].as_str())
                    .map(str::to_owned)
            }));
        }
        Some(Self {
            raw,
            argv: argv(entry),
            scope,
            profile,
            invalid_profile,
            stale_env: stale_env.into_iter().collect(),
            disabled,
            extra,
            stdio: entry.get("type").is_none_or(|kind| kind == "stdio")
                && entry.get("url").is_none(),
        })
    }

    fn matches_command(&self, target: &Target) -> bool {
        self.argv == target.argv
            || !self.argv.is_empty()
                && self.argv[1..] == target.argv[1..]
                && normalize_launcher(&self.argv[0]) == normalize_launcher(&target.argv[0])
    }

    fn matches_profile(&self, profile: &Option<String>) -> bool {
        !self.invalid_profile && self.profile == *profile
    }

    fn matches(&self, target: &Target) -> bool {
        self.matches_command(target)
            && self.matches_profile(&target.profile)
            && self.stale_env.is_empty()
            && self.extra.is_empty()
            && !self.disabled
    }

    fn report(&self, target: Option<&Target>) -> Value {
        let mut out = if let Some(target) = target.filter(|target| self.matches_command(target)) {
            json!({"command": target.reported_argv()})
        } else if let Some(command) = self.argv.first() {
            let mut out = json!({"command": [basename(command)]});
            if self.argv.len() > 1 {
                out["args"] = json!("<hidden>");
            }
            out
        } else {
            json!({"command": []})
        };
        if !self.stale_env.is_empty() {
            out["staleEnv"] = json!(self.stale_env);
        }
        if !self.extra.is_empty() {
            out["extraSettings"] = json!(self.extra);
        }
        if self.disabled {
            out["disabled"] = json!(true);
        }
        out
    }
}

fn argv(entry: &Value) -> Vec<String> {
    let Some(command) = entry["command"].as_str() else {
        return Vec::new();
    };
    let args = match entry.get("args") {
        None => Vec::new(),
        Some(args) => {
            let Some(args) = args
                .as_array()
                .and_then(|items| items.iter().map(Value::as_str).collect::<Option<Vec<_>>>())
            else {
                return Vec::new();
            };
            args
        }
    };
    std::iter::once(command)
        .chain(args)
        .map(str::to_owned)
        .collect()
}

fn normalize_launcher(command: &str) -> String {
    let path = Path::new(command);
    match (path.is_absolute(), path.parent(), path.file_name()) {
        (true, Some(parent), Some(name)) => {
            resolved(parent).join(name).to_string_lossy().into_owned()
        }
        _ => command.to_owned(),
    }
}

fn cache_roots(home: &Path) -> Vec<PathBuf> {
    let mut paths = [
        ("UV_CACHE_DIR", ""),
        ("XDG_CACHE_HOME", "uv"),
        ("LOCALAPPDATA", "uv/cache"),
    ]
    .into_iter()
    .filter_map(|(var, subdir)| {
        env::var_os(var)
            .filter(|v| !v.is_empty())
            .map(|path| PathBuf::from(path).join(subdir))
    })
    .collect::<Vec<_>>();
    paths.extend([home.join(".cache/uv"), home.join("Library/Caches/uv")]);
    paths
}

pub(crate) fn in_uv_cache(path: &Path, home: &Path) -> bool {
    let names = [absolute(path), resolved(path)];
    cache_roots(home).iter().any(|root| {
        [absolute(root), resolved(root)]
            .iter()
            .any(|root| names.iter().any(|path| path.starts_with(root)))
    })
}

fn profile(home: &Path) -> Option<String> {
    profile_value(home, &env::var_os("SAI_HOME")?)
}

fn profile_value(home: &Path, value: &OsStr) -> Option<String> {
    if value.is_empty() {
        return None;
    }
    let path = Path::new(value);
    let path = path
        .strip_prefix("~")
        .map_or_else(|_| path.to_owned(), |tail| home.join(tail));
    Some(absolute(&path).to_string_lossy().into_owned())
}

fn server_target(override_argv: Vec<String>, home: &Path) -> ClientResult<Target> {
    let profile = profile(home);
    if !override_argv.is_empty() {
        return Ok(Target {
            argv: override_argv,
            profile,
            source: TargetSource::UserSupplied,
        });
    }
    let path = which("sai").map(|path| absolute(&path)).ok_or_else(|| {
        format!("No `sai` on PATH to register. Install it with `{INSTALL}`, or pass the server command after `--`.")
    })?;
    if in_uv_cache(&path, home) {
        return Err(format!(
            "The `sai` on PATH ({}) is inside the uv cache, which uv cleans up, so a client registered with it would break. Install sai first with `{INSTALL}`, then rerun this command.",
            path.display()
        ));
    }
    Ok(Target::generated(
        path.to_string_lossy().into_owned(),
        profile,
    ))
}

fn reported_target(override_argv: Vec<String>, home: &Path) -> ClientResult<Target> {
    let target = server_target(override_argv, home)?;
    if !target.explicit()
        && let Ok(exe) = env::current_exe()
        && resolved(Path::new(&target.argv[0])) != resolved(&exe)
    {
        eprintln!(
            "The `sai` on PATH ({}) is a different install than the one running ({}); clients get the one on PATH.",
            target.argv[0],
            exe.display()
        );
    }
    Ok(target)
}

fn doctor_target(home: &Path) -> Option<Target> {
    use std::os::unix::fs::MetadataExt;

    let executable = env::current_exe().ok()?;
    if in_uv_cache(&executable, home) {
        return None;
    }
    if checkout_root(&executable).is_some() {
        // Python's source checkouts also use the developer's PATH.
        return server_target(Vec::new(), home).ok();
    }
    #[cfg(target_os = "linux")]
    let running = std::fs::metadata("/proc/self/exe").ok()?;
    #[cfg(not(target_os = "linux"))]
    let running = executable.metadata().ok()?;

    let path = env::var_os("PATH").unwrap_or_default();
    let launchers = std::iter::once(home.join(".local/bin/sai"))
        .chain(env::split_paths(&path).map(|dir| absolute(&dir).join("sai")));
    let launcher = launchers.into_iter().find(|launcher| {
        launcher.is_absolute()
            && !in_uv_cache(launcher, home)
            && launcher.to_str().and_then(which).is_some()
            && launcher
                .metadata()
                .is_ok_and(|meta| meta.dev() == running.dev() && meta.ino() == running.ino())
    })?;
    Some(Target::generated(
        launcher.into_os_string().into_string().ok()?,
        profile(home),
    ))
}

pub(crate) fn checkout_root(executable: &Path) -> Option<&Path> {
    executable.ancestors().skip(1).find(|path| {
        path.join("Cargo.toml").is_file()
            && path.join("pyproject.toml").is_file()
            && path.join("sai/src/main.rs").is_file()
    })
}

struct Clients {
    home: PathBuf,
    cwd: Option<PathBuf>,
}

impl Clients {
    fn open() -> ClientResult<Self> {
        let home = env::home_dir().ok_or("Could not determine the home directory")?;
        let cwd = env::current_dir().map_err(|_| "Could not determine the working directory")?;
        Ok(Self {
            home,
            cwd: Some(cwd),
        })
    }

    // Commands opened without a project can write only user registrations.
    fn cwd(&self) -> &Path {
        self.cwd.as_deref().unwrap_or(&self.home)
    }

    fn registrations(
        &self,
        client: Client,
        name: &str,
        deadline: Instant,
    ) -> ClientResult<Vec<Registration>> {
        client.registrations(
            &self.home,
            self.cwd.as_deref(),
            name,
            CodexLookup::Discover,
            deadline,
        )
    }

    fn all(
        &self,
        client: Client,
        deadline: Instant,
    ) -> ClientResult<Vec<(&'static str, Vec<Registration>)>> {
        NAMES
            .into_iter()
            .map(|name| {
                self.registrations(client, name, deadline)
                    .map(|entries| (name, entries))
            })
            .collect()
    }

    fn list(&self, target: Option<&Target>) -> Value {
        let clients = CLIENTS.map(|client| self.list_client(client, target));
        json!({"command": target.map(Target::reported_argv), "clients": clients})
    }

    fn list_client(&self, client: Client, target: Option<&Target>) -> Value {
        let via = client.detected(&self.home);
        let mut row = json!({
            "client": client.name(), "detected": via.is_some(), "via": via, "registered": false,
            "current": false, "registeredCommand": null, "scope": null,
        });
        let mut registrations = Vec::new();
        match self.all(client, Instant::now() + CLIENT_TIMEOUT) {
            Ok(found) => {
                for (name, entries) in found {
                    for (index, entry) in entries.iter().enumerate() {
                        let mut info = entry.report(target);
                        let current = target.is_some_and(|target| entry.matches(target));
                        if registrations.is_empty() {
                            row["registered"] = json!(true);
                            row["scope"] = json!(entry.scope.name());
                            row["current"] = json!(current);
                            row["registeredCommand"] = info["command"].clone();
                            for key in ["args", "staleEnv", "extraSettings", "disabled"] {
                                if let Some(value) = info.get(key) {
                                    row[key] = value.clone();
                                }
                            }
                            if name != NAMES[0] {
                                row["registeredName"] = json!(name);
                            }
                        }
                        info["scope"] = json!(entry.scope.name());
                        info["current"] = json!(current);
                        info["effective"] = json!(index == 0);
                        if name != NAMES[0] {
                            info["name"] = json!(name);
                        }
                        registrations.push(info);
                    }
                }
            }
            Err(error) => row["error"] = json!(error),
        }
        row["registrations"] = json!(registrations);
        row
    }

    fn verify(
        &self,
        client: Client,
        scope: Scope,
        name: &str,
        target: &Target,
        deadline: Instant,
    ) -> ClientResult<()> {
        let entries = client.registrations(
            &self.home,
            self.cwd.as_deref(),
            name,
            CodexLookup::Verify,
            deadline,
        )?;
        let matches = |entry: &Registration| entry.matches(target);
        if !entries
            .iter()
            .any(|entry| entry.scope == scope && matches(entry))
        {
            return Err(format!("{} does not show the new sai entry", client.name()));
        }
        if client == Client::Codex
            && !client::codex_get(
                self.cwd(),
                Scope::Project,
                name,
                CodexLookup::Verify,
                deadline,
            )?
            .as_ref()
            .is_some_and(matches)
        {
            return Err(CODEX_STILL_ACTIVE.into());
        }
        Ok(())
    }
}

fn after_options(mut args: &[String], root: bool) -> Option<&[String]> {
    while let Some(arg) = args.first() {
        if arg == "--" {
            return Some(&args[1..]);
        }
        if root && arg == "--json" {
            args = &args[1..];
            continue;
        }
        let (flag, inline) = arg
            .split_once('=')
            .map_or((arg.as_str(), None), |(flag, value)| (flag, Some(value)));
        let key = KEYS.into_iter().find(|key| key.spec().flag == Some(flag));
        if key.is_none() && !(root && flag == "--as") {
            break;
        }
        let value = match inline {
            Some(value) => {
                args = &args[1..];
                value
            }
            None => {
                let value = args.get(1)?;
                args = &args[2..];
                value
            }
        };
        if let Some(key) = key {
            key.check_diagnostic_url(value).ok()?;
        }
    }
    Some(args)
}

fn serves_mcp(args: &[String]) -> bool {
    let Some(args) = after_options(args, true).and_then(|args| args.strip_prefix(&["mcp".into()]))
    else {
        return false;
    };
    let args = if args.first().is_some_and(|arg| arg == "--") {
        &args[1..]
    } else {
        args
    };
    args.first().is_some_and(|arg| arg == "serve")
        && after_options(&args[1..], false).is_some_and(<[_]>::is_empty)
}

struct Stale {
    reason: &'static str,
    args: Option<Vec<String>>,
}

fn stale(reg: &Registration, home: &Path) -> Option<Stale> {
    let Some((command, args)) = reg.argv.split_first() else {
        return Some(Stale {
            reason: "Sai registration has no executable command",
            args: None,
        });
    };
    let binary = basename(command).trim_end_matches(".exe");
    let serve = serves_mcp(args);
    if binary == NAMES[1] {
        let replacement = if serve {
            args.to_vec()
        } else {
            prepend_serve(args)
        };
        return Some(Stale {
            reason: "Sai registration uses the retired executable",
            args: serves_mcp(&replacement).then_some(replacement),
        });
    }
    let on_path = which(command);
    let reason = if on_path.is_none() {
        "Sai registration points to a missing command"
    } else if binary == NAMES[0] && !serve {
        "Sai registration does not run `sai mcp serve`"
    } else if binary == NAMES[0] && in_uv_cache(on_path.as_deref().expect("command checked"), home)
    {
        "Sai registration points inside the uv cache"
    } else if binary == NAMES[0] && !Path::new(command).is_absolute() {
        "Sai registration uses a relative command"
    } else {
        return None;
    };
    Some(Stale {
        reason,
        args: (binary == NAMES[0] && serve).then(|| args.to_vec()),
    })
}

fn prepend_serve(args: &[String]) -> Vec<String> {
    ["mcp".into(), "serve".into()]
        .into_iter()
        .chain(args.iter().cloned())
        .collect()
}

fn claude_args(reg: &Registration, issue: &Stale) -> Option<Vec<String>> {
    if let Some(args) = &issue.args {
        return Some(args.clone());
    }
    let (_, args) = reg.argv.split_first()?;
    let replacement = if serves_mcp(args) {
        args.to_vec()
    } else {
        prepend_serve(args)
    };
    serves_mcp(&replacement).then_some(replacement)
}

#[derive(Debug)]
pub(crate) struct RegistrationCheck {
    pub(crate) client: &'static str,
    pub(crate) status: &'static str,
    pub(crate) summary: String,
    pub(crate) fix: Option<String>,
    pub(crate) changes: Vec<String>,
    pinned: bool,
    legacy_name: bool,
    manual_repairs: Vec<Value>,
}

impl RegistrationCheck {
    fn new(
        client: Client,
        status: &'static str,
        summary: impl Into<String>,
        fix: impl Into<Option<String>>,
    ) -> Self {
        Self {
            client: client.name(),
            status,
            summary: summary.into(),
            fix: fix.into(),
            changes: Vec::new(),
            pinned: false,
            legacy_name: false,
            manual_repairs: Vec::new(),
        }
    }
}

pub(crate) fn check_registrations(
    fix: bool,
    timeout: Duration,
    profile_override: Option<&OsStr>,
) -> ClientResult<Vec<RegistrationCheck>> {
    let clients = Clients::open()?;
    let target = doctor_target(&clients.home);
    let expected_profile = match profile_override {
        Some(value) => profile_value(&clients.home, value),
        None => profile(&clients.home),
    };
    Ok(clients.check_all(target.as_ref(), fix, Some(&expected_profile), timeout))
}

impl Clients {
    fn check_all(
        &self,
        target: Option<&Target>,
        fix: bool,
        expected_profile: Option<&Option<String>>,
        timeout: Duration,
    ) -> Vec<RegistrationCheck> {
        let deadline = Instant::now() + timeout;
        CLIENTS
            .into_iter()
            .filter(|client| client.detected(&self.home).is_some())
            .map(|client| self.check(client, target, expected_profile, fix, deadline))
            .collect()
    }

    fn check(
        &self,
        client: Client,
        target: Option<&Target>,
        expected_profile: Option<&Option<String>>,
        fix: bool,
        deadline: Instant,
    ) -> RegistrationCheck {
        let install = format!("Run `sai mcp install {}`", client.name());
        let check = |status, summary: &str, hint: Option<String>| {
            RegistrationCheck::new(client, status, summary, hint)
        };
        if client == Client::Codex && client.executable().is_none() {
            return check(
                "fail",
                "Could not inspect Sai registration",
                Some("Put `codex` on PATH".into()),
            );
        }
        let Ok(found) = self.all(client, deadline) else {
            return check(
                "fail",
                "Could not read the client's Sai registrations",
                Some(install),
            );
        };
        let inspected = found
            .iter()
            .flat_map(|(name, entries)| {
                entries
                    .iter()
                    .map(|entry| (*name, entry, stale(entry, &self.home)))
            })
            .collect::<Vec<_>>();
        if inspected.is_empty() {
            return check("warn", "Sai is not registered", Some(install));
        }
        let legacy = inspected.iter().any(|(name, _, _)| *name == NAMES[1]);
        let legacy_warning = format!("Sai retains its existing {RETIRED_NAME} registration name");
        let issues = inspected
            .iter()
            .filter_map(|(name, entry, issue)| issue.as_ref().map(|issue| (*name, *entry, issue)))
            .collect::<Vec<_>>();
        if client == Client::Claude && !issues.is_empty() {
            let mut result = claude_check(&issues);
            result.legacy_name = legacy;
            return result;
        }
        let different_profile = expected_profile.is_some_and(|expected_profile| {
            inspected
                .iter()
                .any(|(_, entry, _)| !entry.matches_profile(expected_profile))
        });
        let pinned = target.is_some_and(|target| {
            inspected.iter().any(|(_, entry, issue)| {
                issue.is_none()
                    && entry.argv.first().is_some_and(|command| {
                        let path = Path::new(command);
                        path.is_absolute() && resolved(path) != resolved(Path::new(&target.argv[0]))
                    })
            })
        });
        let mut changes = Vec::new();
        let mut failure = None;
        for (name, entry, issue) in issues {
            match self.repair(client, name, entry, issue, target, fix, deadline) {
                Ok(()) => changes.push(format!(
                    "Repaired the Sai registration in {} scope",
                    entry.scope.name()
                )),
                Err(problem) => {
                    failure.get_or_insert(problem);
                }
            }
        }
        if client == Client::Codex && !changes.is_empty() && failure.is_none() {
            match self.all(client, deadline) {
                Ok(found)
                    if found
                        .iter()
                        .flat_map(|(_, entries)| entries)
                        .any(|entry| stale(entry, &self.home).is_some()) =>
                {
                    failure = Some((
                        "A stale Codex project registration is still active".into(),
                        "Edit the Sai registration in .codex/config.toml".into(),
                    ));
                }
                Err(_) => {
                    failure = Some((
                        "Could not verify the Codex registration".into(),
                        "Check the Sai registration and rerun `sai doctor`".into(),
                    ))
                }
                Ok(_) => {}
            }
        }
        let mut result = if let Some((mut summary, hint)) = failure {
            if legacy {
                summary.push_str(". ");
                summary.push_str(&legacy_warning);
            }
            check("fail", &summary, Some(hint))
        } else if pinned || different_profile || legacy {
            let warnings = [
                pinned.then_some("Sai uses a pinned launcher from a different install"),
                different_profile.then_some("Sai registration has a different SAI_HOME setting"),
                legacy.then_some(legacy_warning.as_str()),
            ];
            check(
                "warn",
                &warnings
                    .into_iter()
                    .flatten()
                    .collect::<Vec<_>>()
                    .join(". "),
                different_profile.then(|| "Check SAI_HOME in the client registration".into()),
            )
        } else if changes.is_empty() {
            check("pass", "Sai registration is current", None)
        } else {
            check("fixed", "Sai registration repaired", None)
        };
        result.pinned = result.status == "warn" && pinned;
        result.legacy_name = legacy;
        result.changes = changes;
        result
    }

    #[allow(clippy::too_many_arguments)]
    fn repair(
        &self,
        client: Client,
        name: &str,
        entry: &Registration,
        issue: &Stale,
        target: Option<&Target>,
        fix: bool,
        deadline: Instant,
    ) -> Result<(), (String, String)> {
        let Some(args) = &issue.args else {
            return Err((
                issue.reason.into(),
                "Edit the Sai registration in this client's config".into(),
            ));
        };
        if client == Client::Codex && entry.scope == Scope::Project {
            return Err((
                issue.reason.into(),
                "Edit the Sai registration in this client's config".into(),
            ));
        }
        let Some(target) = target else {
            return Err((
                issue.reason.into(),
                "Install a stable Sai launcher on PATH".into(),
            ));
        };
        if !fix {
            return Err((
                issue.reason.into(),
                "Run `sai doctor` with fixes enabled".into(),
            ));
        }
        let target = target.repair(args.clone(), client == Client::Codex);
        let problem = |error: String, hint: Option<String>| {
            if error == client::READ_ONLY_CONFIG {
                return (
                    format!("{}. {error}", issue.reason),
                    "Edit the Sai registration in this client's config".into(),
                );
            }
            let mut summary = format!(
                "{}. Could not repair the {} registration",
                issue.reason,
                entry.scope.name()
            );
            if hint.is_some() {
                summary.push_str(&format!(". {error}"));
            }
            (
                summary,
                hint.unwrap_or_else(|| {
                    "Check this client's Sai registration and rerun `sai doctor`".into()
                }),
            )
        };
        if client == Client::Codex {
            Client::repair_codex(&self.home, self.cwd(), name, &target, entry, deadline)
                .map_err(|(error, hint)| problem(error, hint))
        } else {
            Client::repair_cursor(&self.home, self.cwd(), name, &target, entry, deadline)
                .map_err(|error| problem(error, None))
        }
    }
}

fn claude_check(issues: &[(&str, &Registration, &Stale)]) -> RegistrationCheck {
    let summary = issues
        .iter()
        .map(|(name, entry, issue)| format!("{} {name}: {}", entry.scope.name(), issue.reason))
        .collect::<Vec<_>>()
        .join(". ");
    let mut commands = Vec::new();
    let mut manual_repairs = Vec::new();
    let mut reset = false;
    let mut project = false;
    for (name, entry, issue) in issues.iter().filter(|(_, entry, _)| entry.stdio) {
        let needs_reset = claude_args(entry, issue).is_none();
        let mut repair = json!({"scope": entry.scope.name(), "name": name});
        if needs_reset {
            repair["resetArgs"] = json!(true);
        }
        manual_repairs.push(repair);
        reset |= needs_reset;
        project |= entry.scope != Scope::User;
        let flag = if needs_reset { " --reset-args" } else { "" };
        commands.push(format!(
            "`sai mcp install --client claude --scope {} --repair-existing {name}{flag}`",
            entry.scope.name()
        ));
    }
    let mut hint = if commands.is_empty() {
        String::new()
    } else {
        format!("Run {}.", commands.join(" and "))
    };
    if project {
        hint.push_str(" Run project and local commands from the project directory.");
    }
    if reset {
        hint.push_str(" The --reset-args flag replaces unsupported arguments with the defaults.");
    }
    if commands.len() < issues.len() {
        if !hint.is_empty() {
            hint.push(' ');
        }
        hint.push_str("Review the remote Sai registration in Claude Code.");
    }
    let mut result = RegistrationCheck::new(Client::Claude, "fail", summary, Some(hint));
    result.manual_repairs = manual_repairs;
    result
}

fn private_server_args(args: &[String], placeholder: &str, attached: &str) -> Vec<String> {
    let mut expects_value = false;
    args.iter()
        .map(|arg| {
            if std::mem::take(&mut expects_value) {
                return placeholder.to_owned();
            }
            let flag = arg.split_once('=').map_or(arg.as_str(), |(flag, _)| flag);
            if matches!(
                flag,
                "--endpoint" | "--ws-endpoint" | "--lcd-endpoint" | "--as"
            ) {
                if flag.len() == arg.len() {
                    expects_value = true;
                    arg.clone()
                } else {
                    format!("{flag}={attached}")
                }
            } else if matches!(arg.as_str(), "mcp" | "serve" | "--json" | "--") {
                arg.clone()
            } else {
                placeholder.to_owned()
            }
        })
        .collect()
}

impl McpArgs {
    pub(crate) fn run(
        self,
        flags: Flags,
        env: &Environment,
        json: bool,
        explicit_separator: bool,
        writer: Option<Arc<LogWriter>>,
    ) -> Result<(), SaiError> {
        let open_clients =
            || Clients::open().map_err(|err| SaiError::new(Code::InternalError, err));
        match self.command {
            Some(McpCommand::Serve(args)) => args.run(flags, env, writer),
            Some(McpCommand::List) => {
                let clients = open_clients()?;
                let target = reported_target(Vec::new(), &clients.home)
                    .map_err(|err| eprintln!("{err}"))
                    .ok();
                crate::local::emit(&clients.list(target.as_ref()), json)
            }
            Some(McpCommand::Install(args)) => args.run(&open_clients()?, json, explicit_separator),
            Some(McpCommand::Uninstall(args)) => args.run(&open_clients()?, json),
            None => {
                open_clients()?;
                use clap::CommandFactory;
                let mut command = crate::Cli::command();
                let mut mcp = command
                    .find_subcommand_mut("mcp")
                    .expect("mcp command registered")
                    .clone()
                    .bin_name("sai mcp");
                print!("{}", mcp.render_long_help());
                Ok(())
            }
        }
    }
}

fn chosen(names: impl IntoIterator<Item = String>) -> Result<Vec<Client>, SaiError> {
    let mut selected = Vec::new();
    for name in names {
        let client = match name.trim().to_ascii_lowercase().as_str() {
            "claude" | "claude-code" | "claude_code" => Client::Claude,
            "codex" => Client::Codex,
            "cursor" => Client::Cursor,
            _ => {
                return Err(SaiError::usage(
                    "Invalid client: choose from claude, codex, cursor.",
                ));
            }
        };
        if !selected.contains(&client) {
            selected.push(client);
        }
    }
    Ok(selected)
}

fn interactive() -> bool {
    use std::io::{self, IsTerminal};

    io::stdin().is_terminal() && io::stdout().is_terminal()
}

fn pick_clients(prompt: &str, clients: Vec<Client>) -> Result<Vec<Client>, SaiError> {
    use std::io::{self, Write};

    for (index, client) in clients.iter().enumerate() {
        eprintln!("  {}. {}", index + 1, client.name());
    }
    eprint!("{prompt} [all]: ");
    let _ = io::stderr().flush();
    let mut answer = String::new();
    if !io::stdin()
        .read_line(&mut answer)
        .is_ok_and(|size| size != 0)
    {
        return Err(SaiError::new(Code::InternalError, "Aborted!"));
    }
    if answer.trim().is_empty() || answer.trim().eq_ignore_ascii_case("all") {
        return Ok(clients);
    }
    chosen(
        answer
            .split(|c: char| c.is_whitespace() || c == ',')
            .filter(|name| !name.is_empty())
            .map(|name| {
                name.parse::<usize>()
                    .ok()
                    .and_then(|index| index.checked_sub(1))
                    .and_then(|index| clients.get(index))
                    .map_or(name, |client| client.name())
                    .to_owned()
            }),
    )
}

impl InstallArgs {
    fn run(
        self,
        clients: &Clients,
        json_output: bool,
        explicit_separator: bool,
    ) -> Result<(), SaiError> {
        if explicit_separator && self.server_argv.is_empty() {
            return Err(SaiError::usage("Give the server command after `--`."));
        }
        let mut names = chosen(self.clients.into_iter().chain(self.client_options))?;
        if self.reset_args && self.repair_existing.is_none() {
            return Err(SaiError::usage("--reset-args requires --repair-existing."));
        }
        if self.repair_existing.is_some()
            && (names != [Client::Claude] || self.all_clients || !self.server_argv.is_empty())
        {
            return Err(SaiError::usage(
                "--repair-existing requires --client claude and the default server command.",
            ));
        }
        let target = reported_target(self.server_argv, &clients.home).map_err(|err| {
            let mut error = SaiError::new(Code::InternalError, err);
            error.exit_code = ExitCode::Unsupported;
            error.hint = Some("sai mcp install --help".into());
            error
        })?;
        if let Some(name) = self.repair_existing {
            let result =
                clients.explicit_claude(self.scope, &name, &target, self.dry_run, self.reset_args);
            return finish(vec![result], json_output);
        }
        let detected = CLIENTS
            .into_iter()
            .filter(|client| client.detected(&clients.home).is_some())
            .collect::<Vec<_>>();
        if self.all_clients {
            for client in &detected {
                if !names.contains(client) {
                    names.push(*client);
                }
            }
        }
        if names.is_empty() {
            if !interactive() {
                crate::local::emit(&clients.list(Some(&target)), json_output)?;
                eprintln!(
                    "Nothing changed. Name clients, as in `sai mcp install claude`, or pass --all."
                );
                return Ok(());
            }
            if detected.is_empty() {
                return Err(SaiError::usage(
                    "No agent clients found; name one to register.",
                ));
            }
            names = pick_clients("Register Sai with (numbers, names, or all)", detected)?;
        }
        if self.scope == Scope::Local && names != [Client::Claude] {
            return Err(SaiError::usage(
                "Install scope local requires the Claude Code client.",
            ));
        }
        let results = names
            .into_iter()
            .map(|client| clients.install(client, self.scope, &target, self.dry_run))
            .collect();
        finish(results, json_output)
    }
}

struct RegistrationResult {
    report: Value,
    changed: bool,
}

impl RegistrationResult {
    fn new(client: Client, scope: impl Into<Option<Scope>>) -> Self {
        let mut report = json!({"client": client.name(), "action": "failed"});
        if let Some(scope) = scope.into() {
            report["scope"] = json!(scope.name());
        }
        Self {
            report,
            changed: false,
        }
    }

    fn failed(&mut self, error: String) {
        self.report["action"] = json!("failed");
        self.report["error"] = json!(error);
    }

    fn planned(&mut self, action: &str) {
        self.report["action"] = json!("planned");
        self.report["would"] = json!(action);
    }
}

impl Clients {
    fn explicit_claude(
        &self,
        scope: Scope,
        name: &str,
        target: &Target,
        dry: bool,
        reset: bool,
    ) -> RegistrationResult {
        let client = Client::Claude;
        let mut result = RegistrationResult::new(client, scope);
        result.report["name"] = json!(name);
        let work = || -> ClientResult<Option<Target>> {
            let deadline = Instant::now() + CLIENT_TIMEOUT;
            let mut entries = self.registrations(client, name, deadline)?;
            let entry = entries
                .drain(..)
                .find(|entry| entry.scope == scope)
                .ok_or_else(|| {
                    format!("No existing {name} registration in {} scope", scope.name())
                })?;
            if !entry.stdio {
                return Err("The existing Claude Code entry uses a remote transport".into());
            }
            let Some(issue) = stale(&entry, &self.home) else {
                return Ok(None);
            };
            let args = if reset {
                Some(target.argv[1..].to_vec())
            } else {
                claude_args(&entry, &issue)
            };
            let args = args.ok_or(
                "Unsupported server arguments. Use --reset-args to replace them with the defaults.",
            )?;
            let replacement = target.repair(args, false);
            if !dry {
                client.write(
                    scope,
                    &self.home,
                    self.cwd(),
                    name,
                    &replacement,
                    Some(&entry),
                    deadline,
                )?;
            }
            Ok(Some(replacement))
        };
        match work() {
            Ok(None) => result.report["action"] = json!("unchanged"),
            Ok(Some(_)) if dry => result.planned("replaced"),
            Ok(Some(_)) => {
                result.report["action"] = json!("replaced");
                result.changed = true;
            }
            Err(error) => result.failed(error),
        }
        result
    }

    fn install(
        &self,
        client: Client,
        scope: Scope,
        target: &Target,
        dry: bool,
    ) -> RegistrationResult {
        let mut result = RegistrationResult::new(client, scope);
        result.report["command"] = json!(target.reported_argv());
        if let Err(error) = self.install_one(client, scope, target, dry, &mut result) {
            result.failed(error);
        }
        result
    }

    fn install_one(
        &self,
        client: Client,
        scope: Scope,
        target: &Target,
        dry: bool,
        result: &mut RegistrationResult,
    ) -> ClientResult<()> {
        let launcher = &target.argv[0];
        if target.explicit() && Path::new(launcher).is_absolute() && which(launcher).is_none() {
            return Err("The requested server launcher is missing or not executable".into());
        }
        if client == Client::Codex {
            if scope != Scope::User {
                return Err("Codex registers MCP servers per user only".into());
            }
            if client.executable().is_none() {
                return Err("`codex` is not on PATH".into());
            }
        }
        let deadline = Instant::now() + CLIENT_TIMEOUT;
        let entries = self.registrations(client, NAMES[0], deadline)?;
        let existing = entries.iter().find(|entry| entry.scope == scope);
        if existing.is_none()
            && let Some(legacy) = self.registrations(client, NAMES[1], deadline)?.first()
        {
            *result = self.legacy_install(client, legacy, target);
            return Ok(());
        }
        if client == Client::Claude && existing.is_some_and(|entry| entry.disabled) {
            result
                .report
                .as_object_mut()
                .expect("result object")
                .remove("command");
            return Err(format!(
                "Claude Code has disabled the {} sai registration. Review MCP approvals and settings in the client.",
                scope.name()
            ));
        }
        if let Some(winner) = entries
            .first()
            .filter(|entry| entry.scope.rank() < scope.rank() && !entry.matches(target))
        {
            if client == Client::Codex {
                return Err(CODEX_PROJECT_ENTRY.into());
            }
            result.report["shadowedBy"] = json!(winner.scope.name());
        }
        if existing.is_some_and(|entry| entry.matches(target)) {
            result.report["action"] = json!("unchanged");
            return Ok(());
        }
        if client == Client::Claude
            && let Some(entry) = existing
        {
            self.report_different_claude(entry, target, result);
            return Ok(());
        }
        let action = if existing.is_some() {
            "replaced"
        } else {
            "added"
        };
        let mut step = None;
        if client == Client::Claude {
            let exe = client
                .executable()
                .ok_or("`claude` is not on PATH")?
                .to_string_lossy()
                .into_owned();
            let mut add = vec![
                exe,
                "mcp".into(),
                "add".into(),
                "--scope".into(),
                scope.name().into(),
                NAMES[0].into(),
            ];
            let mut reported_add = add.clone();
            if let Some(profile) = &target.profile {
                add.push(format!("--env=SAI_HOME={profile}"));
                reported_add.push("--env=SAI_HOME=[redacted]".into());
            }
            add.push("--".into());
            add.extend(target.argv.iter().cloned());
            reported_add.push("--".into());
            reported_add.extend(target.reported_argv());
            result.report["argv"] = json!([reported_add]);
            step = Some(add);
        } else {
            result.report["path"] = json!(client.path(scope, &self.home, self.cwd()));
        }
        if dry {
            result.planned(action);
            return Ok(());
        }
        if let Some(step) = step {
            client::run_step(&step, self.cwd(), deadline)?;
        } else {
            client.write(
                scope,
                &self.home,
                self.cwd(),
                NAMES[0],
                target,
                None,
                deadline,
            )?;
        }
        result.changed = true;
        self.verify(client, scope, NAMES[0], target, deadline)?;
        result.report["action"] = json!(action);
        Ok(())
    }

    fn report_different_claude(
        &self,
        entry: &Registration,
        target: &Target,
        result: &mut RegistrationResult,
    ) {
        let scope = entry.scope.name();
        let path = absolute(&Client::Claude.path(entry.scope, &self.home, self.cwd()));
        let mut fields = Vec::new();
        if entry.argv.first().is_none_or(|command| {
            normalize_launcher(command) != normalize_launcher(&target.argv[0])
        }) {
            fields.push("command");
        }
        let args = entry.argv.get(1..).unwrap_or_default();
        if args != &target.argv[1..] {
            fields.push("args");
        }
        if !entry.stale_env.is_empty() || !entry.matches_profile(&target.profile) {
            fields.push("env");
        }
        fields.extend(&entry.extra);
        let label = format!("{scope} sai entry");
        let mut fix = format!("The {label} in {} differs", path.display());
        if !fields.is_empty() {
            fix.push_str(&format!(" in {}", fields.join(", ")));
        }
        fix.push('.');

        if !entry.stdio {
            let transport = entry.raw["type"]
                .as_str()
                .filter(|kind| matches!(*kind, "http" | "sse" | "streamable-http"))
                .unwrap_or("remote");
            let endpoint = entry.raw["url"]
                .as_str()
                .map(|url| format!(" at {}", sai_core::log::origin(url)))
                .unwrap_or_default();
            fix.push_str(&format!(
                " The saved transport is {transport}{endpoint}. Review the entry in Claude Code and keep its transport, URL, headers, and other settings."
            ));
        } else if !entry.extra.is_empty() {
            fix.push_str(" Edit the differing fields in Claude Code or its config. Keep the saved working directory and any custom settings you still need.");
        } else {
            Self::claude_replacement_hint(entry, target, args, &label, &mut fix);
        }
        result
            .report
            .as_object_mut()
            .expect("result object")
            .remove("command");
        result.report["path"] = json!(path);
        result.report["fix"] = json!(fix);
        result.failed(format!(
            "The existing {scope} Claude Code sai registration differs from the requested settings"
        ));
    }

    fn claude_replacement_hint(
        entry: &Registration,
        target: &Target,
        args: &[String],
        label: &str,
        fix: &mut String,
    ) {
        const ARG: &str = "<original-argument>";
        const PROFILE: &str = "<original-SAI_HOME>";
        let scope = entry.scope.name();
        let launcher = if target.explicit() {
            which("sai")
                .map(|path| absolute(&path))
                .or_else(|| env::current_exe().ok())
        } else {
            Some(PathBuf::from(&target.argv[0]))
        };
        if let Some(launcher) = launcher.filter(|path| path.is_absolute()) {
            let launcher = launcher.to_string_lossy();
            let profile = if entry.invalid_profile {
                None
            } else if entry.profile.is_none() || entry.profile == target.profile {
                target.profile.as_deref()
            } else {
                Some(PROFILE)
            };
            let prefixed = prepend_serve(args);
            let args = if !serves_mcp(args) && serves_mcp(&prefixed) {
                &prefixed
            } else {
                args
            };
            let args = private_server_args(args, ARG, ARG);
            let extra_env = entry.stale_env.len() + usize::from(entry.invalid_profile);
            if profile == Some(PROFILE)
                || extra_env != 0
                || args.iter().any(|arg| arg.contains(ARG))
            {
                fix.push_str("\nFill in the placeholders from the existing config before removing the entry. You can edit just the launcher in the config to keep those settings in place.");
            }
            let add = ["claude", "mcp", "add", "--scope", scope, NAMES[0]]
                .into_iter()
                .map(str::to_owned)
                .chain(profile.map(|profile| format!("--env=SAI_HOME={profile}")))
                .chain(std::iter::repeat_n(
                    "--env=<NAME>=<value>".to_owned(),
                    extra_env,
                ))
                .chain(["--".to_owned(), launcher.to_string()])
                .chain(args)
                .map(|arg| shell_quote(&arg))
                .collect::<Vec<_>>()
                .join(" ");
            let launcher = shell_quote(&launcher);
            fix.push_str(&format!(
                "\nRun `test -f {launcher} && test -x {launcher}` before removing the entry. Continue only if it succeeds.\nRemove the existing {label} in Claude from the project directory you normally use, then run `{add}`."
            ));
        } else {
            fix.push_str(" Run `sai doctor` for current replacement instructions.");
        }
        if target.explicit() {
            fix.push_str(" For a custom launcher, substitute your requested command after -- and use its executable in the check.");
        }
    }

    fn legacy_install(
        &self,
        client: Client,
        entry: &Registration,
        target: &Target,
    ) -> RegistrationResult {
        let name = NAMES[1];
        let mut result = RegistrationResult::new(client, entry.scope);
        let shown = entry.report(Some(target));
        result.report["name"] = json!(name);
        result.report["command"] = shown["command"].clone();
        if let Some(args) = shown.get("args") {
            result.report["args"] = args.clone();
        }
        if entry.disabled {
            result.failed(format!("The existing {name} registration is disabled in {}. Review MCP approvals and settings in the client.", client.name()));
        } else if let Some(issue) = stale(entry, &self.home) {
            result.failed(format!("The existing {name} registration needs repair. {}. Run `sai doctor` for repair instructions.", issue.reason));
        } else if target.explicit() && entry.argv != target.argv {
            result.failed(format!("Kept the existing {name} registration and skipped the requested server command. Edit the existing registration in the client to change it."));
        } else {
            result.report["action"] = json!("unchanged");
        }
        result
    }
}

fn finish(results: Vec<RegistrationResult>, json_output: bool) -> Result<(), SaiError> {
    let reconnect = results.iter().any(|result| result.changed);
    let mut summary = json!({
        "results": results.iter().map(|result| &result.report).collect::<Vec<_>>(),
        "reconnectNeeded": reconnect,
    });
    if reconnect {
        eprintln!("Restart or reconnect the changed clients to load the change.");
    }
    if results.iter().any(|r| {
        r.report["client"] == "cursor"
            && matches!(r.report["action"].as_str(), Some("added" | "replaced"))
    }) {
        eprintln!("You may need to enable sai in Cursor under Cursor Settings > MCP.");
    }
    if results.iter().all(|r| r.report["action"] != "failed") {
        return crate::local::emit(&summary, json_output);
    }
    let message = if json_output {
        let mut lines = results
            .iter()
            .map(|result| {
                let r = &result.report;
                let detail = r["error"]
                    .as_str()
                    .map_or_else(String::new, |err| format!(": {err}"));
                let client = r["client"].as_str().unwrap_or_default();
                let scope = r["scope"]
                    .as_str()
                    .map_or_else(String::new, |scope| format!(" ({scope})"));
                let action = r["action"].as_str().unwrap_or_default();
                format!("{client}{scope}: {action}{detail}")
            })
            .collect::<Vec<_>>();
        if reconnect {
            lines.push("Client registrations changed.".into());
        }
        lines.join("\n")
    } else {
        let mut text_summary = summary.clone();
        let mut hints = Vec::new();
        for row in text_summary["results"]
            .as_array_mut()
            .expect("JSON results")
        {
            if let Some(Value::String(fix)) =
                row.as_object_mut().expect("result object").remove("fix")
            {
                hints.push(fix);
            }
        }
        let mut text = serde_json::to_string_pretty(&text_summary).expect("JSON value");
        for fix in hints {
            text.push('\n');
            text.push_str(&fix);
        }
        text
    };
    for (row, result) in summary["results"]
        .as_array_mut()
        .expect("JSON results")
        .iter_mut()
        .zip(results)
    {
        row["changed"] = json!(result.changed);
    }
    let mut error = SaiError::new(Code::InternalError, message);
    error.hint = Some("sai mcp list".into());
    error.details = summary.as_object().cloned().map(Box::new);
    Err(error)
}

pub(crate) fn launchers_on_path() -> Vec<PathBuf> {
    let mut found = Vec::new();
    if let Some(path) = env::var_os("PATH") {
        for directory in env::split_paths(&path).filter(|dir| !dir.as_os_str().is_empty()) {
            let launcher = absolute(&directory).join("sai");
            if which(&launcher.to_string_lossy()).is_some() {
                let launcher = resolved(&launcher);
                if !found.contains(&launcher) {
                    found.push(launcher);
                }
            }
        }
    }
    found
}

pub(crate) fn checkout_state(path: &Path) -> Option<Value> {
    let run = |args: &[&str]| {
        let args = std::iter::once("git")
            .chain(args.iter().copied())
            .map(str::to_owned)
            .collect::<Vec<_>>();
        let (status, out, _) =
            client::invoke(&args, path, Instant::now() + Duration::from_secs(2), true).ok()?;
        status
            .success()
            .then(|| String::from_utf8_lossy(&out).trim().to_owned())
    };
    let commit = run(&["rev-parse", "HEAD"])?;
    let dirty = !run(&["status", "--porcelain", "--", "."])?.is_empty();
    Some(json!({"commit": commit, "dirty": dirty}))
}
