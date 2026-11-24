//! Parse v0.1 TOML while preserving offsets for comment-preserving edits.

use std::{borrow::Cow, num::IntErrorKind, ops::Range};

use num_bigint::BigInt;
use serde_json::Value;
use toml_edit::{Document, TomlError};
use toml_parser::{
    Source,
    decoder::ScalarKind,
    parser::{Event, EventKind, RecursionGuard, parse_document},
};

pub(super) struct ParsedDocument {
    pub document: Document<String>,
    oversized: Vec<(Range<usize>, BigInt)>,
}

impl ParsedDocument {
    pub fn parse(text: &str) -> Result<Self, TomlError> {
        match Document::parse(text.to_owned()) {
            Ok(document) => Ok(Self {
                document,
                oversized: Vec::new(),
            }),
            Err(error) => {
                let oversized = oversized_integers(text).ok_or(error)?;
                // Substitute a same-width token so that unrelated spans,
                // whitespace, and comments still locate the original bytes.
                let mut masked = text.to_owned();
                for (span, _) in &oversized {
                    masked.replace_range(span.clone(), &format!("0{}", " ".repeat(span.len() - 1)));
                }
                let document = Document::parse(masked)?;
                Ok(Self {
                    document,
                    oversized,
                })
            }
        }
    }

    pub fn value_span(&self, span: Range<usize>) -> Range<usize> {
        self.oversized
            .iter()
            .find(|(raw, _)| raw.start == span.start)
            .map_or(span, |(raw, _)| raw.clone())
    }

    pub fn oversized_number(&self, span: &Range<usize>) -> Option<Value> {
        let (_, value) = self
            .oversized
            .iter()
            .find(|(raw, _)| raw.start == span.start)?;
        serde_json::from_str(&value.to_string()).ok()
    }
}

// tomllib accepts integers wider than i64. Use the scaffold's token pass to
// find them, and retain the original numeric values for known settings.
fn oversized_integers(text: &str) -> Option<Vec<(Range<usize>, BigInt)>> {
    let source = Source::new(text);
    let mut values = Vec::new();
    let mut collect = |event: Event| {
        if event.kind() != EventKind::Scalar || event.encoding().is_some() {
            return;
        }
        let Some(raw) = source.get(event) else { return };
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
            && let Some(value) = BigInt::parse_bytes(decoded.as_bytes(), radix.value())
        {
            values.push((event.span().start()..event.span().end(), value));
        }
    };
    let mut error = None;
    let tokens = source.lex().into_vec();
    let mut receiver = RecursionGuard::new(&mut collect, 80);
    parse_document(&tokens, &mut receiver, &mut error);
    if error.is_some() || values.is_empty() {
        None
    } else {
        Some(values)
    }
}
