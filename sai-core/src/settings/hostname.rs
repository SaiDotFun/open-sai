//! The hostname checks HTTPX applies to HTTP endpoint settings.

use std::sync::LazyLock;

use regex::Regex;
use unicode_normalization::is_nfc;

mod pvalid;

pub(super) fn valid_http_host(authority: &str, host: &str) -> bool {
    // Endpoint::split has already validated bracketed IP literals. HTTPX
    // only accepts IPv6 in this form.
    if authority.starts_with('[') {
        return !authority.starts_with("[v");
    }
    // HTTPX chooses its ASCII branch before lowercasing the registered name.
    if authority.is_ascii() {
        return true;
    }

    const DOTS: [char; 4] = ['.', '\u{3002}', '\u{FF0E}', '\u{FF61}'];
    let host = host.strip_suffix(DOTS).unwrap_or(host);
    if host.chars().take(254).count() > 253 {
        return false;
    }

    let mut length = 0;
    for label in host.split(DOTS) {
        let decoded;
        let unicode =
            if let Some(punycode) = label.strip_prefix("xn--").filter(|_| label.is_ascii()) {
                let Some(value) = idna::punycode::decode_to_string(punycode) else {
                    return false;
                };
                decoded = value;
                decoded.as_str()
            } else {
                label
            };
        if !valid_label(unicode) {
            return false;
        }
        // Check length, hyphens, bidirectional text and joiner context per
        // label. The RFC 5892 checks above prevent UTS #46 from widening the
        // set of accepted input characters or normalizing invalid input.
        let Ok(ascii) = idna::domain_to_ascii_strict(label) else {
            return false;
        };
        length += ascii.len() + 1;
    }
    length <= 254
}

fn valid_label(label: &str) -> bool {
    if label.is_empty() || !is_nfc(label) {
        return false;
    }
    let chars: Vec<_> = label.chars().collect();
    chars.iter().copied().enumerate().all(|(index, ch)| {
        let scalar = u32::from(ch);
        let count = pvalid::PVALID.partition_point(|&(start, _)| start <= scalar);
        let protocol_valid = count != 0 && scalar <= pvalid::PVALID[count - 1].1;
        protocol_valid || valid_context(&chars, index, ch)
    })
}

fn valid_context(label: &[char], index: usize, ch: char) -> bool {
    static GREEK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\p{Greek}").unwrap());
    static HEBREW: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\p{Hebrew}").unwrap());
    static JAPANESE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"[\p{Han}\p{Hiragana}\p{Katakana}]").unwrap());

    let previous = index.checked_sub(1).map(|i| label[i]);
    let next = label.get(index + 1).copied();
    let matches_script = |ch: char, script: &Regex| script.is_match(ch.encode_utf8(&mut [0; 4]));
    match ch {
        // The IDNA library checks joiner context.
        '\u{200C}' | '\u{200D}' => true,
        '\u{00B7}' => previous == Some('l') && next == Some('l'),
        '\u{0375}' => next.is_some_and(|ch| matches_script(ch, &GREEK)),
        '\u{05F3}' | '\u{05F4}' => previous.is_some_and(|ch| matches_script(ch, &HEBREW)),
        '\u{30FB}' => label
            .iter()
            .copied()
            .any(|ch| matches_script(ch, &JAPANESE)),
        '\u{0660}'..='\u{0669}' => !label
            .iter()
            .any(|&ch| matches!(ch, '\u{06F0}'..='\u{06F9}')),
        '\u{06F0}'..='\u{06F9}' => !label
            .iter()
            .any(|&ch| matches!(ch, '\u{0660}'..='\u{0669}')),
        _ => false,
    }
}
