use std::io::{self, Write};

use sai_core::errors::SaiError;
use serde::Serialize;
use serde_json::{
    Value,
    ser::{CompactFormatter, Formatter, PrettyFormatter},
};

use crate::local::{InlineJson, ascii_json};

pub(super) fn read(value: &impl Serialize, json: bool) -> Result<(), SaiError> {
    let text = if json {
        inline(value)?
    } else {
        render(value, PrettyFormatter::new())?
    };
    println!("{text}");
    Ok(())
}

pub(crate) fn inline(value: &impl Serialize) -> Result<String, SaiError> {
    render(value, InlineJson)
}

pub(super) fn line(value: &Value) -> Result<String, SaiError> {
    render(value, CompactFormatter)
}

fn render(value: &impl Serialize, format: impl Formatter) -> Result<String, SaiError> {
    let mut bytes = Vec::new();
    value
        .serialize(&mut serde_json::Serializer::with_formatter(
            &mut bytes,
            PythonNumbers(format),
        ))
        .map_err(|_| SaiError::internal())?;
    let text = String::from_utf8(bytes).map_err(|_| SaiError::internal())?;
    Ok(ascii_json(&text).into_owned())
}

struct PythonNumbers<F>(F);

// Delegate layout to the existing formatters. serde_json sends arbitrary
// precision Value numbers through write_number_str in all three layouts.
macro_rules! delegate_layout {
    ($($method:ident $(($arg:ident: $ty:ty))?),+ $(,)?) => {
        $(fn $method<W: Write + ?Sized>(&mut self, writer: &mut W $(, $arg: $ty)?) -> io::Result<()> {
            self.0.$method(writer $(, $arg)?)
        })+
    };
}

impl<F: Formatter> Formatter for PythonNumbers<F> {
    delegate_layout!(
        begin_array,
        end_array,
        begin_array_value(first: bool),
        end_array_value,
        begin_object,
        end_object,
        begin_object_key(first: bool),
        begin_object_value,
        end_object_value,
    );

    fn write_number_str<W: Write + ?Sized>(
        &mut self,
        writer: &mut W,
        wire: &str,
    ) -> io::Result<()> {
        if !wire.contains(['.', 'e', 'E']) {
            return writer.write_all(if wire == "-0" { b"0" } else { wire.as_bytes() });
        }
        let value: f64 = wire.parse().map_err(|_| io::ErrorKind::InvalidData)?;
        if value.is_infinite() {
            let text = if value.is_sign_negative() {
                "-Infinity"
            } else {
                "Infinity"
            };
            return writer.write_all(text.as_bytes());
        }

        let mut buffer = zmij::Buffer::new();
        let text = buffer.format_finite(value);
        let unsigned = text.strip_prefix('-').unwrap_or(text);
        if let Some(digits) = unsigned.strip_prefix("0.0000")
            && !digits.is_empty()
        {
            // zmij uses fixed notation at exponent -5, where Python uses e-05.
            let (first, rest) = digits.split_at(1);
            let sign = if value.is_sign_negative() { "-" } else { "" };
            let point = if rest.is_empty() { "" } else { "." };
            return write!(writer, "{sign}{first}{point}{rest}e-05");
        }
        if let Some((digits, exponent)) = text.split_once('e') {
            let exponent: i32 = exponent.parse().map_err(|_| io::ErrorKind::InvalidData)?;
            return write!(writer, "{digits}e{exponent:+03}");
        }
        writer.write_all(text.as_bytes())
    }
}
