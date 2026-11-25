use std::{env, path::Path, time::Duration};

use sai_core::log::{EventContext, Level};
use serde_json::{Map, Value, json};

use super::{Clients, RETIRED_NAME, Target, in_uv_cache, which};

const BACKGROUND: (&str, &str) = (
    "background",
    "Background updates check user MCP registrations. Run `sai doctor` in each project to check project registrations.",
);
const CWD_MISSING: (&str, &str) = (
    "cwd-missing",
    "The working directory is gone. Run `sai doctor` in a project to check its MCP registrations.",
);
const FAILED: (&str, &str) = (
    "failed",
    "Could not check MCP registrations. Run `sai doctor` for details.",
);

pub(crate) struct UpdateRepair {
    reports: Vec<Value>,
    notes: Vec<(&'static str, &'static str)>,
}

impl UpdateRepair {
    pub(crate) fn json_fields(&self) -> Map<String, Value> {
        self.fields(|(_, text)| text)
    }

    pub(crate) fn state_fields(&self) -> Map<String, Value> {
        self.fields(|(code, _)| code)
    }

    fn fields(
        &self,
        note: impl Fn((&'static str, &'static str)) -> &'static str,
    ) -> Map<String, Value> {
        let mut fields = Map::new();
        if !self.reports.is_empty() {
            fields.insert("mcp".into(), json!(self.reports));
        }
        if !self.notes.is_empty() {
            fields.insert(
                "mcpNotes".into(),
                json!(self.notes.iter().copied().map(note).collect::<Vec<_>>()),
            );
        }
        fields
    }
}

/// Check registrations after the installer has verified and committed the
/// new install. Use the installer's stable launcher, since the old binary may
/// still be running. Add the repair fields to the successful outcome and state.
pub(crate) fn repair_after_update(
    launcher: &Path,
    background: bool,
    events: EventContext<'_>,
) -> UpdateRepair {
    let mut notes = Vec::new();
    let cwd = if background {
        notes.push(BACKGROUND);
        None
    } else {
        env::current_dir().map_err(|_| notes.push(CWD_MISSING)).ok()
    };
    let mut reports = Vec::new();
    if let Some((clients, target)) = env::home_dir().and_then(|home| {
        let target = verified_target(launcher, &home)?;
        Some((Clients { home, cwd }, target))
    }) {
        for check in clients.check_all(Some(&target), true, None, Duration::from_secs(7)) {
            if !matches!(check.status, "fixed" | "fail") && !check.pinned && !check.legacy_name {
                continue;
            }
            let mut report = json!({
                "client": check.client,
                "status": check.status,
                "changes": check.changes,
            });
            if check.pinned {
                report["pinned"] = json!(true);
            }
            if check.legacy_name {
                report["legacyName"] = json!(RETIRED_NAME);
            }
            if !check.manual_repairs.is_empty() {
                report["manualRepairs"] = json!(check.manual_repairs);
                report["fix"] = json!(check.fix);
            }
            reports.push(report);
        }
    } else {
        notes.push(FAILED);
    }
    if !notes.is_empty() || !reports.is_empty() {
        let warn = notes.contains(&FAILED)
            || reports
                .iter()
                .any(|row| matches!(row["status"].as_str(), Some("warn" | "fail")));
        events.event(
            "update.mcp_repair",
            if warn { Level::Warn } else { Level::Info },
            [
                ("background", json!(background).into()),
                ("clients", json!(reports).into()),
                (
                    "notes",
                    json!(notes.iter().map(|(code, _)| code).collect::<Vec<_>>()).into(),
                ),
            ],
        );
    }
    UpdateRepair { reports, notes }
}

fn verified_target(launcher: &Path, home: &Path) -> Option<Target> {
    let command = launcher.to_str()?;
    if !launcher.is_absolute() || in_uv_cache(launcher, home) {
        return None;
    }
    which(command)?;
    Some(Target {
        argv: vec![command.into(), "mcp".into(), "serve".into()],
        profile: None,
        explicit: false,
    })
}

#[cfg(all(test, unix))]
mod tests;
