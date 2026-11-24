mod install;
mod release;
mod smoke;
mod state;

use std::{
    env,
    io::IsTerminal,
    os::unix::process::CommandExt,
    path::Path,
    process::{Command, Stdio},
    time::Duration,
};

use clap::Args;
use install::{Change, Install};
use release::{API, Releases, WEB, tag};
use sai_core::{
    errors::{Code, ExitCode, SaiError},
    log::{self, EventContext, Field, Level},
    settings::{Environment, Flags, Key, Settings, repr},
};
use semver::Version;
use serde_json::{Map, Value, json};

const RUNNING: &str = env!("CARGO_PKG_VERSION");
const LOCK_WAIT: Duration = Duration::from_secs(300);
const NO_RELEASE: &str = "No sai-cli release is published yet; nothing to update to.";

#[derive(Args)]
pub(crate) struct UpdateCommand {
    /// Report the latest version only.
    #[arg(long)]
    check: bool,
    /// Restore a pending uv installation using its verified rollback wheel.
    #[arg(long)]
    recover: bool,
    /// Install this release instead of the latest. Allows downgrades and prereleases.
    #[arg(long, value_name = "X.Y.Z")]
    to: Option<String>,
    #[arg(long, hide = true)]
    source: bool,
    #[arg(long, hide = true)]
    background: bool,
    #[arg(long, hide = true, default_value = API)]
    api_base: String,
    #[arg(long, hide = true, default_value = WEB)]
    web_base: String,
}

impl UpdateCommand {
    pub(crate) fn run(
        self,
        flags: &Flags,
        env: &Environment,
        json_output: bool,
        events: EventContext<'_>,
    ) -> Result<(), SaiError> {
        if self.check
            && (self.to.as_ref().is_some_and(|to| !to.is_empty()) || self.source || self.background)
        {
            return Err(SaiError::usage("--check takes no other update option."));
        }
        if self.recover && (self.check || self.to.is_some() || self.source || self.background) {
            return Err(SaiError::usage("--recover takes no other update option."));
        }
        let mut env = env.clone();
        env.home = absolute_home(&env.home).map_err(|err| {
            SaiError::new(
                Code::InternalError,
                format!("Could not resolve SAI_HOME: {err}"),
            )
        })?;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| SaiError::internal())?;
        let install = if self.recover {
            runtime
                .block_on(Install::discover_recovery(&self.web_base))
                .map_err(|message| SaiError {
                    exit_code: ExitCode::Unsupported,
                    ..SaiError::new(Code::InternalError, message)
                })?
        } else {
            Install::discover(&env.home, &self.web_base)
        };
        let releases = Releases::new(&self.api_base, &self.web_base);
        if self.check {
            return runtime.block_on(check(releases, &install, &env.home, json_output, events));
        }
        let allowed = || {
            !self.background
                || Settings::load(flags, &env)
                    .is_ok_and(|settings| settings.value(Key::UpdateAuto).as_bool() == Some(true))
        };
        let out = runtime.block_on(self.update(&install, releases, &env.home, allowed));
        let exit = out.exit();
        events.event(
            "update.result",
            if exit == ExitCode::Ok {
                Level::Info
            } else {
                Level::Warn
            },
            [
                ("status", json!(out.status).into()),
                ("installed", json!(out.installed).into()),
                ("target", json!(out.target).into()),
                ("rolledBack", json!(out.rolled_back).into()),
                ("background", json!(self.background).into()),
                ("message", Field::Untrusted(out.message.clone())),
                (
                    "code",
                    json!((exit != ExitCode::Ok).then_some(Code::InternalError)).into(),
                ),
            ],
        );
        if self.background {
            return Ok(());
        }
        if exit != ExitCode::Ok {
            return Err(SaiError {
                exit_code: exit,
                ..SaiError::new(Code::InternalError, out.message)
            });
        }
        if json_output {
            emit(&json!({
                "status": out.status,
                "message": out.message,
                "installed": out.installed,
                "target": out.target,
                "rolledBack": out.rolled_back,
            }));
        } else {
            println!("{}", out.message);
        }
        Ok(())
    }

    async fn update(
        &self,
        install: &Install,
        releases: Result<Releases, release::ReleaseError>,
        home: &Path,
        allowed: impl Fn() -> bool,
    ) -> Outcome {
        let Some(lock_path) = install.lock_path() else {
            return Outcome::new("unsupported", guidance(install), RUNNING, None);
        };
        let wait = if self.background {
            Duration::ZERO
        } else {
            LOCK_WAIT
        };
        let _lock = match state::lock(&lock_path, wait).await {
            Ok(Some(held)) => held,
            Ok(None) => {
                return Outcome::new(
                    if self.background { "busy" } else { "failed" },
                    "Another sai update is still running; try again shortly.",
                    RUNNING,
                    None,
                );
            }
            Err(error) => {
                return Outcome::new(
                    "failed",
                    format!("Could not lock the Sai installation: {error}"),
                    RUNNING,
                    None,
                );
            }
        };
        if self.recover
            && let Err(reason) = install.require_pending_uv_recovery()
        {
            return Outcome::new("unsupported", reason, RUNNING, None);
        }
        if let Err(reason) = install.owner(true) {
            return Outcome::new("unsupported", reason, RUNNING, None);
        }
        if self.source {
            return Outcome::new(
                "unsupported",
                "Use a source checkout to build sai from Git.",
                RUNNING,
                None,
            );
        }
        // An opted-out worker can still report an available release. Leave the
        // launcher and any pending transaction for an authorized update.
        let opted_in = allowed();
        let allowed = || opted_in && allowed();
        let version = if opted_in {
            install.recover_and_version(home, &allowed).await
        } else {
            install.version_without_recovery()
        };
        let installed = match version {
            Ok(version) => version,
            Err(reason) => {
                return Outcome::new(
                    "failed",
                    format!("Could not read or recover the installed sai version: {reason}"),
                    RUNNING,
                    None,
                );
            }
        };
        let before = installed.to_string();
        if self.recover {
            return Outcome {
                rolled_back: true,
                ..Outcome::new(
                    "recovered",
                    format!("Restored the uv launcher for sai {installed}."),
                    &before,
                    None,
                )
            };
        }
        if self.background {
            let _ = state::save(home, json!({"startedAt": state::now()})).await;
        }
        let failed_lookup = |error: release::ReleaseError| {
            Outcome::new(
                "failed",
                format!("update check failed: {error}."),
                &before,
                self.to.as_deref(),
            )
        };
        let releases = match releases {
            Ok(releases) => releases,
            Err(error) => return failed_lookup(error),
        };
        let target = if let Some(to) = &self.to {
            let Some(target) = release::version(to) else {
                return Outcome::new(
                    "failed",
                    format!("{} is not a version like 0.2.0.", repr(to)),
                    &before,
                    Some(to),
                );
            };
            target
        } else {
            match latest(&releases, home, false).await {
                Ok(Some(target)) => target,
                Ok(None) => {
                    return Outcome::new("no-release", NO_RELEASE, &before, None);
                }
                Err(error) => return failed_lookup(error),
            }
        };
        let selected = target.to_string();
        let to = Some(selected.as_str());
        if target == installed {
            return Outcome::new(
                "current",
                format!("sai {installed} is up to date."),
                &before,
                to,
            );
        }
        if self.to.is_none() && !is_newer(&target, &installed) {
            return Outcome::new(
                "current",
                format!(
                    "sai {installed} is newer than the latest release {target}; nothing to do. `sai update --to {target}` installs it anyway."
                ),
                &before,
                to,
            );
        }

        let available = || Outcome::new("available", available_message(&selected), &before, to);
        let mut out = if !allowed() {
            available()
        } else {
            let last = state::read(home);
            if self.background
                && last
                    .get("result")
                    .is_some_and(|result| result["status"] == "failed" && result["to"] == selected)
            {
                return Outcome::new(
                    "failed",
                    format!("The update to {target} already failed; run `sai update`."),
                    &before,
                    to,
                );
            }
            match install
                .change(&releases, &installed, &target, home, allowed)
                .await
            {
                Change::Updated => Outcome::new(
                    "updated",
                    format!(
                        "Updated sai from {installed} to {target}. Restart agent clients and any open `sai` shell so they load it."
                    ),
                    &selected,
                    to,
                ),
                Change::Available => available(),
                Change::Unsupported(reason) => {
                    return Outcome::new("unsupported", reason, &before, to);
                }
                Change::Failed {
                    reason,
                    rolled_back,
                } => Outcome {
                    rolled_back,
                    ..Outcome::new(
                        "failed",
                        format!("Could not update to {target}: {reason}"),
                        &before,
                        to,
                    )
                },
            }
        };
        out.message = log::redact(home, &out.message);
        out.save(home, &before, !self.background).await;
        out
    }
}

fn is_newer(latest: &Version, installed: &Version) -> bool {
    installed.pre.is_empty() && installed.build.is_empty() && latest > installed
}

async fn latest(
    releases: &Releases,
    home: &Path,
    warn: bool,
) -> Result<Option<Version>, release::ReleaseError> {
    let latest = releases.latest().await?;
    let changes = json!({"latest": latest.as_ref().map(tag), "checkedAt": state::stamp()});
    if state::save(home, changes).await.is_err() && warn {
        eprintln!("Warning: could not save the update check.");
    }
    Ok(latest)
}

fn guidance(install: &Install) -> &'static str {
    match install {
        Install::Other(..) => "This launcher is managed outside sai. Use its installer to update.",
        _ => "Run `sai update` to install it.",
    }
}

async fn check(
    releases: Result<Releases, release::ReleaseError>,
    install: &Install,
    home: &Path,
    json_output: bool,
    events: EventContext<'_>,
) -> Result<(), SaiError> {
    let release = match releases {
        Ok(releases) => latest(&releases, home, true)
            .await
            .map(|latest| (releases, latest)),
        Err(error) => Err(error),
    };
    let (releases, latest) = release.map_err(|error| {
        events.event(
            "update.check",
            Level::Warn,
            [
                ("ok", json!(false).into()),
                ("code", json!(Code::InternalError).into()),
                ("error", Field::Untrusted(error.to_string())),
            ],
        );
        SaiError::new(Code::InternalError, format!("update check failed: {error}"))
    })?;
    let installed = Version::parse(RUNNING).expect("Cargo package version is semantic");
    let newer = latest
        .as_ref()
        .is_some_and(|latest| is_newer(latest, &installed));
    let latest_version = latest.as_ref().map(Version::to_string);
    let kind = install.kind();
    events.event(
        "update.check",
        Level::Info,
        [
            ("ok", json!(true).into()),
            ("installed", json!(RUNNING).into()),
            ("latest", json!(latest_version).into()),
            ("updateAvailable", json!(newer).into()),
            ("installKind", json!(kind).into()),
        ],
    );
    if json_output {
        let (wheel, sums) = latest
            .as_ref()
            .map(|latest| {
                (
                    Some(
                        releases
                            .asset(
                                latest,
                                &release::wheel_name(latest)
                                    .expect("stable releases have wheel names"),
                            )
                            .to_string(),
                    ),
                    Some(releases.asset(latest, "SHA256SUMS").to_string()),
                )
            })
            .unwrap_or_default();
        emit(&json!({
            "installed": RUNNING,
            "latest": latest_version,
            "latestTag": latest.as_ref().map(tag),
            "wheelUrl": wheel,
            "sha256sumsUrl": sums,
            "updateAvailable": newer,
            "install": install.report(),
            "update": newer.then(|| json!({"note": guidance(install), "command": null})),
        }));
    } else {
        println!("installed  {RUNNING} ({kind})");
        println!("latest     {}", latest_version.as_deref().unwrap_or("none"));
        if latest.is_none() {
            println!("{NO_RELEASE}");
        } else if newer {
            println!("A newer version is available. {}", guidance(install));
        } else {
            println!("sai is up to date.");
        }
    }
    Ok(())
}

fn emit(value: &Value) {
    println!("{}", crate::local::ascii_json(&value.to_string()));
}

struct Outcome {
    status: &'static str,
    message: String,
    installed: String,
    target: Option<String>,
    rolled_back: bool,
}

impl Outcome {
    fn new(
        status: &'static str,
        message: impl Into<String>,
        installed: &str,
        target: Option<&str>,
    ) -> Self {
        Self {
            status,
            message: message.into(),
            installed: installed.into(),
            target: target.map(str::to_owned),
            rolled_back: false,
        }
    }

    fn exit(&self) -> ExitCode {
        match self.status {
            "failed" => ExitCode::Error,
            "unsupported" => ExitCode::Unsupported,
            _ => ExitCode::Ok,
        }
    }

    async fn save(&self, home: &Path, before: &str, seen: bool) {
        let mut changes = json!({"result": {
            "status": self.status,
            "from": before,
            "to": self.target,
            "at": state::stamp(),
            "error": (self.status == "failed").then_some(&self.message),
            "rolledBack": self.rolled_back,
        }});
        if seen && let Some(target) = &self.target {
            changes["notified"] = json!(format!("{}:{target}", self.status));
        }
        let _ = state::save(home, changes).await;
    }
}

fn available_message(target: &str) -> String {
    format!("sai {target} is available. Run `sai update` to install it.")
}

fn notice(state: &Map<String, Value>) -> Option<(String, String)> {
    let result = state.get("result")?;
    let status = result["status"].as_str()?;
    let target = result["to"].as_str()?;
    let version = release::version(target)?;
    let key = format!("{status}:{target}");
    if state.get("notified").and_then(Value::as_str) == Some(&key) {
        return None;
    }
    if status == "updated" {
        return Some((
            key,
            format!("sai updated itself to {target}. Restart agent clients so they load it."),
        ));
    }
    if !is_newer(&version, &release::version(RUNNING)?) {
        return None;
    }
    let message = match status {
        "available" => available_message(target),
        "failed" => format!(
            "sai could not update itself to {target}. Run `sai update` to retry, or `sai doctor` for the reason."
        ),
        _ => return None,
    };
    Some((key, message))
}

fn absolute_home(home: &Path) -> std::io::Result<std::path::PathBuf> {
    if home.as_os_str().is_empty() {
        return env::current_dir();
    }
    std::path::absolute(home)
}

/// Reserve a background check and print a pending notice on interactive commands.
pub(crate) fn tick(command: &str, json: bool, home: &Path, events: EventContext<'_>) {
    if json
        || matches!(
            command.split(' ').next(),
            Some("mcp" | "update" | "doctor" | "config" | "logs" | "skill")
        )
        || env::var_os("CI").is_some_and(|ci| !ci.is_empty())
        || !std::io::stdin().is_terminal()
        || !std::io::stderr().is_terminal()
    {
        return;
    }
    let Ok(home) = absolute_home(home) else {
        return;
    };
    let home = home.as_path();
    let install = Install::discover(home, WEB);
    let Some(launcher) = install.launcher().filter(|_| install.owner(false).is_ok()) else {
        return;
    };
    let Ok(Some(lock)) = state::try_state_lock(home) else {
        return;
    };
    let state = state::read(home);
    let mut changes = Map::new();
    if let Some((key, message)) = notice(&state) {
        eprintln!("{message}");
        changes.insert("notified".into(), json!(key));
    }
    let now = state::now();
    let due = state::due(&state, now);
    if due {
        changes.insert("scheduledAt".into(), json!(now));
    }
    if !changes.is_empty() && state::merge(home, changes).is_err() {
        return;
    }
    drop(lock);
    if due
        && Command::new(launcher)
            .args(["update", "--background"])
            .env("SAI_HOME", home)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()
            .is_ok()
    {
        events.event("update.scheduled", Level::Info, []);
    }
}
