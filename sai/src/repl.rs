use std::{
    borrow::Cow,
    cell::RefCell,
    env,
    ffi::OsString,
    fs::{self, File},
    future::Future,
    io::{self, IsTerminal, Read, Write},
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use chrono::Utc;
use rustyline::{
    Cmd, ConditionalEventHandler, Config, Context, Editor, Event, EventContext, EventHandler,
    Helper, KeyCode, KeyEvent, Modifiers, Movement, RepeatCount,
    completion::Completer,
    error::ReadlineError,
    highlight::Highlighter,
    hint::{Hinter, HistoryHinter},
    history::MemHistory,
    validate::Validator,
};
use sai_core::{
    errors::{Code, SaiError},
    settings::Flags,
};

thread_local! {
    static INTERRUPT: RefCell<Option<Arc<AtomicBool>>> = const { RefCell::new(None) };
}

#[derive(Default)]
struct HistorySuggestions(HistoryHinter);

impl Helper for HistorySuggestions {}
impl Validator for HistorySuggestions {}

impl Highlighter for HistorySuggestions {
    fn highlight_hint<'a>(&self, hint: &'a str) -> Cow<'a, str> {
        Cow::Owned(format!("\x1b[38;5;240m{hint}\x1b[0m"))
    }
}

impl Completer for HistorySuggestions {
    type Candidate = String;
}

impl Hinter for HistorySuggestions {
    type Hint = String;

    fn hint(&self, line: &str, position: usize, context: &Context<'_>) -> Option<String> {
        self.0.hint(line, position, context)
    }
}

struct HistorySearch {
    lines: Vec<String>,
    selection: Option<HistorySelection>,
}

struct HistorySelection {
    line: String,
    prefix_end: usize,
    index: usize,
}

struct HistoryKeys(Arc<Mutex<HistorySearch>>);

impl ConditionalEventHandler for HistoryKeys {
    fn handle(
        &self,
        event: &Event,
        count: RepeatCount,
        _: bool,
        context: &EventContext<'_>,
    ) -> Option<Cmd> {
        let backwards = event.get(0)?.0 == KeyCode::Up;
        let mut history = self.0.lock().ok()?;
        let selection = history.selection.take();
        let mut selection = selection
            .filter(|entry| {
                context.line() == history.lines[entry.index]
                    && context.pos() == context.line().len()
            })
            .unwrap_or_else(|| HistorySelection {
                line: context.line().into(),
                prefix_end: context.pos(),
                index: history.lines.len(),
            });
        let prefix = &selection.line[..selection.prefix_end];
        let matches = |&index: &usize| history.lines[index].starts_with(prefix);
        let skip = usize::from(count.saturating_sub(1));
        let next = if backwards {
            (0..selection.index).rev().filter(matches).nth(skip)
        } else {
            (selection.index + 1..history.lines.len())
                .filter(matches)
                .nth(skip)
        };
        if let Some(index) = next {
            selection.index = index;
            let line = history.lines[index].clone();
            history.selection = Some(selection);
            Some(Cmd::Replace(Movement::WholeLine, Some(line)))
        } else if !backwards && selection.index < history.lines.len() {
            Some(Cmd::Replace(Movement::WholeLine, Some(selection.line)))
        } else {
            if selection.index < history.lines.len() {
                history.selection = Some(selection);
            }
            Some(Cmd::Noop)
        }
    }
}

pub fn active() -> bool {
    INTERRUPT.with_borrow(Option::is_some)
}

pub fn interrupt_flag() -> Option<Arc<AtomicBool>> {
    INTERRUPT.with_borrow(Clone::clone)
}

pub async fn interruptible<T>(
    operation: impl Future<Output = Result<T, SaiError>>,
) -> Result<T, SaiError> {
    let Some(interrupt) = interrupt_flag() else {
        return operation.await;
    };
    let wait_for_interrupt = async {
        while !interrupt.load(Ordering::Relaxed) {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    };
    tokio::select! {
        biased;
        () = wait_for_interrupt => Err(SaiError::new(Code::InvalidRequest, "Aborted")),
        result = operation => result
    }
}

struct InterruptRegistration(signal_hook::SigId);

impl Drop for InterruptRegistration {
    fn drop(&mut self) {
        signal_hook::low_level::unregister(self.0);
    }
}

fn argument_error(message: &str) -> SaiError {
    SaiError::new(Code::InvalidRequest, message)
}

// This is the POSIX shlex grammar used by Python's REPL. In a quoted
// string only a backslash before a quote or backslash is removed.
fn arguments(line: &str) -> Result<Vec<String>, SaiError> {
    let mut args = Vec::new();
    let mut input = line.chars().peekable();
    while let Some(&next) = input.peek() {
        if matches!(next, ' ' | '\t' | '\r' | '\n') {
            input.next();
            continue;
        }
        let mut word = String::new();
        let mut quote = None;
        while let Some(current) = input.next() {
            if let Some(delimiter) = quote {
                if current == delimiter {
                    quote = None;
                } else if current == '\\' && delimiter == '"' {
                    let next = input
                        .next()
                        .ok_or_else(|| argument_error("No escaped character"))?;
                    if !matches!(next, '"' | '\\') {
                        word.push('\\');
                    }
                    word.push(next);
                } else {
                    word.push(current);
                }
            } else {
                match current {
                    ' ' | '\t' | '\r' | '\n' => break,
                    '\'' | '"' => quote = Some(current),
                    '\\' => word.push(
                        input
                            .next()
                            .ok_or_else(|| argument_error("No escaped character"))?,
                    ),
                    other => word.push(other),
                }
            }
        }
        if quote.is_some() {
            return Err(argument_error("No closing quotation"));
        }
        args.push(word);
    }
    if args
        .first()
        .is_some_and(|command| matches!(command.as_str(), "help" | "?"))
    {
        args.remove(0);
        args.push("--help".into());
    }
    Ok(args)
}

fn banner(color: bool) {
    const DARK: &str = "\x1b[38;5;240m";
    const LIGHT: &str = "\x1b[38;5;250m";
    const BRAND: &str = "\x1b[38;5;80m\x1b[1m";
    const ACCENT: &str = "\x1b[38;5;75m\x1b[1m";
    const MAGENTA: &str = "\x1b[38;5;176m";
    let paint = |code: &str, text: &str| {
        if color {
            format!("{code}{text}\x1b[0m")
        } else {
            text.to_owned()
        }
    };
    let border = paint(DARK, "│");
    let row = |text: String, width: usize| {
        println!(
            "{border}{text}{:padding$}{border}",
            "",
            padding = 72_usize.saturating_sub(width)
        );
    };
    println!("{}", paint(DARK, &format!("╭{}╮", "─".repeat(72))));
    row(
        format!(
            " {}  {} {} {}",
            paint(BRAND, "◆"),
            paint(BRAND, "Sai"),
            paint(DARK, "·"),
            paint(ACCENT, "Trader CLI")
        ),
        20,
    );
    let version = format!("   v{}", env!("CARGO_PKG_VERSION"));
    row(format!(" {}", paint(DARK, &version)), version.len() + 1);
    for (label, detail) in [
        (
            "Install:",
            "npx skills add SaiDotFun/open-sai --skill sai -g -y",
        ),
        ("Global skill:", "~/.agents/skills/sai/SKILL.md"),
    ] {
        row(
            format!(
                " {} {} {}",
                paint(MAGENTA, "◇"),
                paint(DARK, label),
                paint(LIGHT, detail)
            ),
            4 + label.len() + detail.len(),
        );
    }
    row(String::new(), 0);
    let hint = "   Type help for commands, quit to exit";
    row(format!(" {}", paint(DARK, hint)), hint.len() + 1);
    println!(
        "{}\n\nEnter a command, 'help', or 'exit'.",
        paint(DARK, &format!("╰{}╯", "─".repeat(72)))
    );
}

fn history_directory(home: &Path) -> io::Result<std::path::PathBuf> {
    let trader = home.join("trader");
    #[cfg(unix)]
    {
        use rustix::{
            fs::{Mode, OFlags, open},
            process::geteuid,
        };
        use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
        let mut builder = fs::DirBuilder::new();
        builder.recursive(true).mode(0o700);
        builder.create(home)?;
        let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW;
        let profile = File::from(open(home, flags, Mode::empty())?);
        if profile.metadata()?.uid() != geteuid().as_raw() {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        builder.create(&trader)?;
        let directory = File::from(open(&trader, flags, Mode::empty())?);
        if directory.metadata()?.uid() != geteuid().as_raw() {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        directory.set_permissions(fs::Permissions::from_mode(0o700))?;
    }
    #[cfg(not(unix))]
    fs::create_dir_all(&trader)?;
    Ok(trader)
}

fn open_history(directory: &Path) -> io::Result<File> {
    let path = directory.join("history");
    #[cfg(unix)]
    {
        use rustix::{
            fs::{Mode, OFlags, open},
            process::geteuid,
        };
        use std::os::unix::fs::{MetadataExt, PermissionsExt};

        let flags =
            OFlags::RDWR | OFlags::APPEND | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::NONBLOCK;
        let file = File::from(open(path, flags, Mode::RUSR | Mode::WUSR)?);
        let info = file.metadata()?;
        if !info.is_file() || info.nlink() != 1 || info.uid() != geteuid().as_raw() {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
        Ok(file)
    }
    #[cfg(not(unix))]
    fs::OpenOptions::new()
        .read(true)
        .append(true)
        .create(true)
        .open(path)
}

fn load_history(
    file: &mut File,
    editor: &mut Editor<HistorySuggestions, MemHistory>,
) -> Result<(), SaiError> {
    let mut stored = Vec::new();
    file.read_to_end(&mut stored)
        .map_err(|_| SaiError::internal())?;
    let stored = String::from_utf8_lossy(&stored);
    let mut entry = Vec::new();
    for line in stored.lines().chain([""]) {
        if let Some(line) = line.strip_prefix('+') {
            entry.push(line);
        } else if !entry.is_empty() {
            editor
                .add_history_entry(entry.join("\n"))
                .map_err(|_| SaiError::internal())?;
            entry.clear();
        }
    }
    Ok(())
}

fn record_history(history: &mut File, line: &str) -> io::Result<()> {
    let time = Utc::now().format("%Y-%m-%d %H:%M:%S%.6f");
    let mut entry = format!("\n# {time}\n");
    for line in line.split('\n') {
        entry.push('+');
        entry.push_str(line);
        entry.push('\n');
    }
    history.write_all(entry.as_bytes())
}

pub fn run(
    home: &Path,
    flags: &Flags,
    account: Option<&str>,
    mut dispatch: impl FnMut(Vec<OsString>),
) -> Result<(), SaiError> {
    let directory = history_directory(home).map_err(|_| SaiError::internal())?;
    banner(
        io::stdout().is_terminal() && env::var_os("NO_COLOR").is_none_or(|value| value.is_empty()),
    );
    let terminal = io::stdin().is_terminal();
    let mut session = if terminal {
        let config = Config::builder()
            .max_history_size(usize::MAX)
            .map_err(|_| SaiError::internal())?
            .build();
        let mut editor = Editor::with_config(config).map_err(|_| SaiError::internal())?;
        editor.set_helper(Some(HistorySuggestions::default()));
        let mut history = open_history(&directory).map_err(|_| SaiError::internal())?;
        load_history(&mut history, &mut editor)?;
        let search = Arc::new(Mutex::new(HistorySearch {
            lines: editor.history().into_iter().cloned().collect(),
            selection: None,
        }));
        for key in [KeyCode::Up, KeyCode::Down] {
            editor.bind_sequence(
                KeyEvent(key, Modifiers::NONE),
                EventHandler::Conditional(Box::new(HistoryKeys(Arc::clone(&search)))),
            );
        }
        Some((editor, history, search))
    } else {
        None
    };
    // The terminal installs its own SIGINT handler during editor creation.
    // Register afterward so command cancellation and terminal signals coexist.
    let interrupt = Arc::new(AtomicBool::new(false));
    let _registration = InterruptRegistration(
        signal_hook::flag::register(signal_hook::consts::SIGINT, Arc::clone(&interrupt))
            .map_err(|_| SaiError::internal())?,
    );
    let mut forwarded = vec![OsString::from("sai")];
    for (option, value) in [
        ("--endpoint", flags.endpoint.as_deref()),
        ("--ws-endpoint", flags.ws_endpoint.as_deref()),
        ("--lcd-endpoint", flags.lcd_endpoint.as_deref()),
        ("--as", account.filter(|account| !account.is_empty())),
    ] {
        if let Some(value) = value {
            forwarded.extend([OsString::from(option), OsString::from(value)]);
        }
    }

    loop {
        let line = if let Some((editor, history, search)) = &mut session {
            search.lock().map_err(|_| SaiError::internal())?.selection = None;
            match editor.readline("◆ sai ❯ ") {
                Ok(line) => {
                    if !line.is_empty()
                        && editor
                            .add_history_entry(&line)
                            .map_err(|_| SaiError::internal())?
                    {
                        record_history(history, &line).map_err(|_| SaiError::internal())?;
                        search
                            .lock()
                            .map_err(|_| SaiError::internal())?
                            .lines
                            .push(line.clone());
                    }
                    line
                }
                Err(ReadlineError::Interrupted) => continue,
                #[cfg(unix)]
                Err(ReadlineError::Signal(rustyline::error::Signal::Interrupt)) => continue,
                Err(ReadlineError::Eof) => {
                    println!();
                    break;
                }
                Err(_) => return Err(SaiError::internal()),
            }
        } else {
            print!("> sai ❯ ");
            io::stdout().flush().map_err(|_| SaiError::internal())?;
            let mut line = String::new();
            match io::stdin().read_line(&mut line) {
                Ok(0) => {
                    println!();
                    break;
                }
                Ok(_) => line,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(_) => return Err(SaiError::internal()),
            }
        };
        let line = line.trim();
        if matches!(line, "exit" | "quit") {
            break;
        }
        match arguments(line) {
            Ok(arguments) if !arguments.is_empty() => {
                interrupt.store(false, Ordering::Relaxed);
                let previous = INTERRUPT.replace(Some(Arc::clone(&interrupt)));
                dispatch(
                    forwarded
                        .iter()
                        .cloned()
                        .chain(arguments.into_iter().map(OsString::from))
                        .collect(),
                );
                INTERRUPT.set(previous);
            }
            Ok(_) => {}
            Err(error) => eprintln!("Error: {}", error.message),
        }
    }
    Ok(())
}
