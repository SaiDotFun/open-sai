//! Report tool validation categories and schema-defined fields without rejected input.

use std::fmt::Write as _;

use rmcp::model::Tool;
use sai_core::settings::repr;
use serde_json::{Map, Value};

const PYDANTIC_ERRORS: &str = "https://errors.pydantic.dev/2.14/v";

pub(super) fn arguments(
    tool: &Tool,
    mut args: Map<String, Value>,
) -> Result<Map<String, Value>, String> {
    let properties = tool.input_schema["properties"]
        .as_object()
        .expect("tool properties");
    for (name, schema) in properties {
        // FastMCP interprets JSON containers and null for fields other than
        // plain strings. In particular, an optional address of "null" is null.
        if schema["type"] != "string"
            && let Some(value) = args.get_mut(name)
            && let Some(text) = value.as_str()
            && let Ok(parsed @ (Value::Null | Value::Array(_) | Value::Object(_))) =
                serde_json::from_str(text)
        {
            *value = parsed;
        }
    }
    let required = tool.input_schema.get("required").and_then(Value::as_array);
    let mut issues = Vec::new();
    let mut converted = Vec::new();
    for (name, schema) in properties {
        let Some(value) = args.get(name) else {
            if required.is_some_and(|fields| fields.iter().any(|field| field == name)) {
                issues.push(Issue::new(Some(name), "Field required", "missing"));
            }
            continue;
        };
        let schema = if let Some(alternatives) = schema.get("anyOf").and_then(Value::as_array) {
            if value.is_null() {
                continue;
            }
            &alternatives[0]
        } else {
            schema
        };
        if let Some(choices) = schema.get("enum").and_then(Value::as_array) {
            if !choices.contains(value) {
                let choices: Vec<_> = choices
                    .iter()
                    .map(|choice| choice.as_str().map_or_else(|| choice.to_string(), repr))
                    .collect();
                let (last, rest) = choices.split_last().expect("enum has choices");
                let message = format!("Input should be {} or {last}", rest.join(", "));
                issues.push(Issue::new(Some(name), message, "literal_error"));
            }
            continue;
        }
        let conversion = match schema["type"].as_str() {
            Some("string") if !value.is_string() => {
                Err(("Input should be a valid string", "string_type"))
            }
            Some("integer") => integer(value),
            Some("boolean") => boolean(value),
            _ => Ok(None),
        };
        match conversion {
            Ok(Some(value)) => converted.push((name.clone(), value)),
            Ok(None) => {}
            Err((message, code)) => issues.push(Issue::new(Some(name), message, code)),
        }
    }
    for name in args.keys() {
        if !properties.contains_key(name) {
            issues.push(Issue::new(
                None,
                "Extra inputs are not permitted",
                "extra_forbidden",
            ));
        }
    }
    let Some(first) = issues.first() else {
        args.extend(converted);
        return Ok(args);
    };
    if tool.name == "logs_intent" {
        return Err(match first.field {
            Some("key") => "Argument 'key' must be a string.",
            Some("offline") => "Argument 'offline' must be a boolean.",
            Some("addr") => "Argument 'addr' must be a string or null.",
            Some("before") => "Argument 'before' must be a string or null.",
            _ => "Expected audit arguments 'key' (a string), 'offline' (a boolean), 'addr' (a string or null), 'before' (a string or null).",
        }.into());
    }
    let count = issues.len();
    let plural = if count == 1 { "" } else { "s" };
    let mut message = format!(
        "Error executing tool {}: {count} validation error{plural} for {}Arguments",
        tool.name, tool.name,
    );
    for Issue {
        field,
        message: detail,
        code,
    } in issues
    {
        if let Some(field) = field {
            let _ = write!(message, "\n{field}");
        }
        let _ = write!(
            message,
            "\n  {detail} [type={code}]\n    For further information visit {PYDANTIC_ERRORS}/{code}",
        );
    }
    Err(message)
}

struct Issue<'a> {
    field: Option<&'a str>,
    message: String,
    code: &'static str,
}

impl<'a> Issue<'a> {
    fn new(field: Option<&'a str>, message: impl Into<String>, code: &'static str) -> Self {
        Self {
            field,
            message: message.into(),
            code,
        }
    }
}

fn is_integer(value: &Value) -> bool {
    value
        .as_number()
        .is_some_and(|number| !number.as_str().contains(['.', 'e', 'E']))
}

type Conversion = Result<Option<Value>, (&'static str, &'static str)>;

fn integer(value: &Value) -> Conversion {
    match value {
        Value::Bool(flag) => Ok(Some(Value::from(u8::from(*flag)))),
        Value::Number(_) if is_integer(value) => Ok(None),
        Value::Number(number) => {
            let float = number
                .as_f64()
                .filter(|value| value.is_finite())
                .ok_or(("Input should be a finite number", "finite_number"))?;
            if float.fract() != 0.0 {
                return Err((
                    "Input should be a valid integer, got a number with a fractional part",
                    "int_from_float",
                ));
            }
            if float.abs() >= 2_f64.powi(63) {
                return Err((
                    "Unable to parse input string as an integer, exceeded maximum size",
                    "int_parsing_size",
                ));
            }
            let value = if float == 0.0 {
                Value::from(0)
            } else {
                serde_json::from_str(&format!("{float:.0}")).expect("finite integral float")
            };
            Ok(Some(value))
        }
        Value::String(text)
            if text.bytes().filter(u8::is_ascii_digit).take(4301).count() > 4300 =>
        {
            Err((
                "Unable to parse input string as an integer, exceeded maximum size",
                "int_parsing_size",
            ))
        }
        Value::String(text) => integer_string(text).map(Some).ok_or((
            "Input should be a valid integer, unable to parse string as an integer",
            "int_parsing",
        )),
        _ => Err(("Input should be a valid integer", "int_type")),
    }
}

fn integer_string(text: &str) -> Option<Value> {
    let text = text.trim();
    let (whole, fraction) = text.split_once('.').unwrap_or((text, "0"));
    if fraction.is_empty() || !fraction.bytes().all(|byte| byte == b'0') {
        return None;
    }
    let digits = whole.strip_prefix(['+', '-']).unwrap_or(whole);
    if !digits
        .split('_')
        .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
    {
        return None;
    }
    let digits = digits.trim_start_matches(['0', '_']).replace('_', "");
    if digits.is_empty() {
        return Some(Value::from(0));
    }
    let sign = if whole.starts_with('-') { "-" } else { "" };
    serde_json::from_str(&format!("{sign}{digits}")).ok()
}

fn boolean(value: &Value) -> Conversion {
    const TYPE: (&str, &str) = ("Input should be a valid boolean", "bool_type");
    const PARSING: (&str, &str) = (
        "Input should be a valid boolean, unable to interpret input",
        "bool_parsing",
    );
    let flag = match value {
        Value::Bool(_) => return Ok(None),
        Value::Number(number) => {
            let integral = if is_integer(value) {
                number.as_i64()
            } else {
                number
                    .as_f64()
                    .filter(|n| n.fract() == 0.0 && n.abs() < 2_f64.powi(63))
                    .map(|n| n as i64)
            };
            match integral {
                Some(0) => false,
                Some(1) => true,
                Some(_) => return Err(PARSING),
                None => return Err(TYPE),
            }
        }
        Value::String(text) => match text.to_ascii_lowercase().as_str() {
            "1" | "on" | "t" | "true" | "y" | "yes" => true,
            "0" | "off" | "f" | "false" | "n" | "no" => false,
            _ => return Err(PARSING),
        },
        _ => return Err(TYPE),
    };
    Ok(Some(Value::Bool(flag)))
}
