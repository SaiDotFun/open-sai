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
use privacy::{Budget, EVENT_CHARS, prefix};
pub use privacy::{Field, origin};
use session_tokens::{FileStamp, Tokens, stamp};

const QUEUE_SIZE: usize = 2048;
const QUEUE_CHARS: usize = 4 * 1024 * 1024;
const MAX_LINE: usize = 16 * 1024;
pub const EXIT_FLUSH: Duration = Duration::from_millis(300);

const COMMON: [&str; 10] = [
    "ts",
    "level",
    "event",
    "surface",
    "pid",
    "version",
    "runId",
    "seq",
    "schema",
    "commandId",
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
    endpoint_digest: Option<[u8; 32]>,
    command_bound: bool,
}

pub struct LogWriter {
    events: SyncSender<QueueItem>,
    done: Mutex<Receiver<()>>,
    queue: Arc<Queue>,
    command_schema: bool,
}

/// Log events that occur while handling a CLI command or an MCP call.
#[derive(Clone, Copy)]
pub struct EventContext<'a> {
    pub writer: Option<&'a LogWriter>,
    pub surface: Surface,
    pub command: Option<&'a str>,
}

impl EventContext<'_> {
    pub fn event<'a>(
        self,
        name: &str,
        level: Level,
        fields: impl IntoIterator<Item = (&'a str, Field)>,
    ) {
        let Some(writer) = self.writer else { return };
        let command = self
            .command
            .map(|command| ("command", Field::Text(command.into())));
        writer.event(self.surface, name, level, command.into_iter().chain(fields));
    }
}

impl LogWriter {
    /// Start a writer for one Sai home. Filesystem work stays on its thread.
    pub fn start(env: Environment) -> Option<Self> {
        Self::start_bound(env, None)
    }

    /// Start a writer with the v2 CLI command envelope.
    pub fn start_command(env: Environment) -> Option<Self> {
        Self::start_bound(env, Some(Uuid::new_v4().simple().to_string()))
    }

    fn start_bound(env: Environment, command_id: Option<String>) -> Option<Self> {
        if settings::log_disabled_in_env(&env) {
            return None;
        }
        let (events, pending) = mpsc::sync_channel(QUEUE_SIZE);
        let (done_signal, done) = mpsc::channel();
        let queue = Arc::new(Queue::default());
        let command_schema = command_id.is_some();
        let worker_queue = Arc::clone(&queue);
        thread::Builder::new()
            .name("sai-log".into())
            .spawn(move || {
                drain(env, pending, &worker_queue, command_id.as_deref());
                let _ = done_signal.send(());
            })
            .ok()?;
        Some(Self {
            events,
            done: Mutex::new(done),
            queue,
            command_schema,
        })
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
        let mut endpoint_digest = None;
        let mut command_bound = false;
        let fields = Field::Object(
            fields
                .into_iter()
                .filter_map(|(key, field)| {
                    if COMMON.contains(&key) || matches!(&field, Field::Scalar(Value::Null)) {
                        None
                    } else {
                        Some((key.into(), field))
                    }
                })
                .take(65)
                .inspect(|(key, field)| {
                    command_bound |= *key == "command";
                    if self.command_schema
                        && matches!(name, "keeper.request" | "intent.keeper")
                        && *key == "endpoint"
                        && let Field::Endpoint(url) = field
                    {
                        endpoint_digest = endpoint_digest_for(url);
                    }
                })
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
            endpoint_digest,
            command_bound,
        };
        if self.events.try_send(QueueItem::Event(event)).is_err() {
            self.queue.chars.fetch_sub(chars, Relaxed);
            self.queue.dropped.fetch_add(1, Relaxed);
        }
    }

    /// Wait for already queued diagnostics before inspecting log files.
    pub fn flush(&self, timeout: Duration) {
        let (ack, done) = mpsc::channel();
        if self.events.try_send(QueueItem::Flush(ack)).is_ok() {
            let _ = done.recv_timeout(timeout);
        }
    }

    /// Close the queue and allow the writer up to `timeout` to finish.
    pub fn finish(self, timeout: Duration) -> bool {
        drop(self.events);
        self.done
            .into_inner()
            .is_ok_and(|done| done.recv_timeout(timeout).is_ok())
    }
}

enum QueueItem {
    Event(Event),
    Flush(mpsc::Sender<()>),
}

fn drain(env: Environment, events: Receiver<QueueItem>, queue: &Queue, command_id: Option<&str>) {
    let config_path = env.path();
    let log_path = env.home.join("logs");
    let mut config: Option<(Option<FileStamp>, Option<u64>)> = None;
    let mut checked_day = None;
    let mut tokens = Tokens::default();
    for item in events {
        let event = match item {
            QueueItem::Event(event) => event,
            QueueItem::Flush(ack) => {
                let _ = ack.send(());
                continue;
            }
        };
        queue.chars.fetch_sub(event.chars, Relaxed);
        let current_stamp = stamp(&config_path);
        let keep = match config {
            Some((previous, keep)) if previous == current_stamp => keep,
            _ => {
                let keep = if command_id.is_some() {
                    let (enabled, days) = settings::doctor_log_settings(&env);
                    enabled.then(|| days.as_u64().unwrap_or(u64::MAX))
                } else {
                    settings::log_keep_days(&env)
                };
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
            command_id,
        );
        let today: NaiveDate = Utc::now().date_naive();
        if Some(today) != checked_day {
            checked_day = Some(today);
            let _ = directory.prune(keep, today);
        }
    }
}

fn write(
    directory: &LogDir,
    event: Event,
    tokens: &[String],
    queue: &Queue,
    command_id: Option<&str>,
) {
    let mut line = Map::new();
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
    if let Some(command_id) = command_id {
        line.insert("schema".into(), json!(2));
        if event.command_bound {
            line.insert("commandId".into(), json!(command_id));
        }
    }
    if let Some(digest) = event.endpoint_digest
        && let Some(key) = directory.endpoint_key()
    {
        let key = ring::hmac::Key::new(ring::hmac::HMAC_SHA256, &key);
        let signature = ring::hmac::sign(&key, &digest);
        let mut encoded = String::from("h1_");
        for byte in signature.as_ref() {
            let _ = write!(encoded, "{byte:02x}");
        }
        line.insert("endpointId".into(), json!(encoded));
    }
    if let Value::Object(fields) = event.fields.clean(tokens) {
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

fn endpoint_digest_for(endpoint: &str) -> Option<[u8; 32]> {
    let parsed = crate::endpoint::Endpoint::split(endpoint.trim())?;
    let site = origin(endpoint);
    if site == "[redacted url]" {
        return None;
    }
    let path = if parsed.path.is_empty() {
        "/"
    } else {
        &parsed.path
    };
    let mut digest = ring::digest::Context::new(&ring::digest::SHA256);
    digest.update(site.as_bytes());
    digest.update(path.as_bytes());
    digest.update(b"?");
    digest.update(parsed.query.as_bytes());
    digest.finish().as_ref().try_into().ok()
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
