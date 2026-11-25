use std::{
    env,
    fs::{self, File, Metadata},
    io::{self, Read, Seek, Write},
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    process::{Command, ExitStatus, Stdio},
    thread,
    time::{Duration, Instant},
};

use serde_json::{Map, Value};

use super::{Client, NAMES, Registration, Scope, Target, private_server_args, shell_quote};

#[cfg(test)]
mod tests;

pub(super) type Result<T> = std::result::Result<T, String>;
pub(super) const READ_ONLY_CONFIG: &str = "The client config is read-only";

#[derive(Clone, Copy)]
pub(super) enum CodexLookup<'a> {
    Discover,
    Verify,
    Preview(&'a [String]),
}

enum JsonUpdate<'a> {
    Install,
    Repair(&'a Registration),
}

impl Client {
    pub(super) fn codex_neutral(self, home: &Path) -> PathBuf {
        let directory = self.directory(home);
        if directory.is_dir() {
            directory
        } else {
            env::temp_dir()
        }
    }
    pub(super) fn executable(self) -> Option<PathBuf> {
        which(match self {
            Self::Claude => "claude",
            Self::Codex => "codex",
            Self::Cursor => "cursor-agent",
        })
    }

    fn directory(self, home: &Path) -> PathBuf {
        let (var, default) = match self {
            Self::Claude => ("CLAUDE_CONFIG_DIR", ".claude"),
            Self::Codex => ("CODEX_HOME", ".codex"),
            Self::Cursor => ("", ".cursor"),
        };
        env::var_os(var)
            .filter(|v| !v.is_empty())
            .map_or_else(|| home.join(default), PathBuf::from)
    }

    pub(super) fn detected(self, home: &Path) -> Option<&'static str> {
        if self.executable().is_some() || self == Self::Cursor && which("cursor").is_some() {
            Some("cli")
        } else if self.directory(home).is_dir() {
            Some("home")
        } else {
            None
        }
    }

    pub(super) fn path(self, scope: Scope, home: &Path, cwd: &Path) -> PathBuf {
        match (self, scope) {
            (Self::Claude, Scope::Project) => cwd.join(".mcp.json"),
            (Self::Claude, _) => env::var_os("CLAUDE_CONFIG_DIR")
                .filter(|v| !v.is_empty())
                .map_or_else(|| home.to_owned(), PathBuf::from)
                .join(".claude.json"),
            (Self::Codex, _) => self.directory(home).join("config.toml"),
            (Self::Cursor, Scope::Project) => cwd.join(".cursor/mcp.json"),
            (Self::Cursor, _) => home.join(".cursor/mcp.json"),
        }
    }

    pub(super) fn registrations(
        self,
        home: &Path,
        cwd: Option<&Path>,
        name: &str,
        lookup: CodexLookup<'_>,
        deadline: Instant,
    ) -> Result<Vec<Registration>> {
        if self == Self::Codex {
            let neutral = self.codex_neutral(home);
            let user = codex_get(&neutral, Scope::User, name, lookup, deadline)?;
            let project = if let Some(cwd) = cwd.filter(|cwd| resolved(&neutral) != resolved(cwd)) {
                codex_get(cwd, Scope::Project, name, lookup, deadline)?
                    .filter(|entry| user.as_ref().is_none_or(|user| user.raw != entry.raw))
            } else {
                None
            };
            return Ok(project.into_iter().chain(user).collect());
        }

        let user = read_json(&self.path(Scope::User, home, home), deadline)?;
        let Some(cwd) = cwd else {
            let entry = &user["mcpServers"][name];
            return Ok(self
                .json_registration(entry, Scope::User, name, false)
                .into_iter()
                .collect());
        };
        let project = read_json(&self.path(Scope::Project, home, cwd), deadline)?;
        let checkout = (self == Self::Claude).then(|| claude_checkout_key(home, cwd, deadline));
        let local = checkout.as_ref().map_or(&Value::Null, |path| {
            &user["projects"][path.to_string_lossy().as_ref()]
        });
        let disabled = self == Self::Claude && has_name(&local["disabledMcpServers"], name);
        let project_entry = &project["mcpServers"][name];
        let project_disabled = self == Self::Claude
            && project_entry.is_object()
            && claude_project_disabled(
                home,
                cwd,
                checkout.as_deref().unwrap_or(cwd),
                name,
                deadline,
            )?;
        let entries = [
            (Scope::Local, &local["mcpServers"][name], disabled),
            (Scope::Project, project_entry, project_disabled),
            (Scope::User, &user["mcpServers"][name], disabled),
        ];
        Ok(entries
            .into_iter()
            .filter_map(|(scope, raw, disabled)| {
                if scope == Scope::Local && self != Self::Claude {
                    return None;
                }
                self.json_registration(raw, scope, name, disabled)
            })
            .collect())
    }

    fn json_registration(
        self,
        entry: &Value,
        scope: Scope,
        name: &str,
        disabled: bool,
    ) -> Option<Registration> {
        let mut extra = Vec::new();
        if self == Self::Claude
            && name == NAMES[0]
            && let Some(fields) = entry.as_object()
        {
            extra.extend(
                ["cwd", "url", "headers"]
                    .into_iter()
                    .filter(|key| fields.contains_key(*key)),
            );
            if fields.get("type").is_some_and(|kind| kind != "stdio") {
                extra.push("type");
            }
            if fields.keys().any(|key| {
                !matches!(
                    key.as_str(),
                    "type" | "command" | "args" | "env" | "cwd" | "url" | "headers"
                )
            }) {
                extra.push("custom settings");
            }
        }
        Registration::new(entry.clone(), entry, scope, disabled, extra)
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) fn write(
        self,
        scope: Scope,
        home: &Path,
        cwd: &Path,
        name: &str,
        target: &Target,
        old: Option<&Registration>,
        deadline: Instant,
    ) -> Result<()> {
        let path = self.path(scope, home, cwd);
        if self == Self::Codex {
            return edit(&path, true, deadline, |source| {
                edit_codex(source, name, target, old)
            })
            .map(|_| ());
        }
        let update = old.map_or(JsonUpdate::Install, JsonUpdate::Repair);
        let checkout = (self == Self::Claude && scope == Scope::Local)
            .then(|| claude_checkout_key(home, cwd, deadline));
        self.write_json(
            &path,
            checkout.as_deref().unwrap_or(cwd),
            scope,
            name,
            target,
            update,
            deadline,
        )
        .map(|_| ())
    }

    pub(super) fn repair_cursor(
        home: &Path,
        cwd: &Path,
        name: &str,
        target: &Target,
        old: &Registration,
        deadline: Instant,
    ) -> Result<()> {
        let path = Self::Cursor.path(old.scope, home, cwd);
        let published = Self::Cursor.write_json(
            &path,
            cwd,
            old.scope,
            name,
            target,
            JsonUpdate::Repair(old),
            deadline,
        )?;
        Self::verify_cursor_repair(&path, old.scope, name, target, published, deadline)
    }

    fn verify_cursor_repair(
        path: &Path,
        scope: Scope,
        name: &str,
        target: &Target,
        published: Option<PublishedConfig>,
        deadline: Instant,
    ) -> Result<()> {
        let verification = read_json(path, deadline).and_then(|doc| {
            let raw = &doc["mcpServers"][name];
            let actual = Registration::new(raw.clone(), raw, scope, false, vec![]);
            if actual.is_some_and(|entry| entry.stdio && entry.argv == target.argv) {
                Ok(())
            } else {
                Err("Cursor did not load the repaired Sai registration".into())
            }
        });
        if let Err(error) = &verification
            && let Some(published) = published
            && let Err(restore) = published.restore(path, Instant::now() + Duration::from_secs(1))
        {
            return Err(format!("{error}. {restore}"));
        }
        verification
    }

    pub(super) fn repair_codex(
        home: &Path,
        cwd: &Path,
        name: &str,
        target: &Target,
        old: &Registration,
        deadline: Instant,
    ) -> std::result::Result<(), (String, Option<String>)> {
        let client = Self::Codex;
        let path = client.path(Scope::User, home, cwd);
        let neutral = client.codex_neutral(home);
        let mut overrides = vec![format!(
            "mcp_servers.{name}.command={}",
            toml_edit::Value::from(target.argv[0].as_str())
        )];
        let mut expected = target.clone();
        if old.argv[1..] != target.argv[1..] {
            // Validate the standard prefix without sending saved operands on
            // Codex's command line. The file retains the full argument list.
            overrides.push(format!("mcp_servers.{name}.args=[\"mcp\", \"serve\"]"));
            expected.argv.truncate(1);
            expected.argv.extend(["mcp".into(), "serve".into()]);
        }
        let mut hint = None;
        edit(&path, true, deadline, |source| {
            let rendered = edit_codex(source, name, target, Some(old))?;
            if source != rendered.as_bytes()
                && !matches!(
                    codex_get(
                        &neutral, Scope::User, name, CodexLookup::Preview(&overrides), deadline
                    ),
                    Ok(Some(entry)) if entry.matches_command(&expected)
                )
            {
                hint = Some(codex_restore_hint(source, name, old));
                let state = codex_entry_state(&path, source, name, deadline);
                return Err(format!("Codex rejected the proposed repair. {state}"));
            }
            Ok(rendered)
        })
        .map(|_| ())
        .map_err(|error| (error, hint))
    }

    pub(super) fn remove(
        self,
        path: &Path,
        name: &str,
        old: &Registration,
        deadline: Instant,
    ) -> Result<Option<PublishedConfig>> {
        edit(path, false, deadline, |source| {
            if self == Self::Codex {
                return remove_codex(source, name, old);
            }
            let mut doc = json_object(source, path)?;
            if doc["mcpServers"].get(name).is_none() {
                return String::from_utf8(source.to_vec())
                    .map_err(|_| "Could not write MCP config".into());
            }
            let entries = doc["mcpServers"]
                .as_object_mut()
                .ok_or("The Sai registration changed concurrently")?;
            if entries.get(name) != Some(&old.raw) {
                return Err("The Sai registration changed concurrently".into());
            }
            entries.remove(name);
            let rendered =
                serde_json::to_string_pretty(&doc).map_err(|_| "Could not write MCP config")?;
            Ok(format!("{}\n", crate::local::ascii_json(&rendered)))
        })
    }

    #[allow(clippy::too_many_arguments)]
    fn write_json(
        self,
        path: &Path,
        cwd: &Path,
        scope: Scope,
        name: &str,
        target: &Target,
        update: JsonUpdate<'_>,
        deadline: Instant,
    ) -> Result<Option<PublishedConfig>> {
        let existing = match update {
            JsonUpdate::Install => None,
            JsonUpdate::Repair(old) => Some(old),
        };
        edit(path, existing.is_none(), deadline, |source| {
            let mut doc = json_object(source, path)?;
            let holder = if scope == Scope::Local {
                doc["projects"][cwd.to_string_lossy().as_ref()]
                    .as_object_mut()
                    .ok_or("Could not find the local MCP config")?
            } else {
                doc.as_object_mut().expect("JSON object checked")
            };
            let entries = holder
                .entry("mcpServers")
                .or_insert_with(|| Value::Object(Map::new()));
            if existing.is_none() && !entries.is_object() {
                *entries = Value::Object(Map::new());
            }
            let entries = entries
                .as_object_mut()
                .ok_or("Expected a table of MCP servers")?;
            let current = if let Some(old) = existing {
                Some(
                    entries
                        .get_mut(name)
                        .filter(|entry| {
                            if self == Self::Claude {
                                **entry == old.raw
                            } else {
                                super::argv(entry) == old.argv
                            }
                        })
                        .and_then(Value::as_object_mut)
                        .ok_or("The Sai registration changed concurrently")?,
                )
            } else {
                None
            };
            match update {
                JsonUpdate::Repair(_) => {
                    let entry = current.expect("existing registration checked");
                    entry.insert("command".into(), Value::from(target.argv[0].clone()));
                    entry.insert("args".into(), serde_json::json!(target.argv[1..]));
                }
                JsonUpdate::Install => {
                    let mut entry =
                        serde_json::json!({"command": target.argv[0], "args": target.argv[1..]});
                    if self == Self::Claude {
                        entry["type"] = serde_json::json!("stdio");
                    }
                    if let Some(profile) = &target.profile {
                        entry["env"] = serde_json::json!({"SAI_HOME": profile});
                    }
                    entries.insert(name.to_owned(), entry);
                }
            }
            let rendered =
                serde_json::to_string_pretty(&doc).map_err(|_| "Could not write MCP config")?;
            Ok(format!("{}\n", crate::local::ascii_json(&rendered)))
        })
    }
}

fn has_name(value: &Value, name: &str) -> bool {
    value
        .as_array()
        .is_some_and(|items| items.iter().any(|item| item == name))
}

fn claude_checkout_key(home: &Path, cwd: &Path, deadline: Instant) -> PathBuf {
    if let Ok((status, output, _)) = invoke(
        &[
            "git".into(),
            "worktree".into(),
            "list".into(),
            "--porcelain".into(),
            "-z".into(),
        ],
        cwd,
        deadline.min(Instant::now() + Duration::from_secs(5)),
        true,
    ) && status.success()
    {
        let mut lines = output
            .split(|byte| *byte == 0)
            .take_while(|line| !line.is_empty());
        if let Some(root) = lines
            .next()
            .and_then(|line| line.strip_prefix(b"worktree "))
        {
            use std::os::unix::ffi::OsStrExt;
            let root = Path::new(std::ffi::OsStr::from_bytes(root));
            if root.is_absolute()
                && root != home
                && !lines.any(|line| line == b"bare")
                && owned_checkout(root)
            {
                return root.to_owned();
            }
        }
    }
    cwd.ancestors()
        .find(|path| path.join(".git/HEAD").is_file() || path.join(".git").is_file())
        .unwrap_or(cwd)
        .to_owned()
}

fn owned_checkout(root: &Path) -> bool {
    [root.to_owned(), root.join(".git"), root.join(".claude")]
        .iter()
        .all(|path| match path.metadata() {
            Ok(meta) => meta.uid() == rustix::process::getuid().as_raw(),
            Err(error) => error.kind() == io::ErrorKind::NotFound,
        })
}

fn claude_project_disabled(
    home: &Path,
    cwd: &Path,
    checkout: &Path,
    name: &str,
    deadline: Instant,
) -> Result<bool> {
    let mut paths = vec![
        Client::Claude.directory(home).join("settings.json"),
        cwd.join(".claude/settings.json"),
        cwd.join(".claude/settings.local.json"),
    ];
    if checkout != cwd && checkout != home && owned_checkout(checkout) {
        paths.push(checkout.join(".claude/settings.local.json"));
    }
    for path in paths {
        if has_name(&read_json(&path, deadline)?["disabledMcpjsonServers"], name) {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(super) fn codex_get(
    cwd: &Path,
    scope: Scope,
    name: &str,
    lookup: CodexLookup<'_>,
    deadline: Instant,
) -> Result<Option<Registration>> {
    let Some(exe) = Client::Codex.executable() else {
        return match lookup {
            CodexLookup::Discover => Ok(None),
            CodexLookup::Verify | CodexLookup::Preview(_) => Err("`codex` is not on PATH".into()),
        };
    };
    let label = format!("codex mcp get {name} --json");
    let mut args = vec![
        exe.to_string_lossy().into_owned(),
        "mcp".into(),
        "get".into(),
        name.into(),
        "--json".into(),
    ];
    if let CodexLookup::Preview(overrides) = lookup {
        for value in overrides {
            args.extend(["-c".into(), value.clone()]);
        }
    }
    let (status, out, err) =
        invoke(&args, cwd, deadline, true).map_err(|e| format!("`{label}` did not run: {e}"))?;
    if !status.success() {
        let missing = format!("Error: No MCP server named '{name}' found.");
        let mut lines = err
            .split(|byte| *byte == b'\n')
            .map(|line| line.strip_suffix(b"\r").unwrap_or(line))
            .filter(|line| {
                !line.iter().all(u8::is_ascii_whitespace) && !line.starts_with(b"WARNING: ")
            });
        if status.code() == Some(1)
            && out.iter().all(u8::is_ascii_whitespace)
            && lines.next() == Some(missing.as_bytes())
            && lines.next().is_none()
        {
            return Ok(None);
        }
        return Err(exit_message(&label, status));
    }
    let data: Value =
        serde_json::from_slice(&out).map_err(|_| format!("`{label}` printed invalid JSON"))?;
    let filters = ["enabled_tools", "disabled_tools"]
        .into_iter()
        .filter(|key| !data[key].is_null())
        .collect();
    Registration::new(
        data.clone(),
        &data["transport"],
        scope,
        data["enabled"] == false,
        filters,
    )
    .map(Some)
    .ok_or_else(|| format!("`{label}` returned an invalid registration"))
}

pub(super) fn run_step(args: &[String], cwd: &Path, deadline: Instant) -> Result<()> {
    let label = format!("{} {} {}", basename(&args[0]), args[1], args[2]);
    let (status, _, _) =
        invoke(args, cwd, deadline, false).map_err(|e| format!("`{label}` did not run: {e}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(exit_message(&label, status))
    }
}

fn exit_message(label: &str, status: ExitStatus) -> String {
    use std::os::unix::process::ExitStatusExt;
    let code = status
        .code()
        .unwrap_or_else(|| -status.signal().unwrap_or(1));
    format!("`{label}` exited {code}. Run the client command directly for details.")
}

pub(super) fn invoke(
    args: &[String],
    cwd: &Path,
    deadline: Instant,
    capture: bool,
) -> Result<(ExitStatus, Vec<u8>, Vec<u8>)> {
    let mut command = Command::new(&args[0]);
    command
        .args(&args[1..])
        .current_dir(cwd)
        .stdin(Stdio::null());
    let output = || -> io::Result<(File, File)> {
        let file = tempfile::tempfile()?;
        let writer = file.try_clone()?;
        Ok((file, writer))
    };
    let outputs = if capture {
        let ((stdout, out), (stderr, err)) = output()
            .and_then(|a| output().map(|b| (a, b)))
            .map_err(io_kind)?;
        command.stdout(out).stderr(err);
        Some((stdout, stderr))
    } else {
        command.stdout(Stdio::null()).stderr(Stdio::null());
        None
    };
    if Instant::now() >= deadline {
        return Err("TimeoutExpired".into());
    }
    let mut child = command.spawn().map_err(io_kind)?;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            other => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(other.err().map_or_else(|| "TimeoutExpired".into(), io_kind));
            }
        }
    };
    let read = |mut file: File| {
        const LIMIT: u64 = 1024 * 1024;
        file.rewind().map_err(io_kind)?;
        let mut bytes = Vec::new();
        file.take(LIMIT + 1)
            .read_to_end(&mut bytes)
            .map_err(io_kind)?;
        if bytes.len() as u64 > LIMIT {
            return Err("client output exceeded 1 MiB".into());
        }
        Ok::<_, String>(bytes)
    };
    match outputs {
        Some((out, err)) => Ok((status, read(out)?, read(err)?)),
        None => Ok((status, Vec::new(), Vec::new())),
    }
}

fn io_kind(error: io::Error) -> String {
    match error.kind() {
        io::ErrorKind::NotFound => "FileNotFoundError",
        io::ErrorKind::PermissionDenied => "PermissionError",
        io::ErrorKind::TimedOut => "TimeoutExpired",
        _ => "OSError",
    }
    .into()
}

pub(super) fn basename(command: &str) -> &str {
    Path::new(command)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or_default()
}

pub(super) fn which(command: &str) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    let executable = |path: &Path| {
        path.metadata()
            .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
            && rustix::fs::access(path, rustix::fs::Access::EXEC_OK).is_ok()
    };
    if command.contains('/') {
        let path = PathBuf::from(command);
        return executable(&path).then_some(path);
    }
    env::split_paths(&env::var_os("PATH").unwrap_or_else(|| "/bin:/usr/bin".into()))
        .map(|dir| dir.join(command))
        .find(|path| executable(path))
}

pub(super) fn absolute(path: &Path) -> PathBuf {
    let path = std::path::absolute(path).unwrap_or_else(|_| path.to_owned());
    let mut clean = PathBuf::new();
    for part in path.components() {
        if part == std::path::Component::ParentDir {
            clean.pop();
        } else {
            clean.push(part);
        }
    }
    clean
}

pub(super) fn resolved(path: &Path) -> PathBuf {
    // Resolve existing ancestors and symlinks even when the config is missing.
    let path = std::path::absolute(path).unwrap_or_else(|_| path.to_owned());
    let mut remaining: std::collections::VecDeque<_> = path
        .components()
        .map(|part| part.as_os_str().to_owned())
        .collect();
    let mut current = PathBuf::new();
    let mut links = 0;
    while let Some(part) = remaining.pop_front() {
        if part == ".." {
            current.pop();
            continue;
        }
        current.push(part);
        if let Ok(target) = current.read_link() {
            links += 1;
            if links > 40 {
                return absolute(&path);
            }
            current.pop();
            for part in target.components().rev() {
                remaining.push_front(part.as_os_str().to_owned());
            }
        }
    }
    current
}

fn read(path: &Path, deadline: Instant) -> io::Result<Option<Vec<u8>>> {
    let path = path.to_owned();
    sai_core::files::read_before(deadline, move || read_config(&path))
}

fn read_config(path: &Path) -> io::Result<Option<Vec<u8>>> {
    let mut bytes = Vec::new();
    match sai_core::files::open_regular(path).and_then(|mut file| file.read_to_end(&mut bytes)) {
        Ok(_) => Ok(Some(bytes)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn json_object(source: &[u8], path: &Path) -> Result<Value> {
    let value: Value = serde_json::from_slice(source).map_err(|_| {
        let kind = if std::str::from_utf8(source).is_ok() {
            "JSONDecodeError"
        } else {
            "UnicodeDecodeError"
        };
        format!("could not read {}: {kind}", path.display())
    })?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(format!(
            "could not read {}: not a JSON object",
            path.display()
        ))
    }
}

fn read_json(path: &Path, deadline: Instant) -> Result<Value> {
    match read(path, deadline)
        .map_err(|e| format!("could not read {}: {}", path.display(), io_kind(e)))?
    {
        Some(source) => json_object(&source, path),
        None => Ok(serde_json::json!({})),
    }
}

pub(super) struct PublishedConfig {
    destination: PathBuf,
    original: Option<Vec<u8>>,
    replacement: String,
    metadata: Metadata,
}

impl PublishedConfig {
    pub(super) fn restore(self, path: &Path, deadline: Instant) -> Result<()> {
        // A concurrent edit belongs to the client or user. Restore only the
        // file this repair published, even if a newer file has identical text.
        let original = self
            .original
            .ok_or("The original client config is unavailable")?;
        let restored = replace_if_current(
            path,
            &self.destination,
            Some(self.replacement.as_bytes()),
            &original,
            Some(&self.metadata),
            deadline,
        )?;
        if restored.is_some() {
            Ok(())
        } else {
            Err("The client config changed before Sai could restore it".into())
        }
    }
}

fn edit(
    path: &Path,
    retry: bool,
    deadline: Instant,
    mut render: impl FnMut(&[u8]) -> Result<String>,
) -> Result<Option<PublishedConfig>> {
    let attempts = if retry { 2 } else { 1 };
    for _ in 0..attempts {
        let destination = resolved(path);
        let seen = read(&destination, deadline).map_err(io_kind)?;
        let source =
            seen.as_deref()
                .unwrap_or(if retry && path.extension().is_some_and(|e| e == "toml") {
                    b""
                } else {
                    b"{}"
                });
        let rendered = render(source)?;
        if source == rendered.as_bytes() {
            return Ok(None);
        }
        let original_metadata = if seen.is_some() {
            Some(match destination.metadata() {
                Ok(meta) => meta,
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    continue;
                }
                Err(error) => return Err(io_kind(error)),
            })
        } else {
            None
        };
        if let Some(metadata) = replace_if_current(
            path,
            &destination,
            seen.as_deref(),
            rendered.as_bytes(),
            original_metadata.as_ref(),
            deadline,
        )? {
            return Ok(Some(PublishedConfig {
                destination,
                original: seen,
                replacement: rendered,
                metadata,
            }));
        }
    }
    if !retry {
        Err("The Sai registration changed concurrently".into())
    } else if path.extension().is_some_and(|e| e == "toml") {
        Err("Codex config.toml changed while Sai was writing it".into())
    } else {
        Err(format!(
            "{} kept changing while sai was writing it; nothing was written. Run the command again.",
            path.display()
        ))
    }
}

fn replace_if_current(
    path: &Path,
    destination: &Path,
    seen: Option<&[u8]>,
    replacement: &[u8],
    original_metadata: Option<&Metadata>,
    deadline: Instant,
) -> Result<Option<Metadata>> {
    if let Some(meta) = original_metadata {
        writable(destination, meta)?;
    }
    let parent = destination.parent().ok_or("Missing config directory")?;
    fs::create_dir_all(parent).map_err(io_kind)?;
    let mut temp = tempfile::NamedTempFile::new_in(parent).map_err(io_kind)?;
    temp.write_all(replacement).map_err(io_kind)?;
    if let Some(meta) = original_metadata {
        temp.as_file()
            .set_permissions(meta.permissions())
            .map_err(io_kind)?;
    }
    let published_metadata = temp.as_file().metadata().map_err(io_kind)?;
    if resolved(path) != destination
        || read(destination, deadline).map_err(io_kind)?.as_deref() != seen
    {
        return Ok(None);
    }
    if let Some(expected) = original_metadata
        && !destination.metadata().is_ok_and(|actual| {
            actual.dev() == expected.dev()
                && actual.ino() == expected.ino()
                && actual.mode() == expected.mode()
                && actual.uid() == expected.uid()
                && actual.gid() == expected.gid()
        })
    {
        return Ok(None);
    }
    temp.persist(destination)
        .map_err(|error| io_kind(error.error))?;
    Ok(Some(published_metadata))
}

pub(super) fn check_writable(path: &Path) -> Result<()> {
    writable(path, &path.metadata().map_err(io_kind)?)
}

fn writable(path: &Path, metadata: &Metadata) -> Result<()> {
    if metadata.permissions().readonly()
        || rustix::fs::access(path, rustix::fs::Access::WRITE_OK).is_err()
    {
        return Err(READ_ONLY_CONFIG.into());
    }
    Ok(())
}

fn remove_codex(source: &[u8], name: &str, old: &Registration) -> Result<String> {
    use toml_edit::{Document, Item};

    let bad = "Could not edit Codex config.toml";
    let source = std::str::from_utf8(source).map_err(|_| bad)?;
    let doc = Document::parse(source).map_err(|_| bad)?;
    let changed = "The Sai registration changed concurrently";
    let servers = doc.get("mcp_servers").ok_or(changed)?;
    let item = servers.get(name).ok_or(changed)?;
    let entry = item.as_table_like().ok_or(changed)?;
    let matches = if old.stdio {
        codex_argv_matches(entry, old)
    } else {
        let url = old.raw["transport"]["url"].as_str();
        url.is_some() && entry.get("url").and_then(Item::as_str) == url
    };
    if !matches {
        return Err(changed.into());
    }

    // The parser's spans exclude trivia. Expand only the statements we
    // delete to whole lines; separate table comments and endings stay intact.
    let line = |span: std::ops::Range<usize>| {
        let start = source[..span.start].rfind('\n').map_or(0, |at| at + 1);
        let end = source[span.end..]
            .find('\n')
            .map_or(source.len(), |at| span.end + at + 1);
        start..end
    };
    let mut removed = Vec::new();
    if let Some(inline) = servers.as_inline_table() {
        let mut parts = inline
            .get_values()
            .into_iter()
            .map(|(keys, value)| {
                let leaf = keys.last().ok_or(bad)?;
                Ok((
                    leaf.span().ok_or(bad)?.start..value.span().ok_or(bad)?.end,
                    keys[0].get() == name,
                ))
            })
            .collect::<Result<Vec<_>>>()?;
        parts.sort_by_key(|(span, _)| span.start);
        let mut offset = 0;
        for group in parts.chunk_by(|left, right| left.1 == right.1) {
            if group[0].1 {
                let leaf = group[0].0.start;
                let end = group.last().expect("nonempty group").0.end;
                let comma = |start, end| source[start..end].find(',').map(|at| start + at);
                // A repeated dotted prefix can reuse its first key's span.
                // Find this assignment's start from the preceding delimiter.
                let delimiter = if offset == 0 {
                    inline.span().ok_or(bad)?.start
                } else {
                    comma(parts[offset - 1].0.end, leaf).ok_or(bad)?
                };
                let gap = &source[delimiter + 1..leaf];
                let start = leaf - gap.trim_start().len();
                let span = if let Some(next) = parts.get(offset + group.len()) {
                    start..comma(end, next.0.start).ok_or(bad)? + 1
                } else if offset != 0 {
                    delimiter..end
                } else {
                    let close = inline.span().ok_or(bad)?.end;
                    start..comma(end, close).map_or(end, |at| at + 1)
                };
                removed.push(span);
            }
            offset += group.len();
        }
    } else {
        // Dotted assignments may share the root or an explicit parent table
        // with other servers and may be interleaved with their assignments.
        let dotted = |table: &toml_edit::Table, parent: &[&str]| {
            table
                .get_values()
                .into_iter()
                .filter_map(|(keys, value)| {
                    let path = keys.iter().map(|key| key.get()).take(parent.len());
                    if !path.eq(parent.iter().copied()) {
                        return None;
                    }
                    Some(line(keys.last()?.span()?.start..value.span()?.end))
                })
                .collect::<Vec<_>>()
        };
        removed.extend(dotted(doc.as_table(), &["mcp_servers", name]));
        if let Some(table) = servers.as_table().filter(|table| !table.is_implicit()) {
            removed.extend(dotted(table, &[name]));
        }
        let mut tables = vec![item];
        while let Some(item) = tables.pop() {
            let mut visit = |table: &toml_edit::Table| {
                if !table.is_implicit()
                    && let Some(span) = table.span()
                {
                    let end = table
                        .get_values()
                        .iter()
                        .filter_map(|(_, value)| value.span())
                        .map(|span| span.end)
                        .max()
                        .unwrap_or(span.end);
                    removed.push(line(span.start..end));
                }
            };
            match item {
                Item::Table(table) => {
                    visit(table);
                    tables.extend(table.iter().map(|(_, item)| item));
                }
                Item::ArrayOfTables(array) => {
                    for table in array.iter() {
                        visit(table);
                        tables.extend(table.iter().map(|(_, item)| item));
                    }
                }
                _ => {}
            }
        }
    }
    removed.sort_by_key(|span| span.start);
    let mut rendered = String::new();
    let mut offset = 0;
    for span in removed {
        rendered.push_str(source.get(offset..span.start).ok_or(bad)?);
        offset = span.end;
    }
    rendered.push_str(&source[offset..]);
    let after = Document::parse(rendered.as_str()).map_err(|_| bad)?;
    if after
        .get("mcp_servers")
        .and_then(|servers| servers.get(name))
        .is_some()
    {
        return Err(bad.into());
    }
    check_preserved_codex(doc.as_table(), after.as_table(), name)?;
    Ok(rendered)
}

fn codex_argv_matches(entry: &dyn toml_edit::TableLike, old: &Registration) -> bool {
    use toml_edit::Item;

    entry
        .get("command")
        .and_then(Item::as_str)
        .into_iter()
        .chain(
            entry
                .get("args")
                .and_then(Item::as_array)
                .into_iter()
                .flat_map(|args| args.iter().map(|arg| arg.as_str().unwrap_or_default())),
        )
        .eq(old.argv.iter().map(String::as_str))
}

fn codex_entry(source: &[u8], name: &str) -> Result<Option<toml_edit::Item>> {
    let doc = std::str::from_utf8(source)
        .ok()
        .and_then(|text| text.parse::<toml_edit::DocumentMut>().ok())
        .ok_or("Could not read Codex config.toml")?;
    Ok(doc
        .get("mcp_servers")
        .and_then(toml_edit::Item::as_table_like)
        .and_then(|entries| entries.get(name))
        .cloned())
}

fn codex_entry_state(path: &Path, source: &[u8], name: &str, deadline: Instant) -> &'static str {
    let current = read(path, deadline)
        .map_err(io_kind)
        .and_then(|bytes| codex_entry(bytes.as_deref().unwrap_or_default(), name));
    match current {
        Ok(Some(current))
            if codex_entry(source, name)
                .ok()
                .flatten()
                .as_ref()
                .map(semantic_item)
                == Some(semantic_item(&current)) =>
        {
            "The original Sai entry is unchanged."
        }
        Ok(Some(_)) => "The Sai entry was changed by another process.",
        Ok(None) => "The Sai entry is missing.",
        Err(_) => "Could not read the current Sai entry.",
    }
}

fn codex_restore_hint(source: &[u8], name: &str, old: &Registration) -> String {
    let original = codex_entry(source, name).ok().flatten();
    let original = original.as_ref().and_then(toml_edit::Item::as_table_like);
    let env_count = original
        .and_then(|entry| entry.get("env"))
        .and_then(toml_edit::Item::as_table_like)
        .map_or(0, |env| env.len());
    let launcher = if NAMES.contains(&old.argv[0].as_str()) {
        old.argv[0].as_str()
    } else {
        "<original-launcher>"
    };
    let command = ["codex", "mcp", "add", name]
        .into_iter()
        .chain(std::iter::repeat_n(["--env", "<NAME>=<value>"], env_count).flatten())
        .chain(["--", launcher])
        .map(shell_quote)
        .chain(
            private_server_args(&old.argv[1..], "<original-argument>", "<original-value>")
                .iter()
                .map(|arg| shell_quote(arg)),
        )
        .collect::<Vec<_>>()
        .join(" ");
    let plural = if env_count == 1 { "" } else { "s" };
    let mut hint = format!(
        "If you need to restore the entry, use the same Codex home. The saved entry has {env_count} environment assignment{plural}. Replace the placeholders with the original values in `{command}`."
    );
    if original.is_some_and(|entry| {
        entry
            .iter()
            .any(|(key, _)| !matches!(key, "command" | "args" | "env"))
    }) {
        hint.push_str(" Restore settings such as tool filters in Codex config.toml.");
    }
    hint
}

fn edit_codex(
    source: &[u8],
    name: &str,
    target: &Target,
    old: Option<&Registration>,
) -> Result<String> {
    use toml_edit::{Array, DocumentMut, InlineTable, Item, Table, Value as TomlValue, value};
    let bad = "Could not edit Codex config.toml";
    let source = std::str::from_utf8(source).map_err(|_| bad)?;
    let mut doc = source.parse::<DocumentMut>().map_err(|_| bad)?;
    let original = doc.clone();
    if !doc.contains_key("mcp_servers") {
        let mut implicit = Table::new();
        implicit.set_implicit(true);
        doc.insert("mcp_servers", Item::Table(implicit));
    }
    let entries = doc["mcp_servers"]
        .as_table_like_mut()
        .ok_or("Codex mcp_servers must be a table")?;
    if old.is_none()
        && entries
            .get(name)
            .is_none_or(|entry| entry.as_table_like().is_none())
    {
        let entry = if entries.is_dotted()
            || original
                .get("mcp_servers")
                .is_some_and(Item::is_inline_table)
        {
            value(InlineTable::new())
        } else {
            Item::Table(Table::new())
        };
        entries.insert(name, entry);
    }
    let entry = entries
        .get_mut(name)
        .and_then(Item::as_table_like_mut)
        .ok_or("The Sai registration changed concurrently")?;
    let args = target.argv[1..]
        .iter()
        .map(String::as_str)
        .collect::<Array>();
    if let Some(old) = old {
        if !codex_argv_matches(entry, old) {
            return Err("The Sai registration changed concurrently".into());
        }
    } else {
        let removed = entry
            .iter()
            .map(|(key, _)| key)
            .filter(|key| {
                !(matches!(*key, "command" | "args") || *key == "env" && target.profile.is_some())
            })
            .map(str::to_owned)
            .collect::<Vec<_>>();
        for key in removed {
            entry.remove(&key);
        }
        if let Some(profile) = &target.profile {
            let mut env = InlineTable::new();
            env.insert("SAI_HOME", TomlValue::from(profile.as_str()));
            entry.insert("env", value(env));
        }
    }
    for (key, mut next) in [
        ("command", TomlValue::from(target.argv[0].as_str())),
        ("args", TomlValue::from(args)),
    ] {
        if let Some(existing) = entry.get(key).and_then(Item::as_value) {
            next.decor_mut().clone_from(existing.decor());
            if semantic_value(existing) == semantic_value(&next) {
                continue;
            }
        }
        entry.insert(key, Item::Value(next));
    }
    render_codex(original, doc, name)
}

fn render_codex(
    before: toml_edit::DocumentMut,
    doc: toml_edit::DocumentMut,
    name: &str,
) -> Result<String> {
    use toml_edit::DocumentMut;

    let rendered = doc.to_string();
    // Reparse so changes to TOML table boundaries cannot move unrelated keys.
    let after = rendered
        .parse::<DocumentMut>()
        .map_err(|_| "Could not edit Codex config.toml")?;
    check_preserved_codex(before.as_table(), after.as_table(), name)?;
    Ok(rendered)
}

fn check_preserved_codex(
    before: &toml_edit::Table,
    after: &toml_edit::Table,
    name: &str,
) -> Result<()> {
    let without_entry = |doc: &toml_edit::Table| {
        let mut values = semantic_table(doc);
        if let Some(Value::Object(entries)) = values.get_mut("mcp_servers") {
            entries.remove(name);
            if entries.is_empty() {
                values.remove("mcp_servers");
            }
        }
        values
    };
    if without_entry(before) != without_entry(after) {
        return Err("Could not preserve settings outside the Sai registration".into());
    }
    Ok(())
}

fn semantic_table(table: &dyn toml_edit::TableLike) -> std::collections::BTreeMap<String, Value> {
    table
        .iter()
        .map(|(key, item)| (key.to_owned(), semantic_item(item)))
        .collect()
}

fn semantic_item(item: &toml_edit::Item) -> Value {
    match item {
        toml_edit::Item::Table(table) => {
            serde_json::json!(semantic_table(table))
        }
        toml_edit::Item::ArrayOfTables(tables) => Value::Array(
            tables
                .iter()
                .map(|t| serde_json::json!(semantic_table(t)))
                .collect(),
        ),
        toml_edit::Item::Value(value) => semantic_value(value),
        toml_edit::Item::None => Value::Null,
    }
}

fn semantic_value(value: &toml_edit::Value) -> Value {
    use toml_edit::Value as T;
    match value {
        T::InlineTable(table) => serde_json::json!(semantic_table(table)),
        T::Array(array) => Value::Array(array.iter().map(semantic_value).collect()),
        T::String(value) => serde_json::json!(["string", value.value()]),
        T::Integer(value) => serde_json::json!(["integer", value.value()]),
        T::Float(value) => serde_json::json!(["float", value.value().to_bits()]),
        T::Boolean(value) => serde_json::json!(["boolean", value.value()]),
        T::Datetime(value) => {
            serde_json::json!(["datetime", value.value().to_string()])
        }
    }
}
