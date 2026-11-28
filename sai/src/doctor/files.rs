use std::{
    fs::{DirBuilder, File, Metadata},
    io::{self, BufRead, BufReader, Read},
    os::unix::fs::{DirBuilderExt, MetadataExt},
    path::{Path, PathBuf},
};

use chrono::NaiveDate;
use rustix::{
    fs::{self as unix, AtFlags, Dir, FileType, Mode, OFlags},
    process::{geteuid, getuid},
};
use sai_core::settings::Environment;
use serde_json::{Value, json};

use super::{Check, intents, plural, shell_quote};

pub(super) const SYMLINK_FIX: &str = "Replace the symlink with a real file or directory by hand";
const SHARED_HOME_FIX: &str = "This Sai home may be shared. Stop all Sai CLI and MCP processes before moving the profile to a dedicated directory. Keep the trader data, including the intent ledger. Set SAI_HOME to the new directory for the CLI and in every MCP client configuration before restarting clients or resuming trading.";
const TRADER_FILES: &[&str] = &[
    "sessions.json",
    "session.json",
    "intents.sqlite3",
    "intents.sqlite3-wal",
    "intents.sqlite3-shm",
    "intents.sqlite3-journal",
    "history",
];
const PRUNE_MARKER: &str = ".pruned";
const GROUP_OR_OTHER: u32 = 0o077;
const SHARED: u32 = 0o3022;
const DIR_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

pub(super) fn trader_links(home: &Path, files: &[&str]) -> Vec<PathBuf> {
    let root = home.join("trader");
    if home.is_symlink() {
        return vec![home.to_owned()];
    }
    if root.is_symlink() {
        return vec![root];
    }
    files
        .iter()
        .map(|name| root.join(name))
        .filter(|path| path.is_symlink())
        .collect()
}

pub(super) fn path_list(paths: &[PathBuf], separator: &str) -> String {
    paths
        .iter()
        .map(|path| path.to_string_lossy())
        .collect::<Vec<_>>()
        .join(separator)
}

struct Loose {
    path: PathBuf,
    mode: u32,
}

impl Loose {
    fn new(path: PathBuf) -> Option<Self> {
        let mode = path.symlink_metadata().ok()?.mode() & 0o7777;
        (mode & GROUP_OR_OTHER != 0).then_some(Self { path, mode })
    }

    fn fix(&self) -> String {
        format!("chmod go-rwx {}", shell_quote(&self.path.to_string_lossy()))
    }
}

fn loose_modes(root: &Path, names: &[&str]) -> Vec<Loose> {
    std::iter::once(root.to_owned())
        .chain(names.iter().map(|name| root.join(name)))
        .filter_map(Loose::new)
        .collect()
}

fn loose_details(loose: &[Loose]) -> serde_json::Value {
    json!(
        loose
            .iter()
            .map(|item| json!({"path": item.path, "mode": format!("{:o}", item.mode)}))
            .collect::<Vec<_>>()
    )
}

fn open_directory(path: &Path) -> io::Result<File> {
    Ok(File::from(unix::open(path, DIR_FLAGS, Mode::empty())?))
}

fn require_owner(dir: &File, path: &Path) -> io::Result<()> {
    if dir.metadata()?.uid() != getuid().as_raw() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!("{} belongs to another user", path.display()),
        ));
    }
    Ok(())
}

pub(super) fn verify_profile_owner(home: &Path) -> io::Result<()> {
    match open_directory(home) {
        Ok(dir) => require_owner(&dir, home),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

fn chmod(path: &Path, file: &File, info: &Metadata) -> io::Result<Option<String>> {
    let mode = info.mode() & 0o7777;
    let new = mode & !GROUP_OR_OTHER;
    if mode == new {
        return Ok(None);
    }
    unix::fchmod(file, Mode::from_bits_truncate(new as _))?;
    Ok(Some(format!(
        "chmod {} {mode:o} -> {new:o}",
        path.display()
    )))
}

fn tighten(
    root: &Path,
    names: &[&str],
    apply: bool,
    eligible: impl FnOnce(&Metadata) -> bool,
) -> (Vec<String>, Vec<String>, bool) {
    let mut changes = Vec::new();
    let mut failed = Vec::new();
    let fix = |path: &Path| format!("chmod go-rwx {}", shell_quote(&path.to_string_lossy()));
    let repair = |path: &Path, file: &File, info: &Metadata, eligible: bool| {
        if !eligible || info.mode() & GROUP_OR_OTHER == 0 {
            Ok(None)
        } else if apply {
            chmod(path, file, info)
        } else {
            Ok(Some(fix(path)))
        }
    };
    let open = || -> io::Result<File> {
        let home = root.parent().ok_or(io::ErrorKind::InvalidInput)?;
        let name = root.file_name().ok_or(io::ErrorKind::InvalidInput)?;
        let parent = open_directory(home)?;
        require_owner(&parent, home)?;
        Ok(File::from(unix::openat(
            parent,
            name,
            DIR_FLAGS,
            Mode::empty(),
        )?))
    };
    let Ok(dir) = open() else {
        return (changes, vec![fix(root)], false);
    };
    let Ok(info) = dir.metadata() else {
        return (changes, vec![fix(root)], false);
    };
    if info.uid() != getuid().as_raw() {
        return (
            changes,
            vec![format!(
                "{} belongs to another user; fix by hand",
                root.display()
            )],
            false,
        );
    }
    let eligible = eligible(&info);
    if !root
        .symlink_metadata()
        .is_ok_and(|current| same_file(&current, &info))
    {
        return (
            changes,
            vec![format!(
                "{} changed during the check; run sai doctor again",
                root.display()
            )],
            false,
        );
    }
    match repair(root, &dir, &info, eligible) {
        Ok(change) => changes.extend(change),
        Err(_) => failed.push(fix(root)),
    }
    for name in names {
        let path = root.join(name);
        let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC;
        let file = match unix::openat(&dir, *name, flags, Mode::empty()) {
            Ok(fd) => File::from(fd),
            Err(rustix::io::Errno::NOENT) => continue,
            Err(_) => {
                failed.push(fix(&path));
                continue;
            }
        };
        match file.metadata() {
            Ok(info) if info.uid() != getuid().as_raw() => failed.push(format!(
                "{} belongs to another user; fix by hand",
                path.display()
            )),
            Ok(info) if info.nlink() > 1 => failed.push(format!(
                "{} has other hard links; fix by hand",
                path.display()
            )),
            Ok(info) if info.is_file() => match repair(&path, &file, &info, eligible) {
                Ok(change) => changes.extend(change),
                Err(_) => failed.push(fix(&path)),
            },
            _ => failed.push(fix(&path)),
        }
    }
    (changes, failed, eligible)
}

fn same_file(left: &Metadata, right: &Metadata) -> bool {
    left.dev() == right.dev() && left.ino() == right.ino()
}

#[derive(Clone, Copy)]
enum ProfileDir {
    Root,
    Trader,
    Logs,
    Cache,
    Wheels,
}

impl ProfileDir {
    fn child(self, name: &str) -> Option<Self> {
        match (self, name) {
            (Self::Root, "trader") => Some(Self::Trader),
            (Self::Root, "logs") => Some(Self::Logs),
            (Self::Root, "cache") => Some(Self::Cache),
            (Self::Cache, "wheels") => Some(Self::Wheels),
            _ => None,
        }
    }

    fn file(self, name: &str) -> bool {
        match self {
            Self::Root => [
                "config.toml",
                ".config.lock",
                "update.json",
                "update.lock",
                "update-cache.lock",
            ]
            .contains(&name),
            Self::Trader => TRADER_FILES.contains(&name) || name == "sessions.lock",
            Self::Logs => {
                [PRUNE_MARKER, ".endpoint-key", ".endpoint-key.lock"].contains(&name)
                    || log_name(name).is_some()
            }
            Self::Cache => false,
            Self::Wheels => name
                .strip_prefix("sai_cli-")
                .and_then(|name| name.strip_suffix("-py3-none-any.whl"))
                .and_then(super::version_parts)
                .is_some(),
        }
    }

    fn has_data(
        self,
        dir: &File,
        path: &Path,
        uid: u32,
        allow_loose_trader: bool,
        expected_child: Option<(&str, &Metadata)>,
    ) -> Option<bool> {
        let mut has_data = false;
        let mut matched_child = expected_child.is_none();
        for item in Dir::read_from(dir).ok()? {
            let item = item.ok()?;
            let name = item.file_name();
            let text = name.to_str().ok()?;
            if matches!(text, "." | "..") {
                continue;
            }
            let info = unix::statat(dir, name, AtFlags::SYMLINK_NOFOLLOW).ok()?;
            if info.st_uid != uid {
                return None;
            }
            let mut mode: u32 = info.st_mode as _;
            if allow_loose_trader && matches!(self, Self::Root) && text == "trader" {
                mode &= !GROUP_OR_OTHER;
            }
            match FileType::from_raw_mode(info.st_mode) {
                FileType::Directory if mode & SHARED == 0 => {
                    let child = self.child(text)?;
                    let fd = File::from(unix::openat(dir, name, DIR_FLAGS, Mode::empty()).ok()?);
                    let opened = fd.metadata().ok()?;
                    let device: u64 = info.st_dev as _;
                    if device != opened.dev() || info.st_ino != opened.ino() {
                        return None;
                    }
                    if let Some((name, expected)) = expected_child
                        && text == name
                    {
                        if !same_file(&opened, expected) {
                            return None;
                        }
                        matched_child = true;
                    }
                    has_data |= child.has_data(&fd, &path.join(text), uid, false, None)?;
                }
                FileType::RegularFile if self.file(text) => {
                    if matches!(self, Self::Logs)
                        && let Some((_, surface)) = log_name(text)
                    {
                        let file = open_regular_at(dir, name)?;
                        let opened = file.metadata().ok()?;
                        let device: u64 = info.st_dev as _;
                        if opened.nlink() != 1
                            || device != opened.dev()
                            || info.st_ino != opened.ino()
                        {
                            return None;
                        }
                        let mut line = Vec::new();
                        BufReader::new(file.take((sai_core::log::MAX_LINE + 1) as u64))
                            .read_until(b'\n', &mut line)
                            .ok()?;
                        if !sai_core::log::recognizes_line(&line, surface) {
                            return None;
                        }
                    }
                    if matches!(self, Self::Trader)
                        && matches!(text, "sessions.json" | "session.json" | "intents.sqlite3")
                    {
                        let flags =
                            OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC;
                        let file = File::from(unix::openat(dir, name, flags, Mode::empty()).ok()?);
                        let opened = file.metadata().ok()?;
                        let device: u64 = info.st_dev as _;
                        if opened.nlink() != 1
                            || opened.len() == 0
                            || device != opened.dev()
                            || info.st_ino != opened.ino()
                        {
                            return None;
                        }
                        let evidence = if text == "intents.sqlite3" {
                            intents::has_ledger_schema(path, &opened)
                        } else {
                            let envelope: Value = serde_json::from_reader(file).ok()?;
                            sai_core::storage::is_session_envelope(text, &envelope)
                        };
                        let after = unix::statat(dir, name, AtFlags::SYMLINK_NOFOLLOW).ok()?;
                        if !evidence || after.st_dev != info.st_dev || after.st_ino != info.st_ino {
                            return None;
                        }
                        has_data = true;
                    }
                }
                _ => return None,
            }
        }
        matched_child.then_some(has_data)
    }
}

fn dedicated_profile(
    path: &Path,
    allow_loose_trader: bool,
    expected_child: Option<(&str, &Metadata)>,
) -> Option<(File, Metadata)> {
    let dir = open_directory(path).ok()?;
    let info = dir.metadata().ok()?;
    let mode = info.mode();
    if info.uid() != getuid().as_raw()
        || mode & SHARED != 0
        || !ProfileDir::Root.has_data(&dir, path, info.uid(), allow_loose_trader, expected_child)?
        || dir.metadata().ok()?.mode() != mode
    {
        return None;
    }
    let current = path.symlink_metadata().ok()?;
    same_file(&info, &current).then_some((dir, info))
}

pub(super) fn diagnostic_log_dir(home: &Path) -> io::Result<Option<File>> {
    verify_profile_owner(home)?;
    let root = home.join("logs");
    let dir = match open_directory(&root) {
        Ok(dir) => dir,
        Err(error)
            if error.kind() == io::ErrorKind::NotFound
                && matches!(root.symlink_metadata(), Err(error) if error.kind() == io::ErrorKind::NotFound) =>
        {
            return Ok(None);
        }
        Err(error) => return Err(error),
    };
    let info = dir.metadata()?;
    require_owner(&dir, &root)?;
    let private = info.mode() & GROUP_OR_OTHER == 0
        && Dir::read_from(&dir).is_ok_and(|mut entries| {
            entries.all(|item| {
                item.is_ok_and(|item| {
                    let name = item.file_name();
                    name.to_str().is_ok_and(|text| {
                        log_name(text).is_none()
                            || unix::statat(&dir, name, AtFlags::SYMLINK_NOFOLLOW).is_ok_and(
                                |stat| {
                                    !Mode::from_raw_mode(stat.st_mode)
                                        .intersects(Mode::RWXG | Mode::RWXO)
                                },
                            )
                    })
                })
            })
        })
        && ProfileDir::Logs
            .has_data(&dir, &root, info.uid(), false, None)
            .is_some()
        && !home.is_symlink()
        && root
            .symlink_metadata()
            .is_ok_and(|current| same_file(&info, &current));
    if private || dedicated_profile(home, true, Some(("logs", &info))).is_some() {
        Ok(Some(dir))
    } else {
        Err(io::ErrorKind::PermissionDenied.into())
    }
}

fn repair_profile_root(path: &Path, apply: bool) -> Option<String> {
    let (dir, info) = dedicated_profile(path, !apply, None)?;
    if info.mode() & GROUP_OR_OTHER == 0 {
        return None;
    }
    if apply {
        chmod(path, &dir, &info).ok().flatten()
    } else {
        Some(Loose::new(path.to_owned())?.fix())
    }
}

pub(super) fn home_check(home: &Path, fix: bool) -> Check {
    let root = home.join("trader");
    let links = trader_links(home, TRADER_FILES);
    let home_loose = if links.contains(&home.to_owned()) {
        None
    } else {
        Loose::new(home.to_owned())
    };
    let trader_loose = if links.is_empty() {
        loose_modes(&root, TRADER_FILES)
    } else {
        Vec::new()
    };
    let all_loose = home_loose
        .iter()
        .chain(&trader_loose)
        .map(|item| json!({"path": item.path, "mode": format!("{:o}", item.mode)}))
        .collect::<Vec<_>>();
    let mut details = json!({"path": root, "saiHome": home, "looseModes": all_loose});
    let mut home_fix = if home_loose.is_some() {
        SHARED_HOME_FIX
    } else {
        ""
    };
    if !links.is_empty() {
        details["symlinks"] = json!(links);
        let fix = if home_fix.is_empty() {
            SYMLINK_FIX.to_owned()
        } else {
            format!("{SYMLINK_FIX}. {home_fix}")
        };
        return Check::new(
            "home",
            "fail",
            format!("{}: profile path is a symlink", path_list(&links, ", ")),
        )
        .fix(fix)
        .details(details);
    }
    if fix && let Err(error) = verify_profile_owner(home) {
        return Check::new("home", "fail", error.to_string())
            .fix(SHARED_HOME_FIX)
            .details(details);
    }
    let exists = root.is_dir();
    let mut changes = Vec::new();
    let mut failed = Vec::new();
    if exists && !trader_loose.is_empty() {
        let eligible;
        (changes, failed, eligible) = tighten(&root, TRADER_FILES, fix, |opened| {
            dedicated_profile(home, true, Some(("trader", opened))).is_some()
        });
        if !eligible && failed.is_empty() {
            home_fix = SHARED_HOME_FIX;
        }
    }
    if home_loose.is_some()
        && failed.is_empty()
        && let Some(change) = repair_profile_root(home, fix)
    {
        if fix {
            changes.push(change);
        } else {
            changes.insert(0, change);
        }
        home_fix = "";
    }
    if !fix {
        if !all_loose.is_empty() {
            let (status, summary) = if failed.is_empty() {
                (
                    "warn",
                    format!(
                        "{} in {} accessible by other users",
                        plural(all_loose.len(), "path"),
                        home.display()
                    ),
                )
            } else {
                (
                    "fail",
                    format!("Could not make {} private", plural(failed.len(), "path")),
                )
            };
            let guidance = [failed, changes].concat().join("; ");
            return Check::new("home", status, summary)
                .fix(format!("{home_fix} {guidance}").trim())
                .details(details);
        }
        if !exists {
            return Check::new(
                "home",
                "pass",
                format!(
                    "{} does not exist yet; sai creates it on first use",
                    root.display()
                ),
            )
            .details(details);
        }
    }
    if fix && !exists {
        let home_exists = home.is_dir();
        if let Err(error) = create_trader(home) {
            return super::crashed("home", io_class(&error));
        }
        if !home_exists {
            changes.push(format!("created {} with mode 700", home.display()));
        }
        changes.push(format!("created {} with mode 700", root.display()));
    }
    let check = if !failed.is_empty() {
        Check::new(
            "home",
            "fail",
            format!("Could not make {} private", plural(failed.len(), "path")),
        )
        .fix(format!("{home_fix} {}", failed.join("; ")).trim())
    } else if !home_fix.is_empty() {
        Check::new(
            "home",
            "warn",
            format!("{} is accessible by other users", home.display()),
        )
        .fix(home_fix)
    } else if changes.is_empty() {
        Check::new(
            "home",
            "pass",
            format!("{} and {} are private", home.display(), root.display()),
        )
    } else {
        Check::new(
            "home",
            "fixed",
            format!(
                "Made {} in {} private",
                plural(changes.len(), "path"),
                home.display()
            ),
        )
    };
    check.details(details).changes(changes)
}

fn create_trader(home: &Path) -> io::Result<()> {
    DirBuilder::new().mode(0o700).recursive(true).create(home)?;
    let dir = open_directory(home)?;
    require_owner(&dir, home)?;
    let mode = Mode::RUSR | Mode::WUSR | Mode::XUSR;
    match unix::mkdirat(&dir, "trader", mode) {
        Ok(()) | Err(rustix::io::Errno::EXIST) => {}
        Err(error) => return Err(error.into()),
    }
    let trader = unix::openat(dir, "trader", DIR_FLAGS, Mode::empty())?;
    if unix::fstat(&trader)?.st_uid != getuid().as_raw() {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    unix::fchmod(trader, mode)?;
    Ok(())
}

fn io_class(error: &io::Error) -> &'static str {
    match error.kind() {
        io::ErrorKind::PermissionDenied => "PermissionError",
        io::ErrorKind::AlreadyExists => "FileExistsError",
        io::ErrorKind::NotADirectory => "NotADirectoryError",
        io::ErrorKind::NotFound => "FileNotFoundError",
        _ => "OSError",
    }
}

fn open_regular_at(dir: &File, name: &std::ffi::CStr) -> Option<File> {
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC;
    let file = File::from(unix::openat(dir, name, flags, Mode::empty()).ok()?);
    file.metadata().ok()?.is_file().then_some(file)
}

fn log_name(name: &str) -> Option<(NaiveDate, &str)> {
    let (surface, date) = name.strip_suffix(".jsonl")?.split_once('-')?;
    if !matches!(surface, "cli" | "mcp" | "update") || date.len() != 10 {
        return None;
    }
    let parsed = NaiveDate::parse_from_str(date, "%Y-%m-%d").ok()?;
    (parsed.format("%Y-%m-%d").to_string() == date && !date.starts_with("0000"))
        .then_some((parsed, surface))
}

pub(super) fn logs_check(env: &Environment, fix: bool, (on, keep_days): (bool, Value)) -> Check {
    let root = env.home.join("logs");
    if env.home.is_symlink() {
        return Check::new(
            "logs",
            "fail",
            format!("{}: Sai home is a symlink", env.home.display()),
        )
        .fix(SYMLINK_FIX)
        .details(json!({"path": root, "symlinks": [env.home]}));
    }
    let mut files = Vec::new();
    let mut refused = Vec::new();
    if let Ok(dir) = open_directory(&root)
        && let Ok(entries) = Dir::read_from(&dir)
    {
        for entry in entries.flatten() {
            let name = entry.file_name();
            let Ok(name_str) = name.to_str() else {
                continue;
            };
            let parsed = log_name(name_str);
            if parsed.is_none() && name_str != PRUNE_MARKER {
                continue;
            }
            let Ok(info) = unix::statat(&dir, name, AtFlags::SYMLINK_NOFOLLOW) else {
                continue;
            };
            let reason = match FileType::from_raw_mode(info.st_mode) {
                FileType::Symlink => Some("a symlink"),
                FileType::RegularFile if info.st_nlink > 1 => Some("hard-linked elsewhere"),
                FileType::RegularFile if info.st_uid != geteuid().as_raw() => {
                    Some("owned by another user")
                }
                FileType::RegularFile => None,
                _ => Some("not a regular file"),
            };
            if let Some(reason) = reason {
                refused.push((root.join(name_str), reason));
            }
            if let Some((date, surface)) = parsed
                && FileType::from_raw_mode(info.st_mode) == FileType::RegularFile
            {
                files.push((date, surface.to_owned(), name_str.to_owned()));
            }
        }
    }
    let mut details =
        json!({"path": root, "enabled": on, "keepDays": keep_days, "files": files.len()});
    if root.is_symlink() {
        details["symlinks"] = json!([root]);
        return Check::new(
            "logs",
            "fail",
            format!(
                "{}: logs path is a symlink; doctor changes nothing there",
                root.display()
            ),
        )
        .fix(SYMLINK_FIX)
        .details(details);
    }
    let marker = root.join(PRUNE_MARKER);
    if marker.exists() && marker.is_symlink() {
        details["symlinks"] = json!([marker]);
        return Check::new(
            "logs",
            "fail",
            format!(
                "{}: logs path is a symlink; doctor changes nothing there",
                marker.display()
            ),
        )
        .fix(SYMLINK_FIX)
        .details(details);
    }
    if !refused.is_empty() {
        let listed = refused
            .iter()
            .map(|(path, reason)| format!("{} ({reason})", path.display()))
            .collect::<Vec<_>>()
            .join(", ");
        let paths = refused
            .into_iter()
            .map(|(path, _)| path)
            .collect::<Vec<_>>();
        details["refused"] = json!(paths);
        return Check::new("logs", "fail", format!("sai will not write to {listed}"))
            .fix(format!("Move or remove: {}", path_list(&paths, " ")))
            .details(details);
    }
    let off = if on {
        ""
    } else {
        "; logging is off (logs.enabled)"
    };
    if !root.is_dir() {
        return if root.exists() {
            Check::new(
                "logs",
                "fail",
                format!(
                    "{} is not a directory, so sai cannot write logs",
                    root.display()
                ),
            )
            .fix(format!(
                "Move or remove {}; sai creates the directory on its next run",
                root.display()
            ))
            .details(details)
        } else {
            Check::new(
                "logs",
                "pass",
                format!("No logs yet at {}{off}", root.display()),
            )
            .details(details)
        };
    }
    files.sort_unstable();
    let mut names = files
        .iter()
        .map(|(_, _, name)| name.as_str())
        .collect::<Vec<_>>();
    if marker.exists() {
        names.push(PRUNE_MARKER);
    }
    let loose = loose_modes(&root, &names);
    details["looseModes"] = loose_details(&loose);
    let private = || {
        Check::new(
            "logs",
            "pass",
            format!("{} is private{off}", root.display()),
        )
    };
    let mut changes = Vec::new();
    let check = if loose.is_empty() {
        private()
    } else {
        let (repairs, failed, eligible) = tighten(&root, &names, fix, |opened| {
            dedicated_profile(&env.home, true, Some(("logs", opened))).is_some()
        });
        if fix {
            changes = repairs.clone();
        }
        if !failed.is_empty() {
            let count = failed.len();
            let guidance = if fix {
                failed
            } else {
                [failed, repairs].concat()
            };
            Check::new(
                "logs",
                "fail",
                format!("Could not make {} private", plural(count, "path")),
            )
            .fix(guidance.join("; "))
        } else if !eligible || !fix {
            Check::new(
                "logs",
                "warn",
                format!(
                    "{} under {} readable by other users",
                    plural(loose.len(), "path"),
                    root.display()
                ),
            )
            .fix(if eligible {
                repairs.join("; ")
            } else {
                SHARED_HOME_FIX.into()
            })
        } else if changes.is_empty() {
            private()
        } else {
            Check::new(
                "logs",
                "fixed",
                format!(
                    "Made {} under {} private",
                    plural(changes.len(), "path"),
                    root.display()
                ),
            )
        }
    };
    check.details(details).changes(changes)
}

pub(super) fn open_regular(path: &Path) -> io::Result<File> {
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC;
    let file = File::from(unix::open(path, flags, Mode::empty())?);
    if !file.metadata()?.is_file() {
        return Err(io::ErrorKind::InvalidData.into());
    }
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::{self, Permissions};

    #[test]
    fn trader_repair_refuses_a_replacement_after_profile_validation() {
        use std::os::unix::fs::PermissionsExt;

        for apply in [false, true] {
            let sandbox = tempfile::tempdir().unwrap();
            let home = sandbox.path().join("sai");
            let root = home.join("trader");
            let previous = sandbox.path().join("previous");
            fs::create_dir(&home).unwrap();
            fs::set_permissions(&home, Permissions::from_mode(0o700)).unwrap();
            let create_trader = || {
                fs::create_dir(&root).unwrap();
                fs::set_permissions(&root, Permissions::from_mode(0o755)).unwrap();
                fs::write(
                    root.join("sessions.json"),
                    serde_json::json!({
                        "version": 3,
                        "nonce": "AAAAAAAAAAAAAAAA",
                        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAAA=",
                    })
                    .to_string(),
                )
                .unwrap();
            };
            create_trader();
            let (changes, failed, eligible) = tighten(&root, TRADER_FILES, apply, |opened| {
                assert!(dedicated_profile(&home, true, Some(("trader", opened))).is_some());
                fs::rename(&root, &previous).unwrap();
                create_trader();
                assert!(dedicated_profile(&home, true, None).is_some());
                assert!(dedicated_profile(&home, true, Some(("trader", opened))).is_none());
                true
            });
            assert!(!eligible);
            assert!(changes.is_empty());
            assert_eq!(failed.len(), 1);
            assert_eq!(root.metadata().unwrap().mode() & 0o777, 0o755);
            assert_eq!(previous.metadata().unwrap().mode() & 0o777, 0o755);
        }
    }
}
