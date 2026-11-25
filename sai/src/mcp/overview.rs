use sai_core::{
    errors::{Code, SaiError},
    log::{EventContext, Surface},
    overview,
};
use serde_json::{Value, json};

use super::SaiMcp;

impl SaiMcp {
    pub(super) async fn account_overview(
        &self,
        addr: Option<&str>,
        call_id: &str,
    ) -> Result<Value, SaiError> {
        let settings = self.settings.clone();
        let home = self.env.home.clone();
        let writer = self.writer.clone();
        let addr = addr.map(str::to_owned);
        let call_id = call_id.to_owned();
        let runtime = tokio::runtime::Handle::current();
        let (_cancel_on_drop, cancel) = tokio::sync::oneshot::channel::<()>();
        Self::blocking_work(&self.overview_slots, move || {
            // Overview also reads the local ledger, which can wait on SQLite.
            let events = EventContext::new(writer.as_deref(), Surface::Mcp, Some("mcp serve"))
                .with_tool("account_overview", &call_id);
            let value = runtime
                .block_on(overview::read(
                    &settings,
                    &home,
                    addr.as_deref(),
                    events,
                    async {
                        let _ = cancel.await;
                        SaiError::internal()
                    },
                ))
                .map_err(|mut error| {
                    if error.code == Code::SessionUnavailable {
                        error.message.push_str(". Pass addr explicitly.");
                    }
                    error
                })?;
            Ok(release_response(value))
        })
        .await
    }
}

// MCP uses the v0.1.3 overview fields. The CLI still has a separate pinned
// transcript for the older shape.
fn release_response(mut report: Value) -> Value {
    if let Some(fetched) = report["balances"]["fetchedAt"].as_str().map(str::to_owned)
        && let Some(rows) = report["balances"]["data"].as_object_mut()
    {
        for row in rows.values_mut() {
            if !row["usd"].is_null() && row["sourceAt"].is_null() {
                row["sourceAt"] = json!(fetched);
                row["sourceAtBasis"] = json!("fetchedAt");
            }
        }
    }
    if report["prices"]["fetchedAt"].is_null() && report["prices"]["error"].is_null() {
        let reason = if report["positions"]["fetchedAt"].is_null() {
            "Positions and pending orders could not be loaded."
        } else {
            "No positions or pending orders were available to price."
        };
        report["prices"]["status"] = json!("skipped");
        report["prices"]["skipReason"] = json!(reason);
    }
    for section in report
        .as_object_mut()
        .expect("overview report")
        .values_mut()
    {
        if section["fetchedAt"].is_string() {
            section["sourceAt"] = section["fetchedAt"].clone();
        }
        clamp_ages(section);
    }
    report
}

fn clamp_ages(value: &mut Value) {
    match value {
        Value::Object(fields) => {
            if let Some(age) = fields.get_mut("sourceAgeSeconds")
                && age.as_f64().is_some_and(f64::is_sign_negative)
            {
                *age = json!(0.0);
            }
            fields.values_mut().for_each(clamp_ages);
        }
        Value::Array(items) => items.iter_mut().for_each(clamp_ages),
        _ => (),
    }
}
