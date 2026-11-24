use std::{borrow::Cow, num::IntErrorKind};

use toml_edit::{Document, TomlError};
use toml_parser::{
    Source,
    decoder::ScalarKind,
    parser::{Event, EventKind, RecursionGuard, parse_document},
};

use super::SETTINGS;

pub(super) fn configured_keys(text: &str) -> Result<Vec<&'static str>, TomlError> {
    let masked;
    let document = match Document::parse(text) {
        Ok(document) => document,
        Err(error) => {
            masked = mask_oversized_integers(text).ok_or(error)?;
            Document::parse(masked.as_str())?
        }
    };
    Ok(SETTINGS
        .iter()
        .filter_map(|&(section, key, name)| {
            document
                .get(section)?
                .as_table_like()?
                .get(key)
                .map(|_| name)
        })
        .collect())
}

// Python can parse integers wider than i64. Listing only needs key names, so
// use a placeholder for those tokens in a temporary copy. The full document
// still goes through TOML validation, and no masked values leave this module.
fn mask_oversized_integers(text: &str) -> Option<String> {
    let source = Source::new(text);
    let mut spans = Vec::new();
    let mut collect = |event: Event| {
        if event.kind() != EventKind::Scalar || event.encoding().is_some() {
            return;
        }
        let Some(raw) = source.get(event) else {
            return;
        };
        let mut decoded = Cow::Borrowed("");
        let mut error = None;
        let kind = raw.decode_scalar(&mut decoded, &mut error);
        if let ScalarKind::Integer(radix) = kind
            && error.is_none()
            && let Err(error) = i64::from_str_radix(&decoded, radix.value())
            && matches!(
                error.kind(),
                IntErrorKind::PosOverflow | IntErrorKind::NegOverflow
            )
        {
            spans.push(event.span());
        }
    };
    let mut error = None;
    let tokens = source.lex().into_vec();
    let mut receiver = RecursionGuard::new(&mut collect, 80);
    parse_document(&tokens, &mut receiver, &mut error);
    if error.is_some() || spans.is_empty() {
        return None;
    }

    let mut masked = String::with_capacity(text.len());
    let mut end = 0;
    for span in spans {
        masked.push_str(&text[end..span.start()]);
        masked.push('0');
        end = span.end();
    }
    masked.push_str(&text[end..]);
    Some(masked)
}
