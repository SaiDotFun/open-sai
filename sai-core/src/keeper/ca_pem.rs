//! Read PEM CA certificates and OpenSSL's optional X509_CERT_AUX trust policy.

use std::{
    io::{self, BufRead, BufReader},
    iter,
    path::Path,
};

use base64::{Engine, engine::general_purpose::STANDARD};
use der::{
    Reader, Sequence, SliceReader,
    asn1::{ObjectIdentifier, OctetString, Uint},
    oid::{
        AssociatedOid,
        db::rfc5912::{
            DSA_WITH_SHA_1, ID_DSA, ID_EC_PUBLIC_KEY, ID_RSASSA_PSS, PKCS_1, RSA_ENCRYPTION,
        },
    },
};
use rustls::pki_types::CertificateDer;
use x509_cert::{
    Certificate, TbsCertificate,
    ext::pkix::{
        AuthorityKeyIdentifier, BasicConstraints, KeyUsage, SubjectKeyIdentifier, name::GeneralName,
    },
    name::Name,
    spki::AlgorithmIdentifierOwned,
    time::Validity,
};

use super::ca_dir::canonical_name;

const SERVER_AUTH: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.3.6.1.5.5.7.3.1");
const ANY_EXTENDED_KEY_USAGE: ObjectIdentifier = ObjectIdentifier::new_unwrap("2.5.29.37.0");

pub(super) struct CaCertificate {
    pub der: CertificateDer<'static>,
    pub tbs: TbsCertificate,
    pub trust: ServerTrust,
    pub anchor_validity: Option<Validity>,
}

pub(super) enum ServerTrust {
    Anchor,
    Rejected,
    Untrusted,
}

#[derive(Sequence)]
struct CertificateAux {
    trust: Option<Vec<ObjectIdentifier>>,
    #[asn1(context_specific = "0", tag_mode = "IMPLICIT", optional = "true")]
    reject: Option<Vec<ObjectIdentifier>>,
    alias: Option<String>,
    key_id: Option<OctetString>,
    #[asn1(context_specific = "1", tag_mode = "IMPLICIT", optional = "true")]
    other: Option<Vec<AlgorithmIdentifierOwned>>,
}

pub(super) fn certificates(
    path: &Path,
) -> io::Result<impl Iterator<Item = io::Result<CaCertificate>>> {
    let mut lines = Some(BufReader::new(crate::files::open_regular(path)?).split(b'\n'));
    Ok(iter::from_fn(move || {
        let result = read_certificate(lines.as_mut()?);
        if result.is_err() {
            lines = None;
        }
        result.transpose()
    }))
}

fn read_certificate(
    lines: &mut impl Iterator<Item = io::Result<Vec<u8>>>,
) -> io::Result<Option<CaCertificate>> {
    for line in lines.by_ref() {
        let line = line?;
        let (end, trusted) = match line.trim_ascii_end() {
            b"-----BEGIN CERTIFICATE-----" => (b"-----END CERTIFICATE-----".as_slice(), false),
            b"-----BEGIN TRUSTED CERTIFICATE-----" => {
                (b"-----END TRUSTED CERTIFICATE-----".as_slice(), true)
            }
            _ => continue,
        };
        let mut encoded = Vec::new();
        for line in lines {
            let line = line?;
            if line.trim_ascii_end() == end {
                let der = STANDARD.decode(encoded).map_err(io::Error::other)?;
                let cert = certificate_entry(der, trusted).map_err(io::Error::other)?;
                return Ok(Some(cert));
            }
            if line.starts_with(b"-----") {
                break;
            }
            encoded.extend(line.into_iter().filter(|byte| !byte.is_ascii_whitespace()));
            if encoded.len() > 256 * 1024 * 1024 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "PEM certificate too large",
                ));
            }
        }
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Incomplete PEM certificate",
        ));
    }
    Ok(None)
}

fn certificate_entry(mut der: Vec<u8>, trusted: bool) -> der::Result<CaCertificate> {
    let mut reader = SliceReader::new(&der)?;
    let certificate: Certificate = reader.decode()?;
    let certificate_len = usize::try_from(reader.position())?;
    let aux: Option<CertificateAux> = if trusted && !reader.is_finished() {
        // OpenSSL reads one AUX object and leaves any padding after it unread.
        Some(reader.decode::<CertificateAux>()?)
    } else {
        reader.finish(())?;
        None
    };

    let matches_server = |purposes: &[ObjectIdentifier]| {
        purposes.contains(&SERVER_AUTH) || purposes.contains(&ANY_EXTENDED_KEY_USAGE)
    };
    let trust = match aux {
        Some(aux) if aux.reject.as_deref().is_some_and(matches_server) => ServerTrust::Rejected,
        Some(aux) if aux.trust.is_some() => {
            if aux.trust.as_deref().is_some_and(matches_server) {
                ServerTrust::Anchor
            } else {
                ServerTrust::Rejected
            }
        }
        _ if eligible_for_default_trust(&certificate)? => ServerTrust::Anchor,
        _ => ServerTrust::Untrusted,
    };
    // OpenSSL's full-chain verifier checks self-issued anchors and skips the
    // dates of an explicitly trusted terminal intermediate.
    let anchor_validity = (matches!(trust, ServerTrust::Anchor)
        && eligible_for_default_trust(&certificate)?)
    .then_some(certificate.tbs_certificate.validity);
    der.truncate(certificate_len);
    Ok(CaCertificate {
        der: der.into(),
        tbs: certificate.tbs_certificate,
        trust,
        anchor_validity,
    })
}

fn eligible_for_default_trust(cert: &Certificate) -> der::Result<bool> {
    likely_issuer(&cert.tbs_certificate, &cert.tbs_certificate)
}

pub(super) fn can_issue(issuer: &TbsCertificate, child: &TbsCertificate) -> der::Result<bool> {
    Ok(likely_issuer(issuer, child)?
        && issuer
            .get::<KeyUsage>()?
            .is_none_or(|(_, usage)| usage.key_cert_sign()))
}

fn likely_issuer(issuer: &TbsCertificate, child: &TbsCertificate) -> der::Result<bool> {
    if !same_name(&issuer.subject, &child.issuer)
        || !signature_matches_key(
            child.signature.oid,
            issuer.subject_public_key_info.algorithm.oid,
        )
    {
        return Ok(false);
    }
    let subject_key = issuer.get::<SubjectKeyIdentifier>()?.map(|(_, id)| id.0);
    let Some((_, authority)) = child.get::<AuthorityKeyIdentifier>()? else {
        return Ok(true);
    };
    let issuer_name = authority
        .authority_cert_issuer
        .iter()
        .flatten()
        .find_map(|name| {
            if let GeneralName::DirectoryName(name) = name {
                Some(name)
            } else {
                None
            }
        });
    // OpenSSL compares the authority serial and first directory name when present.
    Ok(
        !matches!((&authority.key_identifier, &subject_key), (Some(a), Some(b)) if a != b)
            && authority
                .authority_cert_serial_number
                .as_ref()
                .is_none_or(|serial| serial == &issuer.serial_number)
            && issuer_name.is_none_or(|name| same_name(name, &issuer.issuer)),
    )
}

// The x509-cert BasicConstraints type stores the limit in a u8.
#[derive(Sequence)]
struct AnchorBasicConstraints {
    #[asn1(default = "Default::default")]
    ca: bool,
    path_len_constraint: Option<Uint>,
}

impl AssociatedOid for AnchorBasicConstraints {
    const OID: ObjectIdentifier = BasicConstraints::OID;
}

pub(super) fn anchor_path_len(tbs: &TbsCertificate) -> der::Result<Option<usize>> {
    Ok(tbs
        .get::<AnchorBasicConstraints>()?
        .and_then(|(_, constraints)| constraints.path_len_constraint)
        .map(|limit| {
            limit.as_bytes().iter().fold(0usize, |value, &byte| {
                value.saturating_mul(256).saturating_add(usize::from(byte))
            })
        }))
}

pub(super) fn same_name(left: &Name, right: &Name) -> bool {
    left == right
        || canonical_name(left)
            .zip(canonical_name(right))
            .is_some_and(|(left, right)| left == right)
}

fn signature_matches_key(signature: ObjectIdentifier, key: ObjectIdentifier) -> bool {
    const ECDSA_SHA1: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.10045.4.1");
    const ECDSA_SHA2: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.10045.4.3");
    const NIST_SIGNATURES: ObjectIdentifier = ObjectIdentifier::new_unwrap("2.16.840.1.101.3.4.3");
    const ED25519: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.3.101.112");
    const ED448: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.3.101.113");
    let parent = signature.parent();
    let last = signature.arcs().last();
    let nist = parent == Some(NIST_SIGNATURES);
    match key {
        RSA_ENCRYPTION => {
            (parent == Some(PKCS_1) && matches!(last, Some(2..=5 | 10..=16)))
                || (nist && matches!(last, Some(13..=16)))
        }
        ID_EC_PUBLIC_KEY => {
            signature == ECDSA_SHA1
                || (parent == Some(ECDSA_SHA2) && matches!(last, Some(1..=4)))
                || (nist && matches!(last, Some(9..=12)))
        }
        ID_DSA => signature == DSA_WITH_SHA_1 || (nist && matches!(last, Some(1..=8))),
        ID_RSASSA_PSS | ED25519 | ED448 => signature == key,
        _ => false,
    }
}
