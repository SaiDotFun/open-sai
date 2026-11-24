//! Match FastMCP tool-level validation errors, including their field order.

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
    let input = Value::Object(args.clone());
    for (name, schema) in properties {
        let Some(value) = args.get(name) else {
            if required.is_some_and(|fields| fields.iter().any(|field| field == name)) {
                issues.push(Issue::new(name, "Field required", "missing", &input));
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
                let choices: Vec<_> = choices.iter().map(python_repr).collect();
                let (last, rest) = choices.split_last().expect("enum has choices");
                let message = format!("Input should be {} or {last}", rest.join(", "));
                issues.push(Issue::new(name, message, "literal_error", value));
            }
            continue;
        }
        let problem = match schema["type"].as_str() {
            Some("string") if !value.is_string() => Some(("string", "string_type")),
            Some("integer") if !is_integer(value) => Some(("integer", "int_type")),
            Some("boolean") if !value.is_boolean() => Some(("boolean", "bool_type")),
            _ => None,
        };
        if let Some((kind, code)) = problem {
            let article = if kind == "integer" { "an" } else { "a" };
            issues.push(Issue::new(
                name,
                format!("Input should be {article} valid {kind}"),
                code,
                value,
            ));
        }
    }
    for (name, value) in &args {
        if !properties.contains_key(name) {
            issues.push(Issue::new(
                name,
                "Extra inputs are not permitted",
                "extra_forbidden",
                value,
            ));
        }
    }
    let Some(first) = issues.first() else {
        return Ok(args);
    };
    if tool.name == "logs_intent" {
        return Err(match first.field {
            "key" => "Argument 'key' must be a string.",
            "offline" => "Argument 'offline' must be a boolean.",
            "addr" => "Argument 'addr' must be a string or null.",
            "before" => "Argument 'before' must be a string or null.",
            _ => "Expected audit arguments 'key' (a string), 'offline' (a boolean), 'addr' (a string or null), 'before' (a string or null).",
        }.into());
    }
    let count = issues.len();
    let plural = if count == 1 { "" } else { "s" };
    let mut message = format!(
        "Error executing tool {}: {count} validation error{plural} for {}Arguments",
        tool.name, tool.name,
    );
    for issue in issues {
        let (kind, input) = (python_type(issue.input), abbreviated_repr(issue.input));
        let Issue {
            field,
            message: detail,
            code,
            ..
        } = issue;
        let _ = write!(
            message,
            "\n{field}\n  {detail} [type={code}, input_value={input}, input_type={kind}]\n    For further information visit {PYDANTIC_ERRORS}/{code}",
        );
    }
    Err(message)
}

struct Issue<'a> {
    field: &'a str,
    message: String,
    code: &'static str,
    input: &'a Value,
}

impl<'a> Issue<'a> {
    fn new(
        field: &'a str,
        message: impl Into<String>,
        code: &'static str,
        input: &'a Value,
    ) -> Self {
        Self {
            field,
            message: message.into(),
            code,
            input,
        }
    }
}

fn is_integer(value: &Value) -> bool {
    value
        .as_number()
        .is_some_and(|number| !number.as_str().contains(['.', 'e', 'E']))
}

fn python_type(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(_) if is_integer(value) => "int",
        Value::Number(_) => "float",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

fn python_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(number) => number.to_string(),
        Value::String(text) => repr(text),
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(python_repr)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(fields) => format!(
            "{{{}}}",
            fields
                .iter()
                .map(|(key, value)| format!("{}: {}", repr(key), python_repr(value)))
                .collect::<Vec<_>>()
                .join(", "),
        ),
    }
}

fn abbreviated_repr(value: &Value) -> String {
    let text = python_repr(value);
    if text.chars().count() <= 50 {
        return text;
    }
    let front: String = text.chars().take(25).collect();
    let back = text
        .char_indices()
        .rev()
        .nth(23)
        .map_or(0, |(index, _)| index);
    format!("{front}...{}", &text[back..])
}
