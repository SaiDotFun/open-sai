use std::{net::Ipv6Addr, str::FromStr};

use unicode_normalization::UnicodeNormalization;

pub(crate) struct Endpoint {
    pub scheme: String,
    pub authority: String,
    pub path: String,
    pub query: String,
    fragment: String,
}

impl Endpoint {
    // Keep the original path, user info, and explicit port. WHATWG URL parsers
    // normalize these fields, but v0.1 stores and derives endpoints as typed.
    pub fn split(raw: &str) -> Option<Self> {
        let text = raw
            .trim_start_matches(|c: char| c <= ' ')
            .replace(['\t', '\n', '\r'], "");
        let (scheme, rest) = text.split_once("://")?;
        let mut chars = scheme.bytes();
        if !chars.next()?.is_ascii_alphabetic()
            || !chars.all(|c| c.is_ascii_alphanumeric() || b"+.-".contains(&c))
        {
            return None;
        }
        let (authority, rest) = rest.split_at(rest.find(['/', '?', '#']).unwrap_or(rest.len()));
        if authority.is_empty() {
            return None;
        }
        let bracketed = authority.contains(['[', ']']);
        if bracketed {
            let host_port = authority
                .rsplit_once('@')
                .map_or(authority, |(_, host)| host);
            let (host, tail) = host_port.strip_prefix('[')?.split_once(']')?;
            if !tail.is_empty() && !tail.starts_with(':') {
                return None;
            }
            if let Some(ipv_future) = host.strip_prefix('v') {
                let (version, suffix) = ipv_future.split_once('.')?;
                if version.is_empty()
                    || !version.bytes().all(|c| c.is_ascii_hexdigit())
                    || suffix.is_empty()
                {
                    return None;
                }
            } else {
                let (address, zone) = host.split_once('%').unwrap_or((host, ""));
                if Ipv6Addr::from_str(address).is_err()
                    || (host.contains('%') && (zone.is_empty() || zone.contains('%')))
                {
                    return None;
                }
            }
        }
        if !authority.is_ascii()
            && authority
                .chars()
                .filter(|c| !matches!(c, '@' | ':' | '#' | '?'))
                .nfkc()
                .any(|c| "/?#@:".contains(c))
        {
            return None;
        }
        let (rest, fragment) = rest.split_once('#').unwrap_or((rest, ""));
        let (path, query) = rest.split_once('?').unwrap_or((rest, ""));
        Some(Self {
            scheme: scheme.to_ascii_lowercase(),
            authority: authority.into(),
            path: path.into(),
            query: query.into(),
            fragment: fragment.into(),
        })
    }

    pub fn host_port(&self) -> (String, Result<Option<u16>, ()>) {
        let (_, host_port) = self
            .authority
            .rsplit_once('@')
            .unwrap_or(("", &self.authority));
        let (host, port) = match host_port.split_once('[') {
            Some((_, bracketed)) => {
                let (host, tail) = bracketed.split_once(']').unwrap_or((bracketed, ""));
                (host, tail.split_once(':').map(|(_, port)| port))
            }
            None => host_port
                .split_once(':')
                .map_or((host_port, None), |(host, port)| (host, Some(port))),
        };
        let host = match host.split_once('%') {
            Some((host, zone)) => format!("{}%{zone}", host.to_lowercase()),
            None => host.to_lowercase(),
        };
        let host = if host.contains(':') {
            format!("[{host}]")
        } else {
            host
        };
        let port = port
            .filter(|port| !port.is_empty())
            .map(|port| {
                if port.bytes().all(|c| c.is_ascii_digit()) {
                    port.parse::<u16>().map_err(|_| ())
                } else {
                    Err(())
                }
            })
            .transpose();
        (host, port)
    }

    pub fn websocket(&self) -> String {
        let scheme = if self.scheme == "https" { "wss" } else { "ws" };
        let mut url = format!("{scheme}://{}{}", self.authority, self.path);
        if !self.query.is_empty() {
            url.push('?');
            url.push_str(&self.query);
        }
        if !self.fragment.is_empty() {
            url.push('#');
            url.push_str(&self.fragment);
        }
        url
    }
}
