//! Best-effort JSONL events shared by the CLI and MCP server.

mod files;
mod privacy;
mod session_tokens;

use std::{
    fmt::Write as _,
    process,
    sync::{
        Arc, LazyLock, Mutex,
        atomic::{AtomicU64, AtomicUsize, Ordering::Relaxed},
        mpsc::{self, Receiver, SyncSender},
    },
    thread,
    time::Duration,
};

use chrono::{DateTime, NaiveDate, SecondsFormat, Utc};
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::settings::{self, Environment};

use files::LogDir;
pub use privacy::Field;
pub use privacy::origin as endpoint_origin;
use privacy::{Budget, EVENT_CHARS, prefix};
use session_tokens::{FileStamp, Tokens, stamp};

const QUEUE_SIZE: usize = 2048;
const QUEUE_CHARS: usize = 4 * 1024 * 1024;
const MAX_LINE: usize = 16 * 1024;
pub const EXIT_FLUSH: Duration = Duration::from_millis(300);

const COMMON: [&str; 9] = [
    "schema", "ts", "level", "event", "surface", "pid", "version", "runId", "seq",
];
static RUN_ID: LazyLock<String> = LazyLock::new(|| Uuid::new_v4().simple().to_string());
static SEQUENCE: AtomicU64 = AtomicU64::new(1);
static KNOWN: LazyLock<Mutex<Arc<[String]>>> = LazyLock::new(|| Mutex::new(Arc::new([])));

#[derive(Clone, Copy)]
pub enum Surface {
    Cli,
    Mcp,
    Update,
}

impl Surface {
    const ALL: [Self; 3] = [Self::Cli, Self::Mcp, Self::Update];

    fn as_str(self) -> &'static str {
        match self {
            Self::Cli => "cli",
            Self::Mcp => "mcp",
            Self::Update => "update",
        }
    }
}

#[derive(Clone, Copy)]
pub enum Level {
    Info,
    Warn,
    Error,
}

impl Level {
    fn as_str(self) -> &'static str {
        match self {
            Self::Info => "info",
            Self::Warn => "warn",
            Self::Error => "error",
        }
    }
}

/// Retain tokens observed by the process, including ones since signed out.
pub fn remember(tokens: impl IntoIterator<Item = String>) {
    let Ok(mut known) = KNOWN.lock() else { return };
    let mut updated = known.to_vec();
    for token in tokens {
        if !token.is_empty() && !updated.contains(&token) {
            updated.push(token);
        }
    }
    if known.len() != updated.len() {
        updated.sort_by_key(|token| std::cmp::Reverse(token.len()));
        *known = updated.into();
    }
}

/// Redact stored session tokens from text shown to the user.
pub fn redact(home: &std::path::Path, text: &str) -> String {
    Tokens::default().refresh(home);
    let known = KNOWN.lock().unwrap_or_else(|error| error.into_inner());
    privacy::replace_tokens(text, &known)
}

pub fn enabled(env: &Environment) -> bool {
    settings::log_keep_days(env).is_some()
}

/// Use the event writer's privacy rules for Keeper-controlled display fields.
pub fn scrubber(home: &std::path::Path) -> impl Fn(Field) -> Value {
    Tokens::default().refresh(home);
    let known = Arc::clone(&KNOWN.lock().unwrap_or_else(|error| error.into_inner()));
    move |field| field.clean(&known)
}

#[derive(Default)]
struct Queue {
    chars: AtomicUsize,
    dropped: AtomicU64,
}

struct Event {
    timestamp: DateTime<Utc>,
    sequence: u64,
    surface: Surface,
    name: String,
    level: Level,
    fields: Field,
    chars: usize,
    v2: bool,
}

pub struct LogWriter {
    events: Mutex<Option<SyncSender<Event>>>,
    done: Mutex<Receiver<()>>,
    queue: Arc<Queue>,
    command_id: Option<String>,
}

/// Log events that occur while handling a CLI command or an MCP call.
#[derive(Clone, Copy)]
pub struct EventContext<'a> {
    writer: Option<&'a LogWriter>,
    surface: Surface,
    command: Option<&'a str>,
    tool_call: Option<(&'a str, &'a str)>,
}

impl<'a> EventContext<'a> {
    pub const fn new(
        writer: Option<&'a LogWriter>,
        surface: Surface,
        command: Option<&'a str>,
    ) -> Self {
        Self {
            writer,
            surface,
            command,
            tool_call: None,
        }
    }

    pub fn with_tool(mut self, tool: &'a str, call_id: &'a str) -> Self {
        self.tool_call = Some((tool, call_id));
        self
    }

    pub fn event<'b>(
        self,
        name: &str,
        level: Level,
        fields: impl IntoIterator<Item = (&'b str, Field)>,
    ) {
        let Some(writer) = self.writer else { return };
        let command = self
            .command
            .map(|command| ("command", Field::Text(command.into())));
        let call = self.tool_call.into_iter().flat_map(|(tool, call_id)| {
            [
                ("tool", Field::Text(tool.into())),
                ("callId", Field::Text(call_id.into())),
            ]
        });
        writer.event(
            self.surface,
            name,
            level,
            command.into_iter().chain(call).chain(fields),
        );
    }
}

impl LogWriter {
    /// Start a writer for one Sai home. Filesystem work stays on its thread.
    pub fn start(env: Environment) -> Option<Self> {
        if settings::log_disabled_in_env(&env) {
            return None;
        }
        let (events, pending) = mpsc::sync_channel(QUEUE_SIZE);
        let (done_signal, done) = mpsc::channel();
        let queue = Arc::new(Queue::default());
        let worker_queue = Arc::clone(&queue);
        thread::Builder::new()
            .name("sai-log".into())
            .spawn(move || {
                drain(env, pending, &worker_queue);
                let _ = done_signal.send(());
            })
            .ok()?;
        Some(Self {
            events: Mutex::new(Some(events)),
            done: Mutex::new(done),
            queue,
            command_id: None,
        })
    }

    /// Enable Python's v2 event metadata for a ported CLI command.
    pub fn with_v2_command(mut self) -> Self {
        self.command_id = Some(Uuid::new_v4().simple().to_string());
        self
    }

    /// Queue an event without waiting for disk, queue space, or the writer.
    pub fn event<'a>(
        &self,
        surface: Surface,
        name: &str,
        level: Level,
        fields: impl IntoIterator<Item = (&'a str, Field)>,
    ) {
        let timestamp = Utc::now();
        let sequence = SEQUENCE.fetch_add(1, Relaxed);
        let known = KNOWN.try_lock().ok().map(|value| Arc::clone(&value));
        let mut budget = Budget::new();
        let command = self
            .command_id
            .as_ref()
            .map(|id| ("commandId", Field::Text(id.clone())));
        let fields = Field::Object(
            fields
                .into_iter()
                .chain(command)
                .filter_map(|(key, field)| {
                    if COMMON.contains(&key) || matches!(&field, Field::Scalar(Value::Null)) {
                        None
                    } else {
                        Some((key.into(), field))
                    }
                })
                .take(65)
                .collect(),
        )
        .snapshot(&mut budget, known.as_deref().unwrap_or_default(), 0);
        let chars = budget.used().min(EVENT_CHARS);
        let queued = self.queue.chars.fetch_add(chars, Relaxed);
        if queued + chars > QUEUE_CHARS {
            self.queue.chars.fetch_sub(chars, Relaxed);
            self.queue.dropped.fetch_add(1, Relaxed);
            return;
        }
        let event = Event {
            timestamp,
            sequence,
            surface,
            name: prefix(name, 300).to_owned(),
            level,
            fields,
            chars,
            v2: self.command_id.is_some(),
        };
        let sent = self.events.lock().is_ok_and(|events| {
            events
                .as_ref()
                .is_some_and(|sender| sender.try_send(event).is_ok())
        });
        if !sent {
            self.queue.chars.fetch_sub(chars, Relaxed);
            self.queue.dropped.fetch_add(1, Relaxed);
        }
    }

    /// Close the queue and allow the writer up to `timeout` to finish.
    pub fn finish(&self, timeout: Duration) -> bool {
        let Ok(mut events) = self.events.lock() else {
            return false;
        };
        events.take();
        drop(events);
        self.done
            .lock()
            .is_ok_and(|done| done.recv_timeout(timeout).is_ok())
    }
}

fn drain(env: Environment, events: Receiver<Event>, queue: &Queue) {
    let config_path = env.path();
    let log_path = env.home.join("logs");
    let mut config: Option<(Option<FileStamp>, Option<u64>)> = None;
    let mut checked_day = None;
    let mut tokens = Tokens::default();
    for event in events {
        queue.chars.fetch_sub(event.chars, Relaxed);
        let current_stamp = stamp(&config_path);
        let keep = match config {
            Some((previous, keep)) if previous == current_stamp => keep,
            _ => {
                let keep = settings::log_keep_days(&env);
                config = Some((current_stamp, keep));
                keep
            }
        };
        let Some(keep) = keep else { continue };
        let Ok(directory) = LogDir::open(&log_path) else {
            continue;
        };
        tokens.refresh(&env.home);
        let known = KNOWN.lock().ok().map(|value| Arc::clone(&value));
        write(
            &directory,
            event,
            known.as_deref().unwrap_or_default(),
            queue,
        );
        let today: NaiveDate = Utc::now().date_naive();
        if Some(today) != checked_day {
            checked_day = Some(today);
            let _ = directory.prune(keep, today);
        }
    }
}

fn write(directory: &LogDir, event: Event, tokens: &[String], queue: &Queue) {
    let mut line = Map::new();
    if event.v2 {
        line.insert("schema".into(), json!(2));
    }
    line.insert(
        "ts".into(),
        json!(event.timestamp.to_rfc3339_opts(SecondsFormat::Millis, true)),
    );
    line.insert("level".into(), json!(event.level.as_str()));
    line.insert("event".into(), Field::Text(event.name).clean(tokens));
    line.insert("surface".into(), json!(event.surface.as_str()));
    line.insert("pid".into(), json!(process::id()));
    line.insert("version".into(), json!(env!("CARGO_PKG_VERSION")));
    line.insert("runId".into(), json!(RUN_ID.as_str()));
    line.insert("seq".into(), json!(event.sequence));
    let mut fields = event.fields;
    if let Field::Object(entries) = &mut fields
        && let Some(index) = entries
            .iter()
            .position(|(_, field)| matches!(field, Field::EndpointIdentity(_)))
    {
        let (key, field) = entries.swap_remove(index);
        if event.v2
            && let Field::EndpointIdentity(digest) = field
            && let Ok(secret) = directory.endpoint_key()
        {
            let mac = ring::hmac::sign(
                &ring::hmac::Key::new(ring::hmac::HMAC_SHA256, &secret),
                &digest,
            );
            let mut id = String::from("h1_");
            for byte in mac.as_ref() {
                write!(id, "{byte:02x}").expect("writing to a string");
            }
            line.insert(key, json!(id));
        }
    }
    if let Value::Object(fields) = fields.clean(tokens) {
        for (key, field) in fields {
            if !COMMON.contains(&key.as_str()) {
                line.insert(key, field);
            }
        }
    }
    let mut data = encode(&line);
    let dropped = queue.dropped.swap(0, Relaxed);
    if dropped != 0 {
        line.retain(|key, _| COMMON.contains(&key.as_str()));
        line.insert("level".into(), json!("warn"));
        line.insert("event".into(), json!("log.dropped"));
        line.insert("count".into(), json!(dropped));
        data.extend(encode(&line));
    }
    let name = format!(
        "{}-{}.jsonl",
        event.surface.as_str(),
        event.timestamp.date_naive()
    );
    let _ = directory.append(&name, &data);
}

fn json_ascii(value: &Map<String, Value>) -> String {
    let encoded = serde_json::to_string(value).expect("serializing JSON to a buffer");
    let mut escaped = String::with_capacity(encoded.len());
    for c in encoded.chars() {
        if c >= '\u{7f}' {
            for unit in c.encode_utf16(&mut [0; 2]) {
                let _ = write!(escaped, "\\u{unit:04x}");
            }
        } else {
            escaped.push(c);
        }
    }
    escaped
}

fn encode(line: &Map<String, Value>) -> Vec<u8> {
    let mut encoded = json_ascii(line);
    if encoded.len() > MAX_LINE {
        let mut kept = Map::new();
        let mut truncated = Vec::new();
        for (key, value) in line {
            if COMMON.contains(&key.as_str()) {
                kept.insert(key.clone(), value.clone());
            } else {
                truncated.push(key);
            }
        }
        truncated.sort();
        kept.insert("truncated".into(), json!(truncated));
        encoded = json_ascii(&kept);
        // Long field names can otherwise make the fallback line oversized.
        if encoded.len() > MAX_LINE {
            kept.insert("truncated".into(), json!(["[cut]"]));
            encoded = json_ascii(&kept);
        }
    }
    encoded.push('\n');
    encoded.into_bytes()
}
