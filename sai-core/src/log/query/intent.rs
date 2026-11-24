use std::{collections::BTreeSet, path::Path};

use chrono::Days;
use serde_json::{Value, json};

use super::{
    Budget, Cursor, Position, Report, Row, Scan, hex, order, scan, text, timestamp, token,
};
use crate::{
    errors::{Code, SaiError},
    keeper::{self, Keeper},
    log::{self, EventContext},
    storage::Storage,
};

const EVIDENCE: &[&str] = &[
    "intent.reserved",
    "intent.status",
    "intent.submitted",
    "intent.keeper",
    "intent.wait.end",
    "intent.status_write_failed",
];

fn address(row: &Row) -> Option<String> {
    text(row, "addr")
        .or_else(|| text(row, "account"))
        .filter(|addr| keeper::is_evm_address(addr))
        .map(str::to_ascii_lowercase)
}

fn scope(row: &Row) -> Option<(String, &str)> {
    let identity = text(row, "endpointId")?;
    if !hex(identity.strip_prefix("h1_")?, 64) {
        return None;
    }
    Some((address(row)?, identity))
}

fn invocation(row: &Row) -> Option<(&str, &str, &str)> {
    let run = text(row, "runId")?;
    if let Some(call) = text(row, "callId") {
        return Some((run, "call", call));
    }
    if text(row, "surface") == Some("mcp") {
        return None;
    }
    Some((run, "command", text(row, "commandId")?))
}

fn event(row: &Row) -> &str {
    text(row, "event").unwrap_or_default()
}

fn has_intent(row: &Row, key: &str) -> bool {
    text(row, "intentKey") == Some(key) && text(row, "tool") != Some("logs_intent")
}

struct IntentContext {
    rows: Vec<(Position, Row)>,
    anchors: Vec<Row>,
    scan: Scan,
}

fn read_context(root: &Path, key: &str, cursor: &Cursor<'_>) -> IntentContext {
    let window = cursor.window();
    let anchor = |row: &Row| has_intent(row, key) && timestamp(row).is_some();
    let mut rows = Vec::new();
    let mut anchors = Vec::new();
    let mut scan = scan::page(
        root,
        Budget::RUN,
        window,
        |row| {
            if anchor(row) {
                anchors.push(row.clone());
            }
        },
        |position, row| {
            if timestamp(&row).is_some() {
                rows.push((position, row));
            }
            true
        },
    );

    // Replay the adjacent page's bounded scan to recover its attribution
    // evidence, including anchors just past a UTC day or scan budget boundary.
    if cursor.position.is_some() {
        scan::page(
            root,
            Budget::RUN,
            scan::Window {
                start: cursor.replay,
                ..window
            },
            |row| {
                if anchor(row) {
                    anchors.push(row.clone());
                }
            },
            |_, _| true,
        );
    }
    let following_days = [
        window.last_day,
        cursor.position.map(|position| position.day),
    ]
    .into_iter()
    .flatten()
    .filter_map(|day| day.checked_add_days(Days::new(1)))
    .collect::<BTreeSet<_>>();
    for day in following_days {
        let next = scan::prefix(root, day, anchor);
        anchors.extend(next.rows);
        scan.truncated |= next.truncated;
    }
    anchors.sort_by_cached_key(order);
    IntentContext {
        rows,
        anchors,
        scan,
    }
}

async fn lookup(endpoint: &str, key: &str, addr: &str) -> Result<Value, SaiError> {
    let variables = json!({"user": addr, "key": key});
    let result = Keeper::new(endpoint, EventContext::quiet())
        .request(
            keeper::INTENTS,
            variables.as_object().unwrap().clone(),
            None,
            keeper::TIMEOUT,
        )
        .await?;
    let rows = result
        .get("userIntents")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            SaiError::new(Code::IncompleteData, "Keeper did not return intent records")
        })?;
    let canonical = |value: &Value| value.as_str().and_then(keeper::canonical_account);
    Ok(rows
        .iter()
        .filter_map(Value::as_object)
        .find(|row| {
            text(row, "intentKey") == Some(key)
                && row.get("evmAddress").and_then(canonical).as_deref() == Some(addr)
                && row
                    .get("nibiAddress")
                    .filter(|value| !value.is_null())
                    .is_none_or(|value| canonical(value).as_deref() == Some(addr))
        })
        .map_or(Value::Null, keeper::log_intent_record))
}

pub async fn intent(
    home: &Path,
    key: &str,
    endpoint: Option<&str>,
    addr: Option<&str>,
    offline: bool,
    bound: Option<&str>,
) -> Result<Value, SaiError> {
    if !token(key) {
        return Err(SaiError::new(Code::InvalidRequest, "Invalid intent key."));
    }
    if addr.is_some_and(|addr| !keeper::is_evm_address(addr)) {
        return Err(SaiError::new(
            Code::InvalidRequest,
            "Pass a 0x-prefixed EVM address.",
        ));
    }
    let addr = addr.map(str::to_ascii_lowercase);
    let root = home.join("logs");
    let cursor = Cursor::parse(bound)?;
    let IntentContext {
        rows,
        anchors,
        mut scan,
    } = read_context(&root, key, &cursor);
    let endpoint_id =
        endpoint.map(|endpoint| log::endpoint_id(&root, endpoint).unwrap_or_default());
    let mut candidates = BTreeSet::new();
    let mut evidence = BTreeSet::new();
    for row in &anchors {
        if let Some(scope) = scope(row).filter(|(account, id)| {
            addr.as_ref().is_none_or(|addr| addr == account)
                && endpoint_id
                    .as_deref()
                    .is_none_or(|endpoint| endpoint == *id)
        }) {
            if EVIDENCE.contains(&event(row)) && row.get("found") != Some(&Value::Bool(false)) {
                evidence.insert(scope.clone());
            }
            candidates.insert(scope);
        }
    }
    let choices = if evidence.is_empty() {
        candidates
    } else {
        evidence
    };
    let ambiguous = choices.len() > 1;
    let chosen = if choices.len() == 1 {
        choices.into_iter().next()
    } else {
        None
    };
    let mut direct = Vec::new();
    if let Some(chosen) = &chosen {
        direct.extend(
            anchors
                .iter()
                .filter(|row| scope(row).as_ref() == Some(chosen)),
        );
    }
    let invocations: BTreeSet<_> = direct.iter().filter_map(|row| invocation(row)).collect();
    let conflicting: BTreeSet<_> = anchors
        .iter()
        .filter(|row| scope(row).is_some_and(|scope| Some(&scope) != chosen.as_ref()))
        .filter_map(invocation)
        .collect();
    let matches_event = |row: &Row| {
        let Some(chosen) = &chosen else {
            return false;
        };
        (has_intent(row, key) && scope(row).as_ref() == Some(chosen))
            || (invocation(row)
                .is_some_and(|call| invocations.contains(&call) && !conflicting.contains(&call))
                && row
                    .get("intentKey")
                    .is_none_or(|value| value.is_null() || value.as_str() == Some(key))
                && address(row).is_none_or(|addr| addr == chosen.0)
                && (row.get("endpoint").is_none_or(Value::is_null)
                    || text(row, "endpointId") == Some(chosen.1))
                && row
                    .get("endpointId")
                    .is_none_or(|value| value.is_null() || value.as_str() == Some(chosen.1)))
    };
    let mut value = json!({
        "intentKey": chosen.as_ref().map(|_| key), "events": [], "gaps": [], "keeper": null, "txHash": null
    });
    if ambiguous
        || anchors.iter().any(|row| {
            scope(row).is_none()
                && (row
                    .get("endpoint")
                    .is_some_and(|endpoint| !endpoint.is_null())
                    || EVIDENCE.contains(&event(row)))
        })
    {
        value["scopeWarning"] = json!(if ambiguous {
            "Intent events have ambiguous account or endpoint attribution."
        } else {
            "Older events without sufficient attribution were omitted."
        });
    }
    if let Some(endpoint) = endpoint.filter(|_| !offline) {
        let account = chosen
            .as_ref()
            .map(|(account, _)| account.clone())
            .or(addr)
            .or_else(|| {
                Storage::new(home)
                    .load_session(None)
                    .ok()
                    .filter(|session| session.endpoint() == Some(endpoint))
                    .map(|session| session.addr().to_ascii_lowercase())
            });
        if let Some(account) = account.filter(|_| !ambiguous) {
            match lookup(endpoint, key, &account).await {
                Ok(row) => value["keeper"] = row,
                Err(error) => {
                    value["keeperError"] = json!({
                        "code": error.code, "message": "Keeper intent lookup failed."
                    })
                }
            }
        } else {
            value["keeperError"] = json!({
                "code": Code::InvalidRequest,
                "message": if ambiguous {
                    "Several accounts match this key. Pass an address for the Keeper lookup."
                } else {
                    "Pass an address for the Keeper lookup or use offline mode."
                }
            });
        }
    }
    if let Some(key) = value["keeper"].get("intentKey").cloned() {
        value["intentKey"] = key;
    }
    let hash = ["intent.submitted", "intent.wait.end", "intent.keeper"]
        .into_iter()
        .find_map(|name| {
            direct
                .iter()
                .rev()
                .filter(|row| event(row) == name)
                .find_map(|row| row.get("txHash").and_then(keeper::request_hash))
        })
        .or_else(|| keeper::request_hash(&value["keeper"]["txHash"]));
    let scrub = log::scrubber(home);
    value["txHash"] = json!(
        hash.as_ref()
            .and_then(|hash| keeper::request_hash(&scrub(json!(hash).into())))
    );
    if !value["keeper"].is_null() {
        value["keeper"] = scrub(value["keeper"].clone().into());
    }

    let runs: BTreeSet<_> = direct.iter().filter_map(|row| text(row, "runId")).collect();
    let attributed_gap = |row: &Row| text(row, "runId").is_some_and(|run| runs.contains(run));
    let mut report = Report::new(value, scrub);
    let mut last = None;
    let mut stop = None;
    let mut unattributed = Vec::new();
    for (position, row) in &rows {
        let field = if event(row) == "log.dropped" {
            if !attributed_gap(row) {
                unattributed.push(row);
                continue;
            }
            "gaps"
        } else if matches_event(row) {
            "events"
        } else {
            continue;
        };
        if !report.add(field, row) {
            stop = Some(*position);
            break;
        }
        last = timestamp(row);
    }
    if !unattributed.is_empty() {
        report.value["unattributedGapCount"] = json!(unattributed.len());
        report.value["unattributedGaps"] = json!([]);
        scan.truncated |= unattributed.len() > 8;
        scan.truncated |= unattributed
            .into_iter()
            .take(8)
            .any(|row| !report.add("unattributedGaps", row));
    }
    let start = scan.start;
    let mut value = report.finish(scan, &cursor, last, stop, "", start);
    if value["truncated"] == true && value.get("nextBefore").is_none() {
        value["more"] = json!("Inspect the retained files from sai logs path.");
    }
    Ok(value)
}
