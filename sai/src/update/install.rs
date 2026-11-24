mod standalone;
mod uv;

use std::ffi::OsStr;
use std::{
    env, fs,
    path::{Path, PathBuf},
};

use semver::Version;
use serde::Serialize;

use super::{release::Releases, smoke, state};

pub(super) type Result<T> = std::result::Result<T, String>;

pub(super) enum Install {
    Standalone(standalone::Standalone),
    Uv(uv::Uv),
    Other(PathBuf),
}

#[derive(Serialize)]
pub(super) struct InstallReport<'a> {
    kind: &'static str,
    prefix: std::borrow::Cow<'a, str>,
    url: Option<&'static str>,
    commit: Option<&'static str>,
    requested: Option<&'static str>,
}

pub(super) enum Change {
    Updated,
    Available,
    Unsupported(String),
    Failed { reason: String, rolled_back: bool },
}

impl Change {
    fn unchanged(reason: impl Into<String>, installed: &Version) -> Self {
        Self::Failed {
            reason: format!("{}. sai {installed} is unchanged.", reason.into()),
            rolled_back: false,
        }
    }
}

impl Install {
    pub(super) fn discover(home: &Path) -> Self {
        let bin = env::current_exe()
            .and_then(fs::canonicalize)
            .unwrap_or_default();
        if let Some(install) = standalone::Standalone::discover(&bin) {
            return Self::Standalone(install);
        }
        if let Some(install) = uv::Uv::discover(&bin, home) {
            return Self::Uv(install);
        }
        Self::Other(bin.parent().unwrap_or(&bin).to_owned())
    }

    pub(super) fn report(&self) -> InstallReport<'_> {
        let (kind, prefix) = match self {
            Self::Standalone(install) => ("standalone", install.root()),
            Self::Uv(install) => ("uv-tool-release", install.prefix()),
            Self::Other(prefix) => ("other", prefix.as_path()),
        };
        InstallReport {
            kind,
            prefix: prefix.to_string_lossy(),
            url: None,
            commit: None,
            requested: None,
        }
    }

    pub(super) fn kind(&self) -> &'static str {
        self.report().kind
    }

    pub(super) fn launcher(&self) -> Option<&Path> {
        match self {
            Self::Standalone(install) => Some(install.launcher()),
            Self::Uv(install) => Some(install.launcher()),
            Self::Other(_) => None,
        }
    }

    pub(super) fn lock_path(&self) -> Option<PathBuf> {
        match self {
            Self::Standalone(install) => Some(install.lock_path()),
            Self::Uv(install) => Some(install.lock_path()),
            Self::Other(_) => None,
        }
    }

    pub(super) fn owner(&self, recover: bool) -> Result<()> {
        let Some(launcher) = self.launcher() else {
            return Err(
                "This launcher is managed outside sai. Use its installer to update.".into(),
            );
        };
        let pending = match self {
            Self::Uv(install) if recover => install.can_recover()?,
            _ => false,
        };
        // When the launcher is missing, PATH may fall through to another
        // install. The recorded launcher and uv destination guard recovery.
        let missing = pending
            && launcher
                .metadata()
                .is_err_and(|err| err.kind() == std::io::ErrorKind::NotFound);
        if !missing
            && let Some(on_path) = smoke::find_executable(OsStr::new("sai"))
            && launcher_path(&on_path).as_deref() != Some(launcher)
        {
            return Err(format!(
                "PATH launches {}, which shadows {}.",
                on_path.display(),
                launcher.display()
            ));
        }
        match self {
            Self::Standalone(install) => install.active().map(|_| ()),
            Self::Uv(install) => install.owner(pending),
            Self::Other(_) => unreachable!(),
        }
    }

    pub(super) async fn recover_and_version(&self, home: &Path) -> Result<Version> {
        match self {
            Self::Standalone(install) => install.recover(),
            Self::Uv(install) => install.recover(home).await,
            Self::Other(_) => unreachable!(),
        }
    }

    pub(super) async fn change(
        &self,
        releases: &Releases,
        installed: &Version,
        target: &Version,
        allowed: impl Fn() -> bool,
    ) -> Change {
        match self {
            Self::Standalone(install) => install.change(releases, installed, target, allowed).await,
            Self::Uv(install) => install.change(releases, installed, target, allowed).await,
            Self::Other(_) => unreachable!(),
        }
    }
}

fn launcher_path(path: &Path) -> Option<PathBuf> {
    let parent = path.parent()?.canonicalize().ok()?;
    Some(parent.join(path.file_name()?))
}

fn io(error: std::io::Error) -> String {
    error.to_string()
}

fn remove_record(path: &Path) -> Result<()> {
    fs::remove_file(path).map_err(io)?;
    state::sync_dir(path.parent().expect("install record has a parent")).map_err(io)
}
