//! HTTPX-compatible bypass matching for Keeper HTTP and HTTPS endpoints.

use std::{collections::BTreeMap, env, net::IpAddr};

use hyper::Uri;
use hyper_util::client::proxy::matcher::{Intercept, Matcher};
use url::{Host, Url};

pub(super) fn for_endpoint(url: &Url, destination: &Uri) -> Option<Intercept> {
    let Some(proxies) = environment_proxies() else {
        return Matcher::from_system().intercept(destination);
    };
    let get = |scheme| proxies.get(scheme).map_or("", String::as_str);
    if get("no")
        .split(',')
        .any(|entry| bypasses(entry.trim(), url))
    {
        return None;
    }
    // Hyper's curl-style exclusions ignore ports. Use its proxy URL and
    // credential parsing after applying the HTTPX URL patterns ourselves.
    Matcher::builder()
        .http(get("http"))
        .https(get("https"))
        .all(get("all"))
        .build()
        .intercept(destination)
}

fn environment_proxies() -> Option<BTreeMap<String, String>> {
    let entries: Vec<_> = env::vars_os()
        .filter_map(|(name, value)| {
            let name = name.into_string().ok()?;
            let scheme = name.to_ascii_lowercase().strip_suffix("_proxy")?.to_owned();
            Some((scheme, name.ends_with("_proxy"), value.into_string().ok()?))
        })
        .collect();
    if entries.is_empty() {
        return None;
    }
    // urllib first reads the case-insensitive names, then lets lowercase
    // names override them. CGI blocks the header-derived uppercase HTTP_PROXY.
    let mut proxies: BTreeMap<_, _> = entries
        .iter()
        .filter(|(_, _, value)| !value.is_empty())
        .map(|(scheme, _, value)| (scheme.clone(), value.clone()))
        .collect();
    if env::var_os("REQUEST_METHOD").is_some() {
        proxies.remove("http");
    }
    for (scheme, lowercase, value) in entries {
        if lowercase {
            if value.is_empty() {
                proxies.remove(&scheme);
            } else {
                proxies.insert(scheme, value);
            }
        }
    }
    Some(proxies)
}

fn bypasses(entry: &str, destination: &Url) -> bool {
    if entry == "*" {
        return true;
    }
    if entry.is_empty() {
        return false;
    }
    let (scheme, authority, qualified) = entry
        .split_once("://")
        .map_or(("all", entry, false), |(scheme, rest)| (scheme, rest, true));
    if !scheme.eq_ignore_ascii_case("all") && !scheme.eq_ignore_ascii_case(destination.scheme()) {
        return false;
    }
    let authority = if qualified {
        authority.split(['/', '?', '#']).next().unwrap_or_default()
    } else {
        authority
    };
    // Bare IPv6 is a host, including when its last component looks like a port.
    let address = authority
        .split('/')
        .next()
        .unwrap_or_default()
        .parse::<IpAddr>();
    let exact = qualified || address.is_ok() || authority.eq_ignore_ascii_case("localhost");
    if qualified && (authority.is_empty() || authority == "*") {
        return true;
    }
    let (pattern, subdomains, apex) = if let Some(domain) =
        authority.strip_prefix("*.").or_else(|| {
            if qualified {
                None
            } else {
                authority.strip_prefix('.')
            }
        }) {
        (domain, true, false)
    } else if let Some(domain) = authority.strip_prefix('*') {
        (domain, true, true)
    } else {
        (authority, !exact, true)
    };
    let pattern = match address {
        Ok(IpAddr::V6(ip)) => format!("[{ip}]"),
        _ => pattern.to_owned(),
    };
    let any_host = qualified && authority.starts_with("*:");
    let pattern = if any_host {
        format!("placeholder{pattern}")
    } else {
        pattern
    };
    let Ok(pattern) = Url::parse(&format!("{}://{pattern}", scheme.to_ascii_lowercase())) else {
        return false;
    };
    if pattern
        .port()
        .is_some_and(|port| Some(port) != destination.port())
    {
        return false;
    }
    if any_host {
        return true;
    }
    let Some(host) = pattern.host_str().and_then(|host| Host::parse(host).ok()) else {
        return false;
    };
    let host = host.to_string();
    let Some(destination_host) = destination.host_str() else {
        return false;
    };
    (apex && destination_host == host)
        || (subdomains
            && destination_host
                .strip_suffix(&host)
                .is_some_and(|prefix| prefix.ends_with('.') && prefix.len() > 1))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn httpx_patterns_distinguish_host_port_scheme_and_domain_suffix() {
        for (entry, url, expected) in [
            ("127.0.0.1:9142", "http://127.0.0.1:9142", true),
            ("127.0.0.1:9142", "http://127.0.0.1:9143", false),
            ("localhost", "http://foo.localhost", false),
            (".example.com", "https://example.com", false),
            (".example.com", "https://www.example.com", true),
            ("example.com", "https://notexample.com", false),
            ("http://example.com", "https://example.com", false),
            ("https://", "https://example.com", true),
            ("https://", "http://example.com", false),
            ("https://*:8443/path", "https://example.com:8443", true),
            ("https://*:8443", "https://example.com:8444", false),
            ("https://.example.com", "https://www.example.com", false),
            ("::1", "http://[::1]:9142", true),
            ("http://[::1]:9142", "http://[::1]:9142", true),
            ("http://[::1]:9142", "http://[::1]:9143", false),
            ("localhost:80", "http://localhost:80", false),
            ("http://localhost:80", "http://localhost:80", true),
        ] {
            assert_eq!(
                bypasses(entry, &Url::parse(url).unwrap()),
                expected,
                "{entry}, {url}"
            );
        }
    }
}
