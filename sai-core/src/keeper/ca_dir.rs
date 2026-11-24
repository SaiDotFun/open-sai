//! Read certificates laid out by `openssl rehash`.

use std::{collections::BTreeMap, fs, path::Path};

use rustls::{
    RootCertStore,
    pki_types::{CertificateDer, pem::PemObject},
};
use x509_cert::{
    Certificate,
    der::{Decode, Encode, Tag, Tagged, asn1::Any},
    name::RelativeDistinguishedName,
};

pub(super) fn add_roots(roots: &mut RootCertStore, dir: &Path) {
    let mut groups = BTreeMap::<u32, BTreeMap<usize, _>>::new();
    for entry in fs::read_dir(dir).into_iter().flatten().flatten() {
        let name = entry.file_name();
        if let Some((hash, index)) = name.to_str().and_then(hash_and_index) {
            groups.entry(hash).or_default().insert(index, entry.path());
        }
    }
    for (hash, entries) in groups {
        for (expected, (index, path)) in entries.into_iter().enumerate() {
            if expected != index || !path.is_file() {
                break;
            }
            let Ok(certs) = CertificateDer::pem_file_iter(path) else {
                break;
            };
            let mut loaded = false;
            for cert in certs.flatten() {
                loaded = true;
                if subject_hash(&cert) == Some(hash) {
                    let _ = roots.add(cert);
                }
            }
            if !loaded {
                break;
            }
        }
    }
}

fn hash_and_index(name: &str) -> Option<(u32, usize)> {
    let (hash, index) = name.split_once('.')?;
    let parsed_hash = u32::from_str_radix(hash, 16).ok()?;
    let parsed_index = index.parse::<usize>().ok()?;
    (hash == format!("{parsed_hash:08x}") && index == parsed_index.to_string())
        .then_some((parsed_hash, parsed_index))
}

fn subject_hash(cert: &[u8]) -> Option<u32> {
    let cert = Certificate::from_der(cert).ok()?;
    let mut digest = ring::digest::Context::new(&ring::digest::SHA1_FOR_LEGACY_USE_ONLY);
    // X509_NAME_hash hashes canonical DER RDNs without the outer SEQUENCE.
    // For the directory string types OpenSSL canonicalizes, convert to UTF-8,
    // collapse ASCII whitespace and lowercase ASCII letters before encoding.
    for rdn in cert.tbs_certificate.subject.0 {
        let mut attributes = Vec::new();
        for mut attribute in rdn.0.into_vec() {
            if let Some(text) = canonical_string(&attribute.value)? {
                attribute.value = Any::new(Tag::Utf8String, text.into_bytes()).ok()?;
            }
            attributes.push(attribute);
        }
        let rdn = RelativeDistinguishedName(attributes.try_into().ok()?);
        digest.update(&rdn.to_der().ok()?);
    }
    let digest = digest.finish();
    Some(u32::from_le_bytes(
        digest.as_ref().get(..4)?.try_into().ok()?,
    ))
}

fn canonical_string(value: &Any) -> Option<Option<String>> {
    let bytes = value.value();
    let text = match value.tag() {
        Tag::Utf8String => std::str::from_utf8(bytes).ok()?.to_owned(),
        Tag::PrintableString | Tag::TeletexString | Tag::Ia5String | Tag::VisibleString => {
            bytes.iter().copied().map(char::from).collect()
        }
        Tag::BmpString => {
            let (words, rest) = bytes.as_chunks::<2>();
            if !rest.is_empty() {
                return None;
            }
            String::from_utf16(
                &words
                    .iter()
                    .copied()
                    .map(u16::from_be_bytes)
                    .collect::<Vec<_>>(),
            )
            .ok()?
        }
        _ => return Some(None),
    };
    let text = text
        .split([' ', '\t', '\n', '\r', '\x0b', '\x0c'])
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
        .to_ascii_lowercase();
    Some(Some(text))
}
