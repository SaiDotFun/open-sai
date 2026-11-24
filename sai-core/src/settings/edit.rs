use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    ops::Range,
    path::Path,
};

use serde_json::Value;
use toml_edit::{Item, Table};

use super::toml::ParsedDocument;
use super::{
    Key, MAINNET_WS, SettingsError, os_message, parse_error, read_text,
    table_error,
};

const TEMPLATE: &str = include_str!("../settings-template.toml");
const LOCK_NAME: &str = ".config.lock";

pub fn set(path: &Path, key: Key, value: &Value) -> Result<(), SettingsError> {
    edit_file(path, |text| {
        let new = edit(text, path, key, Some(value))?;
        Ok(new)
    })?;
    Ok(())
}

pub fn unset(path: &Path, key: Key) -> Result<bool, SettingsError> {
    edit_file(path, |text| edit(text, path, key, None))
}

fn edit_file(
    path: &Path,
    edit: impl FnOnce(Option<&str>) -> Result<Option<String>, SettingsError>,
) -> Result<bool, SettingsError> {
    let parent = path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let writing = |error: io::Error| {
        SettingsError::invalid(format!(
            "Cannot write {}: {}",
            path.display(),
            os_message(&error)
        ))
    };

    let mut directory = fs::DirBuilder::new();
    directory.recursive(true);
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
        directory.mode(0o700);
        options.mode(0o600);
    }
    directory.create(parent).map_err(writing)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
            .map_err(writing)?;
    }
    let lock = options.open(parent.join(LOCK_NAME)).map_err(writing)?;
    lock.lock().map_err(writing)?;
    let text = read_text(path)?;
    let Some(new) = edit(text.as_deref())? else {
        return Ok(false);
    };

    let mut temp = tempfile::Builder::new()
        .prefix(".config-")
        .tempfile_in(parent)
        .map_err(writing)?;
    temp.write_all(new.as_bytes()).map_err(writing)?;
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
    let parsed = ParsedDocument::parse(text)
        .map_err(|error| parse_error(path, text, &error, true))?;
    let document = &parsed.document;
    let (section, name) = key.section_and_name();
    let new_line = |value: &Value| format!("{name} = {}", toml_value(value));
    let Some(item) = document.get(section) else {
        return Ok(new.map(|value| {
            let separator = if text.is_empty() || text.ends_with("\n\n") {
                ""
            } else if text.ends_with('\n') {
                "\n"
            } else {
                "\n\n"
            };
            format!("{text}{separator}[{section}]\n{}\n", new_line(value))
        }));
    };
    let table = match item {
        Item::Table(table) if !table.is_implicit() && !table.is_dotted() => {
            table
        }
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
    let body_end =
        next_header.map_or(text.len(), |offset| line_start(text, offset));
    let body = &text[body_start..body_end];
    let content = body.trim_end();
    let append_at = if content.is_empty() {
        body_start
    } else {
        line_end(&text[..body_end], body_start + content.len())
    };
    let insert_gap = next_header
        .is_some_and(|offset| children.contains(&offset))
        && append_at == body_end;
    let placeholder = format!("# {name} = {}", placeholder_value(key));

    if let Some((key, item)) = table.get_key_value(name) {
        if let Some(value) = item.as_value() {
            let range = parsed
                .value_span(value.span().expect("parsed value has a span"));
            if let Some(new) = new {
                return Ok(Some(replace(text, range, &toml_value(new))));
            }
            let start = line_start(
                text,
                key.span().expect("parsed key has a span").start,
            );
            let end = line_end(text, range.end);
            let note = inline_note(&text[range.end..end]);
            return Ok(Some(replace(
                text,
                start..end,
                &format!("{placeholder}{note}\n"),
            )));
        }
        // A table is an invalid value for every registry key. Overwriting or
        // removing it must remove its child headers and dotted assignments.
        let invalid = item
            .as_table()
            .map(|child| table_ranges(text, child, document.as_table()))
            .or_else(|| {
                item.as_array_of_tables().map(|tables| {
                    tables
                        .iter()
                        .flat_map(|table| {
                            table_ranges(text, table, document.as_table())
                        })
                        .collect()
                })
            })
            .unwrap_or_default();
        let assignment = new.map_or_else(|| placeholder.clone(), new_line);
        let mut updated = text.to_owned();
        let mut offset = append_at;
        for range in invalid.into_iter().rev() {
            offset -= offset.saturating_sub(range.start).min(range.len());
            updated.replace_range(range, "");
        }
        return Ok(Some(insert_line(&updated, offset, &assignment)));
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
        if trimmed.starts_with(&prefix)
            && !values.iter().any(|range| range.contains(&offset))
        {
            let note = trimmed
                .strip_prefix(&placeholder)
                .map(str::trim)
                .filter(|note| note.starts_with('#'));
            let suffix =
                note.map_or_else(String::new, |note| format!("  {note}"));
            return Ok(Some(replace(
                text,
                offset..next,
                &format!("{assignment}{suffix}\n"),
            )));
        }
        offset = next;
    }
    let assignment = if insert_gap {
        format!("{assignment}\n")
    } else {
        assignment
    };
    Ok(Some(insert_line(text, append_at, &assignment)))
}

fn uneditable(section: &str, path: &Path) -> SettingsError {
    SettingsError {
        message: format!(
            "{section} in {} is an inline table, dotted keys, or only subtables; rewrite it as a [{section}] table to edit it with sai config",
            path.display()
        ),
        uneditable: true,
    }
}

fn table_headers(table: &Table, offsets: &mut Vec<usize>) {
    for (_, item) in table.iter() {
        if let Some(table) = item.as_table() {
            offsets.extend(table.span().map(|span| span.start));
            table_headers(table, offsets);
        } else if let Some(tables) = item.as_array_of_tables() {
            for table in tables {
                offsets.extend(table.span().map(|span| span.start));
                table_headers(table, offsets);
            }
        }
    }
}

fn table_ranges(text: &str, table: &Table, root: &Table) -> Vec<Range<usize>> {
    let mut starts = Vec::new();
    starts.extend(table.span().map(|span| span.start));
    table_headers(table, &mut starts);
    let mut headers = Vec::new();
    table_headers(root, &mut headers);
    let mut ranges = starts
        .into_iter()
        .map(|offset| {
            let end = headers
                .iter()
                .copied()
                .filter(|&next| next > offset)
                .min()
                .map_or(text.len(), |next| line_start(text, next));
            line_start(text, offset)..end
        })
        .collect::<Vec<_>>();
    if table.is_dotted() {
        ranges.extend(table.get_values().into_iter().filter_map(
            |(keys, value)| {
                let start = line_start(text, keys.first()?.span()?.start);
                let end = line_end(text, value.span()?.end);
                Some(start..end)
            },
        ));
    }
    ranges.sort_by_key(|range| range.start);
    ranges
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
    let suffix = suffix.trim_end_matches('\n');
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

fn insert_line(text: &str, offset: usize, line: &str) -> String {
    let separator = if offset == 0 || text[..offset].ends_with('\n') {
        ""
    } else {
        "\n"
    };
    replace(text, offset..offset, &format!("{separator}{line}\n"))
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
