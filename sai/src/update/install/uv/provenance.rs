use std::{
    fs,
    path::{Path, PathBuf},
};

use percent_encoding::percent_decode_str;
use semver::Version;
use serde_json::{Value, json};
use url::Url;

use super::{Wheel, release, state};
use crate::update::install::{Result, io as io_error};

const RELEASE_RECORD: &str = ".sai-cli-release.json";
const RELEASE_PATH: &str = "/SaiDotFun/open-sai/releases/download/sai-cli/v";

fn dist_info(prefix: &Path) -> Option<PathBuf> {
    let mut packages = fs::read_dir(prefix.join("lib"))
        .into_iter()
        .flatten()
        .flatten()
        .filter(|entry| entry.file_name().to_string_lossy().starts_with("python"))
        .map(|entry| entry.path().join("site-packages"))
        .collect::<Vec<_>>();
    packages.sort();
    packages.push(prefix.join("Lib/site-packages"));
    packages.into_iter().find_map(|directory| {
        fs::read_dir(directory)
            .ok()?
            .flatten()
            .filter(|entry| {
                let name = entry.file_name();
                let name = name.to_string_lossy();
                name.starts_with("sai_cli-") && name.ends_with(".dist-info")
            })
            .map(|entry| entry.path())
            .min()
    })
}

fn read_json(path: &Path) -> Option<Value> {
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}

fn direct_url(prefix: &Path) -> Value {
    dist_info(prefix)
        .and_then(|info| read_json(&info.join("direct_url.json")))
        .unwrap_or(Value::Null)
}

pub(super) fn installed_version(prefix: &Path) -> Option<Version> {
    let metadata = fs::read_to_string(dist_info(prefix)?.join("METADATA")).ok()?;
    let version = metadata
        .lines()
        .take_while(|line| !line.trim().is_empty())
        .filter_map(|line| line.split_once(':'))
        .find_map(|(name, value)| name.eq_ignore_ascii_case("version").then_some(value.trim()))?;
    release::version(version).or_else(|| {
        let (stable, rc) = version.split_once("rc")?;
        release::version(&format!("{stable}-rc.{rc}"))
    })
}

pub(super) fn installed_digest(prefix: &Path) -> Option<String> {
    let record = fs::read(dist_info(prefix)?.join("RECORD")).ok()?;
    Some(release::sha256(&record))
}

pub(super) fn file_origin(prefix: &Path) -> Option<PathBuf> {
    Url::parse(direct_url(prefix)["url"].as_str()?)
        .ok()?
        .to_file_path()
        .ok()
}

fn recorded_wheel(prefix: &Path, url: &Url) -> Option<()> {
    let record = read_json(&prefix.parent()?.join(RELEASE_RECORD))?;
    if record["schema"] != 1
        || !record["sha256"].as_str().is_some_and(canonical_digest)
        || !record["installed"].as_str().is_some_and(canonical_digest)
        || (!record["wheel"].is_null() && !record["wheel"].is_string())
    {
        return None;
    }
    let version = release::version(record["tag"].as_str()?.strip_prefix("sai-cli/v")?)?;
    let path = url.to_file_path().ok()?;
    if Some(&version) != installed_version(prefix).as_ref()
        || record["installed"].as_str() != installed_digest(prefix).as_deref()
        || path.file_name()?.to_str()? != release::wheel_name(&version)?
    {
        return None;
    }
    Some(())
}

fn canonical_digest(value: &str) -> bool {
    release::valid_digest(value) && !value.bytes().any(|byte| byte.is_ascii_uppercase())
}

pub(super) fn has_origin(prefix: &Path) -> bool {
    direct_url(prefix)["url"].is_string()
}

fn authority(input: &str) -> Option<&str> {
    input
        .split_once(':')?
        .1
        .strip_prefix("//")?
        .split(['/', '?', '#'])
        .next()
}

fn upstream_wheel(url: &Url, raw: &str, web_base: &str) -> bool {
    let Ok(base) = Url::parse(web_base) else {
        return false;
    };
    let expected = format!("{}{RELEASE_PATH}", base.path().trim_end_matches('/'));
    url.scheme() == base.scheme()
        && authority(raw).is_some_and(|origin| Some(origin) == authority(web_base))
        && url.username().is_empty()
        && url.password().is_none()
        && url.path().ends_with(".whl")
        && percent_decode_str(url.path())
            .decode_utf8_lossy()
            .to_ascii_lowercase()
            .starts_with(&expected.to_ascii_lowercase())
}

fn has_vcs(direct: &Value) -> bool {
    direct["vcs_info"]
        .as_object()
        .is_some_and(|vcs| !vcs.is_empty())
}

pub(super) fn is_release(prefix: &Path, web_base: &str) -> bool {
    let direct = direct_url(prefix);
    !has_vcs(&direct)
        && direct["dir_info"]["editable"] != true
        && direct["url"].as_str().is_some_and(|raw| {
            Url::parse(raw).ok().is_some_and(|url| {
                upstream_wheel(&url, raw, web_base) || recorded_wheel(prefix, &url).is_some()
            })
        })
}

pub(super) fn kind(prefix: &Path, web_base: &str) -> &'static str {
    let direct = direct_url(prefix);
    if direct["dir_info"]["editable"] == true {
        return "checkout";
    }
    if prefix.join("uv-receipt.toml").is_file() {
        if is_release(prefix, web_base) {
            return "uv-tool-release";
        }
        if direct["vcs_info"]["vcs"] == "git"
            && direct["url"]
                .as_str()
                .and_then(|url| Url::parse(url).ok())
                .is_some_and(|url| {
                    let path = url.path().trim_end_matches('/');
                    let path = path.strip_suffix(".git").unwrap_or(path);
                    url.host_str() == Some("github.com")
                        && path.eq_ignore_ascii_case("/SaiDotFun/open-sai")
                })
        {
            return "uv-tool-git";
        }
        return "uv-tool-other";
    }
    if prefix
        .parent()
        .and_then(Path::file_name)
        .is_some_and(|name| name == "archive-v0")
    {
        return "uvx";
    }
    "other"
}

pub(super) fn record(prefix: &Path, wheel: &Wheel, path: &Path) -> Result<()> {
    let installed = installed_digest(prefix)
        .ok_or_else(|| "could not read the installed wheel RECORD".to_owned())?;
    let parent = prefix.parent().expect("uv tool prefix has a parent");
    state::atomic_json(
        &parent.join(RELEASE_RECORD),
        &json!({
            "schema": 1,
            "tag": format!("sai-cli/v{}", wheel.version),
            "sha256": wheel.digest,
            "installed": installed,
            "wheel": path,
        }),
    )
    .map_err(io_error)
}
