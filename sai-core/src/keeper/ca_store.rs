//! Build a Rustls trust store from the CA files and OpenSSL trust policies.

use std::{
    collections::{BTreeMap, HashSet, VecDeque},
    sync::Arc,
};

use der::{Decode, asn1::OctetString};

use rustls::{
    CertificateError, ClientConfig, DigitallySignedStruct, Error, OtherError, RootCertStore,
    SignatureScheme,
    client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
    crypto::{CryptoProvider, verify_tls12_signature, verify_tls13_signature},
    pki_types::{CertificateDer, ServerName, UnixTime},
};
use webpki::{EndEntityCert, KeyUsage, VerifiedPath};
use x509_cert::{
    Certificate, TbsCertificate,
    ext::pkix::{AuthorityKeyIdentifier, SubjectKeyIdentifier},
    time::Validity,
};

use super::ca_pem::{CaCertificate, ServerTrust, anchor_path_len, can_issue, same_name};

pub(super) struct CaStore {
    pub roots: RootCertStore,
    seen: HashSet<CertificateDer<'static>>,
    rejected: HashSet<CertificateDer<'static>>,
    rejected_anchors: RootCertStore,
    intermediates: Vec<LocalIntermediate>,
    anchor_constraints: Vec<(usize, AnchorConstraints)>,
}

impl CaStore {
    pub fn new() -> Self {
        Self {
            roots: RootCertStore::empty(),
            seen: HashSet::new(),
            rejected: HashSet::new(),
            rejected_anchors: RootCertStore::empty(),
            intermediates: Vec::new(),
            anchor_constraints: Vec::new(),
        }
    }

    pub fn add(&mut self, cert: CaCertificate) -> Result<(), Error> {
        // OpenSSL retains the trust policy of the first copy of a certificate.
        if self.seen.contains(&cert.der) {
            return Ok(());
        }
        match cert.trust {
            ServerTrust::Anchor => {
                let index = self.roots.len();
                let path_len =
                    anchor_path_len(&cert.tbs).map_err(|_| CertificateError::BadEncoding)?;
                self.roots.add(cert.der.clone())?;
                if path_len.is_some() || cert.anchor_validity.is_some() {
                    self.anchor_constraints.push((
                        index,
                        AnchorConstraints {
                            validity: cert.anchor_validity,
                            path_len,
                        },
                    ));
                }
            }
            ServerTrust::Rejected => {
                self.rejected_anchors.add(cert.der.clone())?;
                self.rejected.insert(cert.der.clone());
            }
            ServerTrust::Untrusted => self.intermediates.push(LocalIntermediate {
                der: cert.der.clone(),
                tbs: cert.tbs,
            }),
        }
        self.seen.insert(cert.der);
        Ok(())
    }

    pub fn add_bundled_roots(&mut self) {
        self.roots.extend(
            webpki_roots::TLS_SERVER_ROOTS
                .iter()
                .filter(|root| !self.rejected_anchors.roots.contains(root))
                .cloned(),
        );
    }

    pub fn tls_config(self) -> ClientConfig {
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let builder = ClientConfig::builder_with_provider(Arc::clone(&provider))
            .with_safe_default_protocol_versions()
            .expect("valid Rustls protocol versions");
        let builder = if (self.rejected.is_empty()
            && self.intermediates.is_empty()
            && self.anchor_constraints.is_empty())
            || self.roots.is_empty()
        {
            builder.with_root_certificates(self.roots)
        } else {
            builder
                .dangerous()
                .with_custom_certificate_verifier(Arc::new(StoreVerifier {
                    rejected: self.rejected,
                    intermediates: self.intermediates,
                    roots: self.roots,
                    provider,
                    anchor_constraints: self.anchor_constraints,
                }))
        };
        let mut config = builder.with_no_client_auth();
        config.alpn_protocols = vec![b"http/1.1".to_vec()];
        config
    }
}

// WebPKI omits these fields when converting a certificate to a trust anchor.
#[derive(Debug)]
struct AnchorConstraints {
    validity: Option<Validity>,
    path_len: Option<usize>,
}

impl AnchorConstraints {
    fn verify(&self, path: &VerifiedPath<'_>, now: UnixTime) -> Result<(), webpki::Error> {
        if let Some(validity) = self.validity {
            let start = validity.not_before.to_unix_duration().as_secs();
            let end = validity.not_after.to_unix_duration().as_secs();
            if !(start..end).contains(&now.as_secs()) {
                return Err(webpki::Error::UnknownIssuer);
            }
        }
        if let Some(limit) = self.path_len {
            let mut count = 0;
            for intermediate in path.intermediate_certificates() {
                let tbs = Certificate::from_der(&intermediate.der())
                    .map_err(|_| webpki::Error::BadDer)?
                    .tbs_certificate;
                if !same_name(&tbs.subject, &tbs.issuer) {
                    count += 1;
                }
            }
            if count > limit {
                return Err(webpki::Error::PathLenConstraintViolated);
            }
        }
        Ok(())
    }
}

#[derive(Debug)]
struct LocalIntermediate {
    der: CertificateDer<'static>,
    tbs: TbsCertificate,
}

#[derive(Debug)]
struct StoreVerifier {
    rejected: HashSet<CertificateDer<'static>>,
    intermediates: Vec<LocalIntermediate>,
    roots: RootCertStore,
    provider: Arc<CryptoProvider>,
    anchor_constraints: Vec<(usize, AnchorConstraints)>,
}

impl StoreVerifier {
    fn verify_path(&self, path: &VerifiedPath<'_>, now: UnixTime) -> Result<(), webpki::Error> {
        // Match the selected anchor by position. Two certificates can have the
        // same name and key with different validity or path length.
        if let Some((_, constraints)) = self
            .anchor_constraints
            .iter()
            .find(|(index, _)| std::ptr::eq(path.anchor(), &self.roots.roots[*index]))
        {
            constraints.verify(path, now)?;
        }
        if self.intermediates.is_empty() {
            return Ok(());
        }
        let mut local_only = false;
        let mut child = path.end_entity().der();
        for issuer in path.intermediate_certificates() {
            let issuer = issuer.der();
            let child_tbs = Certificate::from_der(&child)
                .map_err(|_| webpki::Error::BadDer)?
                .tbs_certificate;
            if let Some(local) = self.intermediates.iter().find(|local| local.der == issuer) {
                if !can_issue(&local.tbs, &child_tbs).unwrap_or(false) {
                    return Err(webpki::Error::UnknownIssuer);
                }
                local_only = true;
            } else if local_only
                || self
                    .intermediates
                    .iter()
                    .any(|local| can_issue(&local.tbs, &child_tbs).unwrap_or(false))
            {
                // OpenSSL prefers a store issuer and keeps looking in the store
                // after selecting one. A peer certificate cannot fill a gap above it.
                return Err(webpki::Error::UnknownIssuer);
            }
            child = issuer;
        }
        Ok(())
    }
}

fn issuer_key_ids(cert: &CertificateDer<'_>) -> (Option<OctetString>, Option<OctetString>) {
    let Ok(cert) = Certificate::from_der(cert) else {
        return (None, None);
    };
    let tbs = cert.tbs_certificate;
    let subject = tbs
        .get::<SubjectKeyIdentifier>()
        .ok()
        .flatten()
        .map(|(_, id)| id.0);
    let authority = tbs
        .get::<AuthorityKeyIdentifier>()
        .ok()
        .flatten()
        .and_then(|(_, id)| id.key_identifier);
    (subject, authority)
}

fn prioritize_issuers<'a>(
    leaf: &CertificateDer<'_>,
    candidates: impl Iterator<Item = CertificateDer<'a>>,
) -> Vec<CertificateDer<'a>> {
    let mut by_subject_key = BTreeMap::<_, Vec<_>>::new();
    let mut candidates: Vec<_> = candidates
        .enumerate()
        .map(|(index, cert)| {
            let (subject, authority) = issuer_key_ids(&cert);
            if let Some(subject) = subject {
                by_subject_key.entry(subject).or_default().push(index);
            }
            (Some(cert), authority)
        })
        .collect();
    let mut ordered = Vec::with_capacity(candidates.len());
    let mut authority_keys: VecDeque<_> = issuer_key_ids(leaf).1.into_iter().collect();
    // WebPKI tries candidates by name and caps signature checks. Give it the
    // key-ID matches along the leaf's chain first, with other candidates as fallback.
    while let Some(key) = authority_keys.pop_front() {
        for index in by_subject_key.remove(&key).into_iter().flatten() {
            let (cert, issuer_key) = &mut candidates[index];
            ordered.extend(cert.take());
            authority_keys.extend(issuer_key.take());
        }
    }
    ordered.extend(candidates.into_iter().filter_map(|(cert, _)| cert));
    ordered
}

impl ServerCertVerifier for StoreVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, Error> {
        if self.rejected.contains(end_entity) {
            return Err(CertificateError::ApplicationVerificationFailure.into());
        }
        // A peer may send a rejected CA as an intermediate under a trusted root.
        let intermediates = prioritize_issuers(
            end_entity,
            self.intermediates
                .iter()
                .map(|cert| cert.der.clone())
                .chain(
                    intermediates
                        .iter()
                        .filter(|cert| !self.rejected.contains(*cert))
                        .cloned(),
                ),
        );
        let as_certificate_error = |err| CertificateError::Other(OtherError(Arc::new(err)));
        let leaf = EndEntityCert::try_from(end_entity).map_err(as_certificate_error)?;
        leaf.verify_for_usage(
            self.provider.signature_verification_algorithms.all,
            &self.roots.roots,
            &intermediates,
            now,
            KeyUsage::server_auth(),
            None,
            Some(&|path| self.verify_path(path, now)),
        )
        .map_err(as_certificate_error)?;
        leaf.verify_is_valid_for_subject_name(server_name)
            .map_err(as_certificate_error)?;
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        verify_tls12_signature(
            message,
            cert,
            dss,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        verify_tls13_signature(
            message,
            cert,
            dss,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider
            .signature_verification_algorithms
            .supported_schemes()
    }
}
