//! Read PEM CA certificates and OpenSSL's optional X509_CERT_AUX trust policy.

use std::{
    fs::File,
    io::{self, BufRead, BufReader},
    iter,
    path::Path,
};

use base64::{Engine, engine::general_purpose::STANDARD};
use der::{
    Reader, Sequence, SliceReader,
    asn1::{ObjectIdentifier, OctetString},
};
use rustls::pki_types::CertificateDer;
use x509_cert::{Certificate, spki::AlgorithmIdentifierOwned};

const SERVER_AUTH: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.3.6.1.5.5.7.3.1");
const ANY_EXTENDED_KEY_USAGE: ObjectIdentifier = ObjectIdentifier::new_unwrap("2.5.29.37.0");

pub(super) struct CaCertificate {
    pub der: CertificateDer<'static>,
    pub trust: ServerTrust,
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
    let mut lines = Some(BufReader::new(File::open(path)?).split(b'\n'));
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
                let cert = if trusted {
                    trusted_certificate(der).map_err(io::Error::other)?
                } else {
                    CaCertificate {
                        der: der.into(),
                        trust: ServerTrust::Anchor,
                    }
                };
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

fn trusted_certificate(mut der: Vec<u8>) -> der::Result<CaCertificate> {
    let mut reader = SliceReader::new(&der)?;
    let certificate: Certificate = reader.decode()?;
    let certificate_len = usize::try_from(reader.position())?;
    let aux: Option<CertificateAux> = reader.decode()?;
    reader.finish(())?;

    let matches_server = |purposes: &[ObjectIdentifier]| {
        purposes.contains(&SERVER_AUTH) || purposes.contains(&ANY_EXTENDED_KEY_USAGE)
    };
    let self_issued = certificate.tbs_certificate.subject == certificate.tbs_certificate.issuer;
    let trust = match aux {
        Some(aux) if aux.reject.as_deref().is_some_and(matches_server) => ServerTrust::Rejected,
        Some(aux) if aux.trust.is_some() => {
            if aux.trust.as_deref().is_some_and(matches_server) {
                ServerTrust::Anchor
            } else {
                ServerTrust::Rejected
            }
        }
        _ if self_issued => ServerTrust::Anchor,
        _ => ServerTrust::Untrusted,
    };
    der.truncate(certificate_len);
    Ok(CaCertificate {
        der: der.into(),
        trust,
    })
}
