use std::{fmt, time::Duration};

use reqwest::{Client, Response, redirect::Policy};
use semver::Version;
use serde_json::Value;
use sha2::{Digest, Sha256};
use url::{Host, Url};

const REPO: &str = "SaiDotFun/open-sai";
pub(super) const API: &str = "https://api.github.com";
pub(super) const WEB: &str = "https://github.com";
const TAG_PREFIX: &str = "sai-cli/v";
const CHECK_TIMEOUT: Duration = Duration::from_secs(30);
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(120);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_PAGES: u8 = 10;
const MAX_PAGE: usize = 16 << 20;
const MAX_SUMS: usize = 1 << 16;
pub(super) const MAX_ASSET: usize = 64 << 20;
const TIMED_OUT: &str = "timed out checking the latest release";

pub(super) fn stable(text: &str) -> Option<Version> {
    Version::parse(text)
        .ok()
        .filter(|version| version.pre.is_empty() && version.build.is_empty())
}

pub(super) fn version(text: &str) -> Option<Version> {
    Version::parse(text)
        .ok()
        .filter(|version| version.build.is_empty())
}

pub(super) fn tag(version: &Version) -> String {
    format!("{TAG_PREFIX}{version}")
}

pub(super) struct Releases {
    api: Url,
    web: Url,
}

#[derive(Debug)]
pub(super) struct ReleaseError(pub(super) String);

impl fmt::Display for ReleaseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}

type Result<T> = std::result::Result<T, ReleaseError>;

fn error(message: impl Into<String>) -> ReleaseError {
    ReleaseError(message.into())
}

fn loopback(url: &Url) -> bool {
    match url.host() {
        Some(Host::Ipv4(ip)) => ip.is_loopback(),
        Some(Host::Ipv6(ip)) => ip.is_loopback(),
        Some(Host::Domain("localhost")) => true,
        _ => false,
    }
}

fn base(input: &str, official: &str) -> Result<Url> {
    let input = input.trim_end_matches('/');
    let url = Url::parse(&format!("{input}/")).map_err(|_| error("invalid release base URL"))?;
    if input == official
        || (matches!(url.scheme(), "http" | "https")
            && loopback(&url)
            && url.username().is_empty()
            && url.password().is_none()
            && url.query().is_none()
            && url.fragment().is_none())
    {
        return Ok(url);
    }
    Err(error(
        "release sources must use the official GitHub repository or loopback",
    ))
}

fn request_error(exc: reqwest::Error) -> ReleaseError {
    if exc.is_timeout() {
        error(TIMED_OUT)
    } else if exc.is_connect() {
        error("could not reach GitHub: ConnectError")
    } else {
        error("could not reach GitHub")
    }
}

async fn bytes(mut response: Response, limit: usize, message: &str) -> Result<Vec<u8>> {
    if response
        .content_length()
        .is_some_and(|len| len > limit as u64)
    {
        return Err(error(message));
    }
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(request_error)? {
        if chunk.len() > limit - body.len() {
            return Err(error(message));
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

impl Releases {
    pub(super) fn new(api: &str, web: &str) -> Result<Self> {
        Ok(Self {
            api: base(api, API)?,
            web: base(web, WEB)?,
        })
    }

    fn client(&self) -> reqwest::ClientBuilder {
        let client = Client::builder()
            .user_agent("sai-cli")
            .connect_timeout(CONNECT_TIMEOUT);
        if loopback(&self.api) || loopback(&self.web) {
            return client.no_proxy();
        }
        client
    }

    pub(super) fn shim_fixture_base(&self) -> Option<&str> {
        (self.web.scheme() == "http"
            && self.web.host_str() == Some("127.0.0.1")
            && self.web.port().is_some()
            && self.web.path() == "/")
            .then(|| self.web.as_str().trim_end_matches('/'))
    }

    pub(super) fn asset(&self, version: &Version, name: &str) -> Url {
        // GitHub accepts the slash in the namespaced tag as part of the path.
        self.web
            .join(&format!("{REPO}/releases/download/{}/{name}", tag(version)))
            .expect("validated base URL and release filename")
    }

    pub(super) async fn latest(&self) -> Result<Option<Version>> {
        tokio::time::timeout(CHECK_TIMEOUT, self.latest_within_budget())
            .await
            .map_err(|_| error(TIMED_OUT))?
    }

    async fn latest_within_budget(&self) -> Result<Option<Version>> {
        let http = self
            .client()
            .redirect(Policy::none())
            .build()
            .map_err(request_error)?;
        let url = self
            .api
            .join(&format!("repos/{REPO}/releases"))
            .expect("release list URL");
        let mut latest = None;
        for page in 1..=MAX_PAGES {
            let response = http
                .get(url.clone())
                .header("Accept", "application/vnd.github+json")
                .header("X-GitHub-Api-Version", "2022-11-28")
                .query(&[("per_page", 100), ("page", page)])
                .send()
                .await
                .map_err(request_error)?;
            let status = response.status();
            if matches!(status.as_u16(), 403 | 429) || status.is_server_error() {
                return self.redirect(&http).await;
            }
            if status.as_u16() != 200 {
                return Err(error(format!(
                    "GitHub API answered HTTP {}",
                    status.as_u16()
                )));
            }
            let more = response
                .headers()
                .get("link")
                .and_then(|value| value.to_str().ok())
                .is_some_and(|link| link.contains("rel=\"next\""));
            let data = bytes(response, MAX_PAGE, "GitHub API answer is too large").await?;
            let releases: Vec<Value> = serde_json::from_slice(&data)
                .map_err(|_| error("GitHub API answered with malformed JSON"))?;
            let best = releases
                .iter()
                .filter(|item| !truthy(&item["draft"]) && !truthy(&item["prerelease"]))
                .filter_map(|item| stable(item["tag_name"].as_str()?.strip_prefix(TAG_PREFIX)?))
                .max();
            latest = latest.max(best);
            if !more {
                return Ok(latest);
            }
        }
        Err(error(format!(
            "the release list is longer than {MAX_PAGES} pages; the lookup is incomplete"
        )))
    }

    async fn redirect(&self, http: &Client) -> Result<Option<Version>> {
        let list = self
            .web
            .join(&format!("{REPO}/releases"))
            .expect("release page URL");
        let url = self
            .web
            .join(&format!("{REPO}/releases/latest"))
            .expect("latest release URL");
        let response = http.head(url.clone()).send().await.map_err(request_error)?;
        let location = response
            .headers()
            .get("location")
            .and_then(|value| value.to_str().ok())
            .filter(|value| !value.is_empty() && response.status().is_redirection());
        let Some(location) = location else {
            return Err(error(format!(
                "release page answered HTTP {} without a redirect",
                response.status().as_u16()
            )));
        };
        if url.join(location).ok().is_some_and(|target| {
            target
                .path()
                .trim_end_matches('/')
                .eq_ignore_ascii_case(list.path())
        }) {
            return Ok(None);
        }
        Err(error(
            "GitHub's API is rate limited or failing, so the latest sai-cli release cannot be determined; try again later",
        ))
    }

    pub(super) async fn verified(
        &self,
        version: &Version,
        name: &str,
    ) -> Result<(Vec<u8>, String)> {
        tokio::time::timeout(DOWNLOAD_TIMEOUT, self.download_pair(version, name))
            .await
            .map_err(|_| error("timed out downloading the release"))?
    }

    async fn download_pair(&self, version: &Version, name: &str) -> Result<(Vec<u8>, String)> {
        let web = self.web.clone();
        let http = self
            .client()
            .redirect(Policy::custom(move |attempt| {
                let url = attempt.url();
                let allowed = if loopback(&web) {
                    url.origin() == web.origin()
                } else {
                    url.scheme() == "https"
                        && url.port_or_known_default() == Some(443)
                        && matches!(
                            url.host_str(),
                            Some(
                                "github.com"
                                    | "release-assets.githubusercontent.com"
                                    | "objects.githubusercontent.com"
                            )
                        )
                };
                if allowed
                    && url.username().is_empty()
                    && url.password().is_none()
                    && attempt.previous().len() < 5
                {
                    attempt.follow()
                } else {
                    attempt.stop()
                }
            }))
            .build()
            .map_err(request_error)?;
        let manifest = self
            .download(&http, version, "SHA256SUMS", MAX_SUMS)
            .await?;
        let manifest =
            std::str::from_utf8(&manifest).map_err(|_| error("SHA256SUMS is not UTF-8"))?;
        let expected = expected_digest(manifest, name)?;
        let asset = self.download(&http, version, name, MAX_ASSET).await?;
        let digest = sha256(&asset);
        if digest != expected {
            return Err(error(format!("{name} does not match its SHA256SUMS entry")));
        }
        Ok((asset, digest))
    }

    async fn download(
        &self,
        http: &Client,
        version: &Version,
        name: &str,
        limit: usize,
    ) -> Result<Vec<u8>> {
        let response = http
            .get(self.asset(version, name))
            .send()
            .await
            .map_err(|exc| {
                if exc.is_timeout() {
                    error(format!("timed out downloading {name}"))
                } else {
                    error(format!("could not download {name}"))
                }
            })?;
        if response.status().as_u16() != 200 {
            return Err(error(format!(
                "download of {name} answered HTTP {}",
                response.status().as_u16()
            )));
        }
        bytes(response, limit, &format!("{name} is too large")).await
    }
}

fn expected_digest(manifest: &str, name: &str) -> Result<String> {
    let mut entries = manifest.lines().filter_map(|line| {
        let mut fields = line.split_ascii_whitespace();
        let digest = fields.next()?;
        let filename = fields.next()?;
        let filename = filename.strip_prefix('*').unwrap_or(filename);
        (filename == name).then_some((digest, fields.next()))
    });
    match (entries.next(), entries.next()) {
        (Some((digest, None)), None) if valid_digest(digest) => Ok(digest.to_ascii_lowercase()),
        (None, _) => Err(error(format!("SHA256SUMS does not list {name}"))),
        _ => Err(error(format!(
            "SHA256SUMS must list exactly one valid digest for {name}"
        ))),
    }
}

pub(super) fn valid_digest(digest: &str) -> bool {
    digest.len() == 64 && digest.bytes().all(|digit| digit.is_ascii_hexdigit())
}

pub(super) fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

pub(super) fn wheel_name(version: &Version) -> Option<String> {
    let base = format!("{}.{}.{}", version.major, version.minor, version.patch);
    let suffix = if version.pre.is_empty() {
        String::new()
    } else {
        let rc = version
            .pre
            .as_str()
            .strip_prefix("rc.")?
            .parse::<u64>()
            .ok()?;
        format!("rc{rc}")
    };
    Some(format!("sai_cli-{base}{suffix}-py3-none-any.whl"))
}

pub(super) fn archive_name(version: &Version) -> Option<String> {
    let platform = match (std::env::consts::OS, std::env::consts::ARCH) {
        ("linux", "x86_64") => "x86_64-unknown-linux-gnu",
        ("linux", "aarch64") => "aarch64-unknown-linux-gnu",
        ("macos", "x86_64") => "x86_64-apple-darwin",
        ("macos", "aarch64") => "aarch64-apple-darwin",
        _ => return None,
    };
    Some(format!("sai-{version}-{platform}.tar.gz"))
}
