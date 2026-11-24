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
use privacy::{Budget, EVENT_CHARS, prefix};
use session_tokens::{FileStamp, Tokens, stamp};

const QUEUE_SIZE: usize = 2048;
const QUEUE_CHARS: usize = 4 * 1024 * 1024;
const MAX_LINE: usize = 16 * 1024;
pub const EXIT_FLUSH: Duration = Duration::from_millis(300);

const COMMON: [&str; 8] = [
    "ts", "level", "event", "surface", "pid", "version", "runId", "seq",
];
static RUN_ID: LazyLock<String> =
    LazyLock::new(|| Uuid::new_v4().simple().to_string());
static SEQUENCE: AtomicU64 = AtomicU64::new(1);
static KNOWN: LazyLock<Mutex<Arc<[String]>>> =
    LazyLock::new(|| Mutex::new(Arc::new([])));

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
}

pub struct LogWriter {
    events: SyncSender<Event>,
    done: Receiver<()>,
    queue: Arc<Queue>,
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
            events,
            done,
            queue,
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
        let fields = Field::Object(
            fields
                .into_iter()
                .filter_map(|(key, field)| {
                    if COMMON.contains(&key)
                        || matches!(&field, Field::Scalar(Value::Null))
                    {
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
        };
        if self.events.try_send(event).is_err() {
            self.queue.chars.fetch_sub(chars, Relaxed);
            self.queue.dropped.fetch_add(1, Relaxed);
        }
    }

    /// Close the queue and allow the writer up to `timeout` to finish.
    pub fn finish(self, timeout: Duration) -> bool {
        drop(self.events);
        self.done.recv_timeout(timeout).is_ok()
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

fn json_ascii(value: &Map<String, Value>) -> String {
    let encoded =
        serde_json::to_string(value).expect("serializing JSON to a buffer");
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
