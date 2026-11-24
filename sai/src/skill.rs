use std::{
    collections::{BTreeMap, BTreeSet},
    env,
    fs::{self, File},
    io::{self, Read, Write},
    path::{Component, Path, PathBuf},
};

use clap::Subcommand;
use sai_core::{
    errors::{Code, SaiError},
    settings::repr,
};
use serde_json::{Value, json};

use crate::local;

const FILES: &[(&str, &[u8])] = include!(concat!(env!("OUT_DIR"), "/skill.rs"));
const CLI_REPOSITORY: &str = "https://github.com/SaiDotFun/open-sai";
const SKILL_REPOSITORY: &str = "https://github.com/Unique-Divine/jiyuu";
const NPX_INSTALL: &str = "npx skills add SaiDotFun/open-sai --skill sai";

#[derive(Subcommand)]
pub enum SkillCommand {
    /// Print SKILL.md or a bundled child page.
    Show {
        #[arg(long, value_name = "NAME")]
        page: Option<String>,
    },
    /// Show where the bundled skill came from.
    Source,
    /// Copy the bundled skill to where agents load skills.
    Install {
        #[arg(long = "dir", value_name = "DIRECTORY")]
        target: Option<PathBuf>,
        #[arg(long)]
        force: bool,
    },
}

impl SkillCommand {
    pub fn run(self, json_output: bool) -> Result<(), SaiError> {
        match self {
            Self::Show { page } => show(page.as_deref()),
            Self::Source => source(json_output),
            Self::Install { target, force } => {
                if let Some(path) = &target
                    && path.is_file()
                {
                    return Err(SaiError::usage(format!(
                        "Invalid value for '--dir': Directory {} is a file.",
                        repr(&path.to_string_lossy())
                    )));
                }
                let target = match target {
                    Some(path) => expand_home(path)?,
                    None => default_dir()?,
                };
                install(&target, force, json_output)
            }
        }
    }
}

fn skill_error(message: impl Into<String>) -> SaiError {
    SaiError::new(Code::InvalidRequest, message)
}

fn io_error(_: io::Error) -> SaiError {
    SaiError::internal()
}

fn pages() -> Vec<&'static str> {
    FILES
        .iter()
        .filter_map(|&(name, _)| {
            (name != "SKILL.md")
                .then(|| name.strip_suffix(".md"))
                .flatten()
        })
        .collect()
}

pub fn show(page: Option<&str>) -> Result<(), SaiError> {
    let known = pages();
    let name = page.unwrap_or("SKILL");
    let wanted = name.strip_suffix(".md").unwrap_or(name);
    let data = if wanted == "SKILL" {
        include_bytes!("../../skills/sai/SKILL.md").as_slice()
    } else {
        let matches = if let Some(&exact) = known.iter().find(|&&page| page == wanted) {
            vec![exact]
        } else {
            known
                .iter()
                .copied()
                .filter(|page| page.rsplit('/').next() == Some(wanted))
                .collect()
        };
        let [page] = matches.as_slice() else {
            let problem = if matches.is_empty() {
                "does not exist"
            } else {
                "is ambiguous"
            };
            return Err(skill_error(format!(
                "Skill page {} {problem}. Pages: {}",
                repr(name),
                known.join(", ")
            )));
        };
        let name = format!("{page}.md");
        FILES.iter().find(|&&(file, _)| file == name).unwrap().1
    };
    io::stdout().lock().write_all(data).map_err(io_error)
}

fn default_dir() -> Result<PathBuf, SaiError> {
    user_home().map(|home| home.join(".agents/skills/sai"))
}

fn user_home() -> Result<PathBuf, SaiError> {
    env::home_dir().ok_or_else(SaiError::internal)
}

fn expand_home(path: PathBuf) -> Result<PathBuf, SaiError> {
    if let Ok(suffix) = path.strip_prefix("~") {
        Ok(user_home()?.join(suffix))
    } else {
        Ok(path)
    }
}

fn source_record() -> Value {
    FILES
        .iter()
        .find(|&&(name, _)| name == ".source.json")
        .and_then(|&(_, bytes)| serde_json::from_slice(bytes).ok())
        .unwrap_or(Value::Null)
}

fn source(json_output: bool) -> Result<(), SaiError> {
    let record = source_record();
    let directory = default_dir()?;
    let executable = env::current_exe().map_err(io_error)?;
    let version = env!("CARGO_PKG_VERSION");
    let pages = pages();
    if json_output {
        return local::emit(
            &json!({
                "name": "sai", "cliVersion": version, "path": executable,
                "pages": pages, "repositories": {"cli": CLI_REPOSITORY, "skill": SKILL_REPOSITORY},
                "source": {
                    "repository": record["repository"].as_str().unwrap_or(SKILL_REPOSITORY),
                    "path": record["path"], "ref": record["ref"], "commit": record["commit"]
                },
                "install": {"sai": "sai skill install", "npx": NPX_INSTALL, "defaultDir": directory}
            }),
            true,
        );
    }
    println!(
        "Skill:      sai (packaged with sai {version})\nPath:       {}\nPages:      {}\nCLI repo:   {CLI_REPOSITORY}\nSkill repo: {SKILL_REPOSITORY}\nSynced at:  {}\nInstall:\n  sai skill install    (copies it to {})\n  {NPX_INSTALL}",
        executable.display(),
        pages.join(", "),
        record["commit"].as_str().unwrap_or("not recorded"),
        directory.display()
    );
    Ok(())
}

fn absolute(path: &Path) -> io::Result<PathBuf> {
    let mut result = PathBuf::new();
    for component in std::path::absolute(path)?.components() {
        match component {
            Component::ParentDir => {
                result.pop();
            }
            Component::CurDir => {}
            other => result.push(other),
        }
    }
    Ok(result)
}

fn existing_files(
    root: &Path,
    directory: &Path,
    files: &mut BTreeMap<PathBuf, Vec<u8>>,
) -> io::Result<bool> {
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let path = entry.path();
        let kind = entry.file_type()?;
        if kind.is_dir() {
            if !existing_files(root, &path, files)? {
                return Ok(false);
            }
        } else if kind.is_file() {
            files.insert(path.strip_prefix(root).unwrap().into(), fs::read(&path)?);
        } else {
            return Ok(false);
        }
    }
    Ok(true)
}

fn is_sai_skill(target: &Path) -> io::Result<bool> {
    let path = target.join("SKILL.md");
    if !fs::symlink_metadata(&path).is_ok_and(|info| info.is_file()) {
        return Ok(false);
    }
    let mut head = Vec::new();
    File::open(path)?.take(200).read_to_end(&mut head)?;
    Ok(String::from_utf8_lossy(&head)
        .replace("\r\n", "\n")
        .starts_with("---\nname: sai\n"))
}

fn replace_dir(target: &Path, force: bool) -> Result<&'static str, SaiError> {
    let parent = target.parent().ok_or_else(SaiError::internal)?;
    let staging = tempfile::Builder::new()
        .prefix(if force { ".sai-previous-" } else { ".sai-new-" })
        .tempdir_in(parent)
        .map_err(io_error)?;
    let staged = staging.path().join("skill");
    fs::create_dir(&staged).map_err(io_error)?;
    for &(name, data) in FILES {
        let path = staged.join(name);
        fs::create_dir_all(path.parent().unwrap()).map_err(io_error)?;
        fs::write(path, data).map_err(io_error)?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&staged, fs::Permissions::from_mode(0o755)).map_err(io_error)?;
    }
    let outcome = action(target, force)?;
    match outcome {
        "unchanged" => {}
        "installed" => {
            // This succeeds only if the destination is still missing or empty.
            fs::rename(staged, target).map_err(io_error)?;
        }
        _ => exchange_dir(target, staging)?,
    }
    Ok(outcome)
}

fn exchange_dir(target: &Path, staging: tempfile::TempDir) -> Result<(), SaiError> {
    #[cfg(any(target_os = "linux", target_vendor = "apple", target_os = "redox"))]
    {
        use rustix::fs::{CWD, RenameFlags, renameat_with};

        // Persist staging before the exchange. The old skill takes its place
        // atomically and can still contain open files that an editor is writing.
        let backup = staging.keep();
        let previous = backup.join("skill");
        if let Err(error) = renameat_with(CWD, &previous, CWD, target, RenameFlags::EXCHANGE) {
            let _ = fs::remove_dir_all(backup);
            return Err(io_error(error.into()));
        }
        Ok(())
    }
    #[cfg(not(any(target_os = "linux", target_vendor = "apple", target_os = "redox")))]
    {
        let _ = staging;
        Err(skill_error(format!(
            "Atomic replacement is unavailable for {}. Move it aside and retry.",
            target.display()
        )))
    }
}

fn action(target: &Path, force: bool) -> Result<&'static str, SaiError> {
    let where_ = target.display();
    let info = match fs::symlink_metadata(target) {
        Ok(info) => info,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok("installed");
        }
        Err(error) => return Err(io_error(error)),
    };
    if info.is_symlink() {
        if force {
            return Ok("replaced");
        }
        let link = fs::read_link(target).map_err(io_error)?;
        return Err(skill_error(format!(
            "{where_} is a symlink to {}, so another tool manages it. Pass --force to replace the link with a copy; the link's target is left untouched.",
            link.display()
        )));
    }
    if !info.is_dir() {
        return Err(skill_error(format!(
            "{where_} exists and is not a directory."
        )));
    }
    let outcome = if fs::read_dir(target).map_err(io_error)?.next().is_none() {
        "installed"
    } else {
        let mut existing = BTreeMap::new();
        let regular = existing_files(target, target, &mut existing).map_err(io_error)?;
        let packaged: BTreeMap<_, _> = FILES
            .iter()
            .map(|&(name, data)| (PathBuf::from(name), data))
            .collect();
        let changed: Vec<_> = existing
            .keys()
            .chain(packaged.keys())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .filter(|&name| existing.get(name).map(Vec::as_slice) != packaged.get(name).copied())
            .collect();
        if regular && changed.is_empty() {
            return Ok("unchanged");
        }
        if !is_sai_skill(target).map_err(io_error)? {
            return Err(skill_error(format!(
                "{where_} holds something other than the sai skill; nothing was changed. Choose another --dir, or remove it yourself."
            )));
        }
        if !regular {
            return Err(skill_error(format!(
                "{where_} contains symlinks or special files, which sai never removes; nothing was changed. Remove them yourself, then retry."
            )));
        }
        if !force {
            let shown = changed
                .iter()
                .take(5)
                .map(|path| path.to_string_lossy())
                .collect::<Vec<_>>()
                .join(", ");
            let more = if changed.len() > 5 { ", ..." } else { "" };
            return Err(skill_error(format!(
                "{where_} differs from the packaged skill in {} file(s): {shown}{more}. Pass --force to replace the whole directory.",
                changed.len()
            )));
        }
        "replaced"
    };
    if env::current_dir()
        .and_then(fs::canonicalize)
        .ok()
        .zip(fs::canonicalize(target).ok())
        .is_some_and(|(cwd, target)| cwd.starts_with(target))
    {
        return Err(skill_error(format!(
            "The current directory is inside {where_}. Run this from another directory; nothing was changed."
        )));
    }
    Ok(outcome)
}

fn install(target: &Path, force: bool, json_output: bool) -> Result<(), SaiError> {
    let target = absolute(target).map_err(io_error)?;
    let parent = target.parent().ok_or_else(SaiError::internal)?;
    fs::create_dir_all(parent).map_err(io_error)?;
    #[cfg(unix)]
    let _lock = {
        let directory = File::open(parent).map_err(io_error)?;
        directory.lock().map_err(io_error)?;
        directory
    };
    let action = if action(&target, force)? == "unchanged" {
        "unchanged"
    } else {
        replace_dir(&target, force)?
    };
    let record = source_record();
    let commit = record["commit"].as_str();
    if json_output {
        return local::emit(
            &json!({
                "action": action, "dir": target, "files": FILES.len(), "commit": commit
            }),
            true,
        );
    }
    let message = match action {
        "installed" => "Installed the sai skill",
        "replaced" => "Replaced the sai skill",
        _ => "The sai skill is already up to date",
    };
    let synced = commit.map_or_else(String::new, |commit| {
        format!(", jiyuu {}", commit.chars().take(12).collect::<String>())
    });
    println!(
        "{message} at {} ({} files{synced}).",
        target.display(),
        FILES.len()
    );
    Ok(())
}

pub fn help(command: &str) -> Option<&'static str> {
    Some(match command {
        "skill" => {
            r#"Usage: sai skill [OPTIONS] COMMAND [ARGS]...

  Read, locate, and install the agent skill packaged with this CLI.

  The skill is a SKILL.md router plus child pages for the CLI, the MCP server,
  trading safety, and Sai protocol references. It is edited in jiyuu and
  copied here; `sai skill source` names the commit.

Options:
  --help  Show this message and exit.

Commands:
  install  Copy the packaged skill directory to where agents load skills.
  show     Print the skill's SKILL.md, or one child page with --page.
  source   Show where the packaged skill lives and where it came from."#
        }
        "skill show" => {
            r#"Usage: sai skill show [OPTIONS]

  Print the skill's SKILL.md, or one child page with --page.

  Output is the raw Markdown, also under --json.

  Examples:
    sai skill show
    sai skill show --page trading-safety
    sai skill show --page references/contracts

Options:
  --page NAME  Print a child page instead, such as cli, mcp, trading-safety,
               or references/contracts.
  --help       Show this message and exit."#
        }
        "skill source" => {
            r#"Usage: sai skill source [OPTIONS]

  Show where the packaged skill lives and where it came from.

  Prints the packaged path, its pages, the CLI and skill repositories, the
  jiyuu commit the skill was synced from, and the install commands.

Options:
  --help  Show this message and exit."#
        }
        "skill install" => {
            r#"Usage: sai skill install [OPTIONS]

  Copy the packaged skill directory to where agents load skills.

  Writes SKILL.md and every child page into DIR itself. Running it again with
  the same version reports `unchanged`. It refuses to replace a symlink or a
  directory that differs unless --force is given, and never replaces a
  directory that is not a sai skill.

  Examples:
    sai skill install
    sai skill install --dir ~/.claude/skills/sai
    sai skill install --force

Options:
  --dir DIRECTORY  Directory to write the skill into. [default:
                   ~/.agents/skills/sai]
  --force          Replace a symlink, or a sai skill whose content differs.
  --help           Show this message and exit."#
        }
        _ => return None,
    })
}
