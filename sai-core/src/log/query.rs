//! Read retained JSONL without creating a log directory or appending events.

mod scan;

use std::{collections::BTreeMap, path::Path, sync::LazyLock};

use chrono::{DateTime, Datelike, Days, NaiveDate, SecondsFormat, TimeDelta, Utc};
use regex::Regex;
use serde::de::{MapAccess, Visitor};
use serde_json::{Map, Value, json, value::RawValue};
use sha2::{Digest, Sha256};

use crate::{
    errors::{Code, SaiError},
    keeper,
    settings::Environment,
};

use super::{Field, files::LogDir, scrubber, valid_identifier as token};
use scan::{Budget, Position, Scan, Window};

pub type Row = Map<String, Value>;

static SCHEMA: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../../../docs/log-events.json")).expect("bundled log schema")
});
static STAMP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z\z").unwrap()
});
const REPORT_ROWS: usize = 128;
const REPORT_BYTES: usize = 96 * 1024 - 8192;
const FREE_TEXT: &[&str] = &[
    "argv",
    "arguments",
    "error",
    "errorMsg",
    "keeperErrors",
    "checks",
    "message",
    "reason",
];

fn text<'a>(row: &'a Row, key: &str) -> Option<&'a str> {
    row.get(key)
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
}

fn hex(text: &str, length: usize) -> bool {
    text.len() == length
        && text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn timestamp(row: &Row) -> Option<&str> {
    text(row, "ts").filter(|ts| valid_stamp(ts))
}

fn valid_stamp(ts: &str) -> bool {
    STAMP.is_match(ts) && crate::session::parse_expiry(ts).is_some()
}

fn stamp(instant: DateTime<Utc>) -> String {
    instant.to_rfc3339_opts(SecondsFormat::Millis, true)
}

fn usage(message: &str) -> SaiError {
    SaiError {
        show_usage: false,
        ..SaiError::usage(message)
    }
}

fn before(value: Option<&str>) -> Result<Option<DateTime<Utc>>, SaiError> {
    value
        .map(|value| {
            crate::session::parse_iso_timestamp(value)
                .filter(|(_, offset)| offset.is_zero())
                .map(|(instant, _)| instant)
                .ok_or_else(|| usage("Pass a UTC timestamp to --before."))
        })
        .transpose()
}

fn day(value: &str) -> Option<NaiveDate> {
    if value.len() != 10 {
        return None;
    }
    let day = NaiveDate::parse_from_str(value, "%Y-%m-%d").ok()?;
    (day.year() >= 1 && day.to_string() == value).then_some(day)
}

fn position(date: &str, offsets: &str) -> Option<Position> {
    let offsets = if offsets == "-" {
        None
    } else {
        let values = offsets
            .split(',')
            .map(|offset| {
                offset
                    .parse::<i64>()
                    .ok()
                    .and_then(|n| u64::try_from(n).ok())
            })
            .collect::<Option<Vec<_>>>()?;
        Some(values.try_into().ok()?)
    };
    Some(Position {
        day: day(date)?,
        offsets,
    })
}

fn format_position(position: Position) -> String {
    let offsets = position.offsets.map_or_else(
        || "-".into(),
        |values| format!("{},{},{}", values[0], values[1], values[2]),
    );
    format!("{}~{offsets}", position.day)
}

#[derive(Default)]
struct Cursor<'a> {
    upper: Option<String>,
    position: Option<Position>,
    timestamp: Option<&'a str>,
    parent: &'a str,
    replay: Option<Position>,
}

impl<'a> Cursor<'a> {
    fn parse(value: Option<&'a str>) -> Result<Self, SaiError> {
        let Some(value) = value.filter(|value| value.contains('~')) else {
            return Ok(Self {
                upper: before(value)?.map(stamp),
                ..Self::default()
            });
        };
        let invalid = || usage("Pass a log cursor or a UTC timestamp to --before.");
        if value.len() > 280 {
            return Err(invalid());
        }
        let parts: Vec<_> = value.split('~').collect();
        let [timestamp, date, offsets, upper, parent, replay @ ..] = parts.as_slice() else {
            return Err(invalid());
        };
        if !valid_stamp(timestamp)
            || (!upper.is_empty() && !valid_stamp(upper))
            || (!matches!(*parent, "" | "-") && !hex(parent, 64))
        {
            return Err(invalid());
        }
        let replay = match replay {
            [] => None,
            [date, offsets] if parent.is_empty() => {
                Some(position(date, offsets).ok_or_else(invalid)?)
            }
            _ => return Err(invalid()),
        };
        Ok(Self {
            upper: (!upper.is_empty()).then(|| (*upper).into()),
            position: Some(position(date, offsets).ok_or_else(invalid)?),
            timestamp: Some(timestamp),
            parent,
            replay,
        })
    }

    fn window(&self) -> Window<'_> {
        Window {
            start: self.position,
            last_day: self.upper.as_deref().and_then(|upper| day(&upper[..10])),
            before: self.upper.as_deref(),
            ..Window::default()
        }
    }

    fn encode(
        &self,
        position: Position,
        timestamp: &str,
        parent: &str,
        replay: Option<Position>,
    ) -> String {
        let position = format_position(position);
        let upper = self.upper.as_deref().unwrap_or_default();
        let mut cursor = format!("{timestamp}~{position}~{upper}~{parent}");
        if let Some(replay) = replay {
            cursor.push('~');
            cursor.push_str(&format_position(replay));
        }
        cursor
    }
}

fn nonfinite(value: &Value) -> bool {
    match value {
        Value::Number(number) if number.is_f64() => !number.as_f64().is_some_and(f64::is_finite),
        Value::Number(number) => {
            let text = number.as_str();
            text.contains(['.', 'e', 'E']) && !text.parse::<f64>().is_ok_and(f64::is_finite)
        }
        Value::Array(values) => values.iter().any(nonfinite),
        Value::Object(values) => values.values().any(nonfinite),
        _ => false,
    }
}

// Python's Unix argv can contain surrogate escapes for non-UTF-8 bytes.
fn decode_python_row(raw: &[u8]) -> Option<Row> {
    struct RetainedRow;

    impl<'de> Visitor<'de> for RetainedRow {
        type Value = Row;

        fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            formatter.write_str("a retained log object")
        }

        fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Row, A::Error> {
            let mut row = Row::new();
            while let Some(key) = map.next_key::<String>()? {
                if key == "argv" {
                    let raw = map.next_value::<&RawValue>()?;
                    if let Ok(value) = serde_json::from_str(raw.get()) {
                        row.insert(key, value);
                    } else {
                        row.remove(&key);
                    }
                } else {
                    row.insert(key, map.next_value()?);
                }
            }
            Ok(row)
        }
    }

    use serde::Deserializer;
    let mut parser = serde_json::Deserializer::from_slice(raw);
    let row = parser.deserialize_map(RetainedRow).ok()?;
    parser.end().ok()?;
    Some(row)
}

fn decode_row(raw: &[u8]) -> Option<Row> {
    let row: Row = serde_json::from_slice(raw)
        .ok()
        .or_else(|| decode_python_row(raw))?;
    if row.values().any(nonfinite) {
        return None;
    }
    let mut row = row;
    if text(&row, "event") == Some("keeper.request") && row.contains_key("keeperErrors") {
        let (codes, total) =
            keeper::graphql_error_fields(&row["keeperErrors"], true, row.get("keeperErrorCount"));
        row.insert("keeperErrors".into(), codes);
        row.insert("keeperErrorCount".into(), total);
    }
    Some(row)
}

pub fn order(row: &Row) -> (String, i128) {
    let ts = row.get("ts").map_or_else(String::new, keeper::stringify);
    let seq = row
        .get("seq")
        .and_then(|value| {
            value
                .as_number()
                .and_then(|n| n.as_str().parse().ok())
                .or_else(|| value.as_bool().map(i128::from))
        })
        .unwrap_or_default();
    (ts, seq)
}

pub fn path(env: &Environment) -> Value {
    let root = env.home.join("logs");
    let files: Vec<_> = LogDir::read_only(&root)
        .ok()
        .into_iter()
        .flat_map(|dir| scan::files(&dir).into_values().flatten().flatten())
        .collect();
    let keep = crate::settings::log_keep_value(env);
    json!({
        "dir": root.to_string_lossy(), "enabled": keep.is_some(),
        "keepDays": keep.unwrap_or_else(|| crate::settings::Key::LogsKeepDays.default_value()),
        "files": files
    })
}

pub fn tail(root: &Path, count: usize, surface: Option<&str>, name: Option<&str>) -> Vec<Row> {
    let Ok(directory) = LogDir::read_only(root) else {
        return Vec::new();
    };
    let mut lines = Vec::new();
    for files in scan::files(&directory).into_values().rev() {
        for file in files.into_iter().flatten().filter(|file| {
            surface.is_none_or(|surface| {
                file.strip_prefix(surface)
                    .is_some_and(|rest| rest.starts_with('-'))
            })
        }) {
            lines.extend(scan::tail(
                &directory,
                &file,
                count.saturating_add(256),
                |row| {
                    name.is_none_or(|name| {
                        text(row, "event").is_some_and(|event| {
                            event == name
                                || event
                                    .strip_prefix(name)
                                    .is_some_and(|suffix| suffix.starts_with('.'))
                        })
                    })
                },
            ));
        }
        if lines.len() >= count {
            break;
        }
    }
    lines.sort_by_cached_key(order);
    lines.drain(..lines.len().saturating_sub(count));
    for row in &mut lines {
        super::privacy::redact_retained_urls(row);
    }
    lines
}

pub fn render(row: &Row) -> String {
    let mut result = ["ts", "surface", "level", "event"]
        .map(|key| row.get(key).map_or_else(|| "?".into(), keeper::stringify))
        .join(" ");
    for (key, value) in row {
        if SCHEMA["common"]["required"].get(key).is_none() {
            result.push(' ');
            result.push_str(key);
            result.push('=');
            if let Some(value) = value.as_str() {
                result.push_str(value);
            } else {
                result.push_str(&super::json_ascii_value(value));
            }
        }
    }
    result.trim_end().into()
}

fn is_code(value: &Value) -> bool {
    serde_json::from_value::<Code>(value.clone()).is_ok()
}

fn export(row: &Row, scrub: &impl Fn(Field) -> Value) -> Value {
    let event = text(row, "event").unwrap_or_default();
    let spec = &SCHEMA["events"][event];
    let common = &SCHEMA["common"];
    let mut safe = Row::new();
    for (name, value) in row {
        if FREE_TEXT.contains(&name.as_str())
            || !public_scalar(value)
            || ![
                &common["required"],
                &common["optional"],
                &spec["required"],
                &spec["optional"],
            ]
            .iter()
            .any(|fields| fields.get(name).is_some())
        {
            continue;
        }
        let valid = match name.as_str() {
            "addr" | "account" => value.as_str().is_some_and(keeper::is_evm_address),
            "code" => is_code(value),
            "operation" => value.as_str().is_some_and(keeper::is_log_operation),
            "intentKey" | "runId" | "callId" | "commandId" => token(&keeper::stringify(value)),
            "previewDigest" if event == "mcp.tool_result" => hex(&keeper::stringify(value), 64),
            "status" if event == "mcp.tool_result" => matches!(
                value.as_str(),
                Some("accepted" | "rejected" | "not_sent" | "unknown" | "terminal" | "absent")
            ),
            "kind" | "lifecycleStep" | "execActor" | "errorCode" => {
                keeper::known_intent_field(name, value)
            }
            _ => true,
        };
        if valid {
            safe.insert(name.clone(), value.clone());
        }
    }
    if event == "keeper.request"
        && let Some(errors) = row.get("keeperErrors")
    {
        safe.insert("keeperErrors".into(), errors.clone());
    }
    if safe.contains_key("code") && (row.contains_key("error") || row.contains_key("errorMsg")) {
        safe.insert(
            "error".into(),
            json!("Sai recorded a failure. See the error code."),
        );
    }
    if let Some(endpoint) = safe.get_mut("endpoint") {
        *endpoint = scrub(Field::Endpoint(keeper::stringify(endpoint)));
    }
    safe.shift_remove("txHash");
    if let Some(hash) = row.get("txHash").filter(|hash| !hash.is_null()) {
        if let Some(hash) = keeper::request_hash(hash) {
            safe.insert("txHash".into(), json!(hash));
        } else {
            safe.insert("txHashInvalid".into(), json!(true));
        }
    }
    scrub(Value::Object(safe).into())
}

fn public_scalar(value: &Value) -> bool {
    value.is_string()
        || value.is_boolean()
        || value
            .as_number()
            .is_some_and(|number| !number.as_str().contains(['.', 'e', 'E']))
}

fn separator_spaces(value: &Value) -> usize {
    match value {
        Value::Object(fields) => {
            fields.len()
                + fields.len().saturating_sub(1)
                + fields.values().map(separator_spaces).sum::<usize>()
        }
        Value::Array(items) => {
            items.len().saturating_sub(1) + items.iter().map(separator_spaces).sum::<usize>()
        }
        _ => 0,
    }
}

struct Report<F> {
    value: Value,
    bytes: usize,
    rows: usize,
    scrub: F,
}

impl<F: Fn(Field) -> Value> Report<F> {
    fn new(value: Value, scrub: F) -> Self {
        Self {
            value,
            bytes: REPORT_BYTES,
            rows: REPORT_ROWS,
            scrub,
        }
    }

    fn add(&mut self, key: &str, row: &Row) -> bool {
        let row = export(row, &self.scrub);
        // Python counts default JSON separators toward the report budget.
        let bytes = super::json_ascii_value(&row).len() + separator_spaces(&row) + 2;
        if self.rows == 0 || bytes > self.bytes {
            return false;
        }
        self.bytes -= bytes;
        self.rows -= 1;
        self.value[key]
            .as_array_mut()
            .expect("initialized report field")
            .push(row);
        true
    }

    fn sort(&mut self) {
        for key in ["events", "gaps", "unattributedGaps"] {
            if let Some(rows) = self.value.get_mut(key).and_then(Value::as_array_mut) {
                rows.sort_by_cached_key(|row| order(row.as_object().unwrap()));
            }
        }
    }

    fn finish(
        mut self,
        scan: Scan,
        cursor: &Cursor<'_>,
        last: Option<&str>,
        stop: Option<Position>,
        parent: &str,
        replay: Option<Position>,
    ) -> Value {
        self.sort();
        if scan.truncated || stop.is_some() {
            self.value["truncated"] = json!(true);
            self.value["more"] = json!("Use --before to continue reading the retained logs.");
            if let Some(position) = stop.or(scan.resume) {
                let last = last.or(cursor.timestamp).or(cursor.upper.as_deref());
                let fallback;
                let ts = match last {
                    Some(last) => last,
                    None => {
                        fallback = format!("{}T00:00:00.000Z", position.day);
                        &fallback
                    }
                };
                self.value["nextBefore"] = json!(cursor.encode(position, ts, parent, replay));
            }
        }
        self.value
    }
}

pub fn run(home: &Path, identifier: &str, before: Option<&str>) -> Result<Value, SaiError> {
    if !token(identifier) {
        return Err(SaiError::new(
            Code::InvalidRequest,
            "Invalid log run or call ID.",
        ));
    }
    let root = home.join("logs");
    let cursor = Cursor::parse(before)?;
    let window = cursor.window();
    let mut parent = if hex(identifier, 32) {
        "-"
    } else {
        cursor.parent
    }
    .to_owned();
    let parent_key = |run: &str| format!("{:x}", Sha256::digest(format!("{identifier}~{run}")));
    let call_parent = |row: &Row| {
        let is_call = text(row, "callId") == Some(identifier)
            && text(row, "event") != Some("log.dropped")
            && timestamp(row).is_some();
        text(row, "runId")
            .filter(|run| is_call && token(run))
            .map(parent_key)
    };
    if parent.is_empty()
        && cursor.position.is_none()
        && let Some(following) = window
            .last_day
            .and_then(|day| day.checked_add_days(Days::new(1)))
    {
        let next = scan::prefix(&root, following, |row| {
            text(row, "callId") == Some(identifier)
        });
        if let Some(key) = next.rows.iter().rev().find_map(|(_, row)| call_parent(row)) {
            parent = key;
        }
    }
    let find_parent = parent.is_empty();
    let mut entries = Vec::new();
    let mut last = None;
    let scan = scan::page(
        &root,
        Budget::RUN,
        window,
        |_, row| {
            if find_parent && let Some(key) = call_parent(row) {
                parent = key;
            }
        },
        |position, row| {
            if let Some(ts) = timestamp(&row) {
                last = Some(ts.to_owned());
                if cursor.replay.is_none()
                    && (text(&row, "event") == Some("log.dropped")
                        || [text(&row, "runId"), text(&row, "callId")].contains(&Some(identifier)))
                {
                    entries.push((position, row));
                }
            }
            true
        },
    );
    let mut report = Report::new(
        json!({"id": identifier, "events": [], "gaps": []}),
        scrubber(home),
    );
    if let Some(replay) = cursor.replay {
        let (stop, replay, parent) = if parent.is_empty() && scan.resume.is_some() {
            (None, Some(replay), "")
        } else {
            (
                Some(replay),
                None,
                if parent.is_empty() { "-" } else { &parent },
            )
        };
        return Ok(report.finish(scan, &cursor, last.as_deref(), stop, parent, replay));
    }

    let mut displayed = None;
    for (position, row) in entries {
        let field = if text(&row, "event") == Some("log.dropped") {
            let Some(run) = text(&row, "runId").filter(|run| token(run)) else {
                continue;
            };
            if run != identifier {
                if parent.is_empty() && scan.resume.is_some() {
                    return Ok(report.finish(
                        scan,
                        &cursor,
                        last.as_deref(),
                        None,
                        "",
                        Some(position),
                    ));
                }
                if parent != parent_key(run) {
                    continue;
                }
            }
            "gaps"
        } else {
            "events"
        };
        if !report.add(field, &row) {
            return Ok(report.finish(
                scan,
                &cursor,
                displayed.as_deref(),
                Some(position),
                &parent,
                None,
            ));
        }
        displayed = timestamp(&row).map(str::to_owned);
    }
    Ok(report.finish(scan, &cursor, last.as_deref(), None, &parent, None))
}

fn duration(value: &str) -> Result<i64, SaiError> {
    let invalid =
        || usage("Use a positive whole duration followed by s, m, h, d, or w, such as 24h.");
    let (amount, unit) = value
        .split_at_checked(value.len().saturating_sub(1))
        .ok_or_else(invalid)?;
    let multiplier = match unit {
        "s" => 1,
        "m" => 60,
        "h" => 3600,
        "d" => 86400,
        "w" => 604800,
        _ => return Err(invalid()),
    };
    amount
        .bytes()
        .all(|byte| byte.is_ascii_digit())
        .then_some(())
        .and_then(|()| amount.parse::<i64>().ok())
        .filter(|&value| value > 0 && value <= 999_999_999 * 86400 / multiplier)
        .map(|amount| amount * multiplier)
        .ok_or_else(invalid)
}

pub fn stats(home: &Path, since: Option<&str>, before: Option<&str>) -> Result<Value, SaiError> {
    let cursor = Cursor::parse(before)?;
    let first = since.map(duration).transpose()?.map(|seconds| {
        let now = Utc::now();
        let first = NaiveDate::from_ymd_opt(1, 1, 1).unwrap();
        let seconds = seconds.min((now.date_naive() - first).num_days() * 86400);
        now - TimeDelta::seconds(seconds)
    });
    let since = first.map(stamp);
    let mut window = cursor.window();
    window.first_day = first.map(|first| first.date_naive());
    window.since = since.as_deref();
    let mut report = Report::new(json!({"since": since, "gaps": []}), scrubber(home));
    let mut durations = BTreeMap::<String, Vec<serde_json::Number>>::new();
    let mut codes = BTreeMap::<String, usize>::new();
    let mut stop = None;
    let mut last = None;
    let scan = scan::page(
        &home.join("logs"),
        Budget::PAGE,
        window,
        |_, _| {},
        |position, row| {
            let Some(ts) = timestamp(&row) else {
                return true;
            };
            if text(&row, "event") == Some("log.dropped") {
                if !report.add("gaps", &row) {
                    stop = Some(position);
                    return false;
                }
            } else {
                if let Some(code) = row
                    .get("code")
                    .filter(|code| is_code(code))
                    .and_then(Value::as_str)
                {
                    *codes.entry(code.into()).or_default() += 1;
                }
                if text(&row, "event") == Some("keeper.request")
                    && let Some(operation) = text(&row, "operation")
                        .filter(|operation| keeper::is_log_operation(operation))
                    && let Some(duration) = row
                        .get("durationMs")
                        .and_then(Value::as_number)
                        .filter(|number| number.as_str().bytes().all(|byte| byte.is_ascii_digit()))
                {
                    durations
                        .entry(operation.into())
                        .or_default()
                        .push(duration.clone());
                }
            }
            last = Some(ts.to_owned());
            true
        },
    );
    let operations: Row = durations
        .into_iter()
        .map(|(operation, mut samples)| {
            samples.sort_unstable_by(|a, b| {
                let (a, b) = (a.as_str(), b.as_str());
                (a.len(), a).cmp(&(b.len(), b))
            });
            let count = samples.len();
            (
                operation,
                json!({
                    "count": count, "p50Ms": samples[(count * 50).div_ceil(100) - 1],
                    "p95Ms": samples[(count * 95).div_ceil(100) - 1]
                }),
            )
        })
        .collect();
    let mut report = report.finish(scan, &cursor, last.as_deref(), stop, "", None);
    report["keeper"] = json!(operations);
    report["errors"] = json!(codes);
    Ok(report)
}

mod intent;
pub use intent::intent;
