use std::{
    fs::OpenOptions,
    io::{self, Write},
    ops::Range,
    path::Path,
    sync::atomic::AtomicBool,
};

use serde_json::Value;
use toml_edit::{Item, Table};

use crate::interrupt;

use super::toml::ParsedDocument;
use super::{Key, MAINNET_WS, SettingsError, os_message, parse_error, read_raw_text, table_error};
use crate::paths;

const TEMPLATE: &str = include_str!("../settings-template.toml");
const LOCK_NAME: &str = ".config.lock";

pub fn set(path: &Path, key: Key, value: &Value) -> Result<(), SettingsError> {
    edit_setting(path, key, Some(value), None).map(|_| ())
}

pub fn unset(path: &Path, key: Key) -> Result<bool, SettingsError> {
    edit_setting(path, key, None, None)
}

pub fn edit_setting(
    path: &Path,
    key: Key,
    value: Option<&Value>,
    interrupt: Option<&AtomicBool>,
) -> Result<bool, SettingsError> {
    let parent = path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let writing = |error: io::Error| {
        if interrupt.is_some() && error.kind() == io::ErrorKind::Interrupted {
            return SettingsError::invalid("Aborted".into());
        }
        SettingsError::invalid(format!(
            "Cannot write {}: {}",
            path.display(),
            os_message(&error)
        ))
    };

    interrupt::check(interrupt).map_err(writing)?;
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    paths::profile(parent).map_err(writing)?;
    let lock = options.open(parent.join(LOCK_NAME)).map_err(writing)?;
    interrupt::lock(&lock, interrupt).map_err(writing)?;
    let text = read_raw_text(path)?;
    let Some(new) = edit(text.as_deref(), path, key, value)? else {
        return Ok(false);
    };

    let mut temp = tempfile::Builder::new()
        .prefix(".config-")
        .tempfile_in(parent)
        .map_err(writing)?;
    temp.write_all(new.as_bytes()).map_err(writing)?;
    interrupt::check(interrupt).map_err(writing)?;
    temp.persist(path).map_err(|error| writing(error.error))?;
    Ok(true)
}

fn edit(
    text: Option<&str>,
    path: &Path,
    key: Key,
    new: Option<&Value>,
) -> Result<Option<String>, SettingsError> {
    let text = text.unwrap_or(TEMPLATE);
    let newline = if text.contains("\r\n") { "\r\n" } else { "\n" };
    let parsed =
        ParsedDocument::parse(text).map_err(|error| parse_error(path, text, &error, true))?;
    let document = &parsed.document;
    let (section, name) = key.section_and_name();
    let new_line = |value: &Value| format!("{name} = {}", toml_value(value));
    let Some(item) = document.get(section) else {
        return Ok(new.map(|value| {
            let trailing_blank = text
                .strip_suffix(newline)
                .is_some_and(|tail| tail.ends_with(newline));
            let separator = if text.is_empty() || trailing_blank {
                ""
            } else {
                newline
            };
            insert_line(
                text,
                text.len(),
                &format!("{separator}[{section}]{newline}{}", new_line(value)),
                newline,
            )
        }));
    };
    let table = match item {
        Item::Table(table) if !table.is_implicit() && !table.is_dotted() => table,
        item if item.is_table_like() => return Err(uneditable(section, path)),
        _ => {
            return Err(SettingsError::invalid(format!(
                "{}: {}",
                path.display(),
                table_error(section)
            )));
        }
    };
    let header = table.span().expect("explicit parsed table has a span");
    let mut children = Vec::new();
    table_headers(table, &mut children);
    if children.iter().any(|&offset| offset < header.start) {
        return Err(uneditable(section, path));
    }
    let mut headers = Vec::new();
    table_headers(document.as_table(), &mut headers);
    let body_start = line_end(text, header.start);
    let next_header = headers
        .into_iter()
        .filter(|&offset| offset > header.start)
        .min();
    let body_end = next_header.map_or(text.len(), |offset| line_start(text, offset));
    let body = &text[body_start..body_end];
    let content = body.trim_end();
    let append_at = if content.is_empty() {
        body_start
    } else {
        line_end(&text[..body_end], body_start + content.len())
    };
    let insert_gap =
        next_header.is_some_and(|offset| children.contains(&offset)) && append_at == body_end;
    let placeholder = format!("# {name} = {}", placeholder_value(key));

    if let Some((key, item)) = table.get_key_value(name) {
        if let Some(value) = item.as_value() {
            let range = parsed.value_span(value.span().expect("parsed value has a span"));
            if let Some(new) = new {
                return Ok(Some(replace(text, range, &toml_value(new))));
            }
            let start = line_start(text, key.span().expect("parsed key has a span").start);
            let end = line_end(text, range.end);
            let note = inline_note(&text[range.end..end]);
            return Ok(Some(replace(
                text,
                start..end,
                &format!("{placeholder}{note}{newline}"),
            )));
        }
        // A table is an invalid value for every registry key. Overwriting or
        // removing it must remove its child headers and dotted assignments.
        let invalid = item_ranges(text, item);
        let assignment = new.map_or_else(|| placeholder.clone(), new_line);
        let mut updated = text.to_owned();
        let mut offset = append_at;
        if new.is_none()
            && item.as_table().is_some_and(Table::is_dotted)
            && let Some(first) = invalid.first()
        {
            offset = first.start;
        }
        for range in invalid.into_iter().rev() {
            offset -= offset.saturating_sub(range.start).min(range.len());
            updated.replace_range(range, "");
        }
        return Ok(Some(insert_line(&updated, offset, &assignment, newline)));
    }

    let Some(new) = new else { return Ok(None) };
    let assignment = new_line(new);
    let prefix = format!("# {name} =");
    let values = table
        .get_values()
        .into_iter()
        .filter_map(|(_, value)| value.span())
        .collect::<Vec<_>>();
    let mut offset = body_start;
    for line in body.split_inclusive('\n') {
        let next = offset + line.len();
        let trimmed = line.trim();
        if trimmed.starts_with(&prefix) && !values.iter().any(|range| range.contains(&offset)) {
            let note = trimmed
                .strip_prefix(&placeholder)
                .map(str::trim)
                .filter(|note| note.starts_with('#'));
            let suffix = note.map_or_else(String::new, |note| format!("  {note}"));
            return Ok(Some(replace(
                text,
                offset..next,
                &format!("{assignment}{suffix}{newline}"),
            )));
        }
        offset = next;
    }
    let assignment = if insert_gap {
        format!("{assignment}{newline}")
    } else {
        assignment
    };
    Ok(Some(insert_line(text, append_at, &assignment, newline)))
}

fn uneditable(section: &str, path: &Path) -> SettingsError {
    SettingsError {
        malformed_toml: false,
        message: format!(
            "{section} in {} is an inline table, dotted keys, or only subtables; rewrite it as a [{section}] table to edit it with sai config",
            path.display()
        ),
        uneditable: true,
        invalid_endpoint: false,
        invalid_override: false,
    }
}

fn table_headers(table: &Table, offsets: &mut Vec<usize>) {
    for (_, item) in table.iter() {
        if let Some(table) = item.as_table() {
            offsets.extend(header_span(table).map(|span| span.start));
            table_headers(table, offsets);
        } else if let Some(tables) = item.as_array_of_tables() {
            for table in tables {
                offsets.extend(header_span(table).map(|span| span.start));
                table_headers(table, offsets);
            }
        }
    }
}

// A dotted or implicit table may have a span for its keys or values but
// only an explicit table has a header and a physical body.
fn header_span(table: &Table) -> Option<Range<usize>> {
    if table.is_implicit() || table.is_dotted() {
        None
    } else {
        table.span()
    }
}

fn item_ranges(text: &str, item: &Item) -> Vec<Range<usize>> {
    let mut ranges = Vec::new();
    collect_item_ranges(item, &mut ranges);
    for range in &mut ranges {
        *range = line_start(text, range.start)..line_end(text, range.end);
    }
    ranges.sort_by_key(|range| range.start);
    let mut merged: Vec<Range<usize>> = Vec::new();
    for range in ranges {
        if let Some(last) = merged.last_mut()
            && range.start <= last.end
        {
            last.end = last.end.max(range.end);
        } else {
            merged.push(range);
        }
    }
    merged
}

fn collect_item_ranges(item: &Item, ranges: &mut Vec<Range<usize>>) {
    let mut add_table = |table: &Table| {
        ranges.extend(header_span(table));
        for (_, child) in table.iter() {
            collect_item_ranges(child, ranges);
        }
    };
    match item {
        Item::Value(value) => ranges.extend(value.span()),
        Item::Table(table) => add_table(table),
        Item::ArrayOfTables(tables) => tables.iter().for_each(add_table),
        Item::None => {}
    }
}

fn line_start(text: &str, offset: usize) -> usize {
    text[..offset]
        .rfind('\n')
        .map_or(0, |position| position + 1)
}

fn line_end(text: &str, offset: usize) -> usize {
    text[offset..]
        .find('\n')
        .map_or(text.len(), |position| offset + position + 1)
}

fn inline_note(suffix: &str) -> String {
    let suffix = suffix.trim_end_matches(['\r', '\n']);
    match suffix.find('#') {
        Some(0) => format!("  {suffix}"),
        Some(_) => suffix.to_owned(),
        None => String::new(),
    }
}

fn replace(text: &str, range: Range<usize>, value: &str) -> String {
    let mut edited = text.to_owned();
    edited.replace_range(range, value);
    edited
}

fn insert_line(text: &str, offset: usize, line: &str, newline: &str) -> String {
    let separator = if offset == 0 || text[..offset].ends_with('\n') {
        ""
    } else {
        newline
    };
    replace(text, offset..offset, &format!("{separator}{line}{newline}"))
}

fn placeholder_value(key: Key) -> String {
    if key == Key::KeeperWsEndpoint {
        return toml_value(&Value::String(MAINNET_WS.to_owned()));
    }
    toml_value(&key.default_value())
}

fn toml_value(value: &Value) -> String {
    value.to_string().replace('\u{7f}', "\\u007F")
}
