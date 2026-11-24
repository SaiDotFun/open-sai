use std::{collections::BTreeSet, path::Path};

use chrono::Days;
use serde_json::{Value, json};

use super::{Budget, Report, Row, before, day, hex, order, scan, stamp, text, timestamp, token};
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

fn in_window(row: &Row, cutoff: Option<&str>) -> bool {
    cutoff.is_none_or(|cutoff| text(row, "ts").is_some_and(|ts| ts < cutoff))
}

fn is_start(row: &Row) -> bool {
    matches!(event(row), "command.start" | "mcp.tool_call")
}

struct IntentContext {
    rows: Vec<Row>,
    truncated: bool,
    cutoff: Option<String>,
    frontier: Option<String>,
}

fn read_context(root: &Path, key: &str, bound: Option<&str>) -> Result<IntentContext, SaiError> {
    let instant = before(bound)?;
    let page_day = instant.map(|instant| instant.date_naive());
    let cutoff = instant.map(stamp);
    let limit = cutoff.as_deref();
    let anchor = |row: &Row| has_intent(row, key);
    let mut read = scan::context(
        root,
        Budget::RUN,
        |day| page_day.is_none_or(|last| day <= last),
        &Default::default(),
        false,
        |row| in_window(row, limit) || anchor(row),
        limit,
    );
    read.rows.retain(|row| timestamp(row).is_some());
    let frontier = if read.truncated {
        read.frontier
    } else {
        read.rows
            .iter()
            .filter(|row| in_window(row, limit))
            .filter_map(timestamp)
            .min()
            .map(str::to_owned)
    };
    if let Some(following) = page_day.and_then(|day| day.checked_add_days(Days::new(1))) {
        let next = scan::context(
            root,
            Budget::CONTEXT,
            |day| day == following,
            &Default::default(),
            true,
            anchor,
            None,
        );
        read.rows.extend(next.rows);
        read.visited.extend(next.visited);
        read.truncated |= next.truncated;
    }
    let starts: BTreeSet<_> = read
        .rows
        .iter()
        .filter(|row| is_start(row))
        .filter_map(invocation)
        .collect();
    let mut missing = BTreeSet::new();
    let mut prior_days = BTreeSet::new();
    for row in read.rows.iter().filter(|row| anchor(row)) {
        if let Some(invocation) = invocation(row).filter(|invocation| !starts.contains(invocation))
            && let Some(day) = timestamp(row)
                .and_then(|ts| day(&ts[..10]))
                .and_then(|day| day.checked_sub_days(Days::new(1)))
                .filter(|day| *day >= chrono::NaiveDate::from_ymd_opt(1, 1, 1).unwrap())
        {
            missing.insert(invocation);
            prior_days.insert(day);
        }
    }
    if !missing.is_empty() {
        let previous = scan::context(
            root,
            Budget::CONTEXT,
            |day| prior_days.contains(&day),
            &read.visited,
            false,
            |row| is_start(row) && invocation(row).is_some_and(|call| missing.contains(&call)),
            None,
        );
        read.rows.extend(previous.rows);
        read.truncated |= previous.truncated;
    }
    read.rows.sort_by_cached_key(order);
    Ok(IntentContext {
        rows: read.rows,
        truncated: read.truncated,
        cutoff,
        frontier,
    })
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
    let IntentContext {
        rows,
        mut truncated,
        cutoff,
        frontier,
    } = read_context(&root, key, bound)?;
    let cutoff = cutoff.as_deref();
    let endpoint_id =
        endpoint.map(|endpoint| log::endpoint_id(&root, endpoint).unwrap_or_default());
    let anchors: Vec<_> = rows.iter().filter(|row| has_intent(row, key)).collect();
    let mut candidates = BTreeSet::new();
    let mut evidence = BTreeSet::new();
    for &row in &anchors {
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
    let mut entries = Vec::new();
    let mut direct = Vec::new();
    if let Some(chosen) = &chosen {
        direct.extend(
            anchors
                .iter()
                .copied()
                .filter(|row| scope(row).as_ref() == Some(chosen)),
        );
        let invocations: BTreeSet<_> = direct.iter().filter_map(|row| invocation(row)).collect();
        let conflicting: BTreeSet<_> = anchors
            .iter()
            .filter(|row| scope(row).is_some_and(|scope| &scope != chosen))
            .filter_map(|row| invocation(row))
            .collect();
        for row in &rows {
            if !in_window(row, cutoff) || event(row) == "log.dropped" {
                continue;
            }
            let matches = has_intent(row, key) && scope(row).as_ref() == Some(chosen);
            let joined = invocation(row)
                .is_some_and(|call| invocations.contains(&call) && !conflicting.contains(&call))
                && row
                    .get("intentKey")
                    .is_none_or(|value| value.is_null() || value.as_str() == Some(key))
                && address(row).is_none_or(|addr| addr == chosen.0)
                && (row.get("endpoint").is_none_or(Value::is_null)
                    || text(row, "endpointId") == Some(chosen.1))
                && row
                    .get("endpointId")
                    .is_none_or(|value| value.is_null() || value.as_str() == Some(chosen.1));
            if matches || joined {
                entries.push(("events", row));
            }
        }
    }
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
    let mut unattributed = Vec::new();
    for row in rows
        .iter()
        .filter(|row| in_window(row, cutoff) && event(row) == "log.dropped")
    {
        if text(row, "runId").is_some_and(|run| runs.contains(run)) {
            entries.push(("gaps", row));
        } else {
            unattributed.push(row);
        }
    }
    entries.sort_by_cached_key(|(_, row)| order(row));
    if !unattributed.is_empty() {
        value["unattributedGapCount"] = json!(unattributed.len());
        value["unattributedGaps"] = json!([]);
        truncated |= unattributed.len() > 8;
    }
    let unattributed = unattributed
        .iter()
        .skip(unattributed.len().saturating_sub(8))
        .map(|row| ("unattributedGaps", *row));
    let mut report = Report::new(value, scrub);
    let mut output_limited = false;
    let mut last = None;
    for (field, row) in unattributed.chain(entries).rev() {
        if !report.add(field, row) {
            output_limited = true;
            break;
        }
        if field != "unattributedGaps" {
            last = timestamp(row);
        }
    }
    for field in ["events", "gaps", "unattributedGaps"] {
        if let Some(rows) = report.value.get_mut(field).and_then(Value::as_array_mut) {
            rows.reverse();
        }
    }
    if truncated || output_limited {
        report.value["truncated"] = json!(true);
        if let Some(next) = last.filter(|_| output_limited).or(frontier.as_deref()) {
            report.value["nextBefore"] = json!(next);
        }
        report.value["more"] = json!(
            "Use --before for earlier timestamps in the scan window. To read past the scan limit, inspect files from sai logs path."
        );
    }
    Ok(report.value)
}
