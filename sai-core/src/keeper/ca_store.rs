//! Build a Rustls trust store from the CA files and OpenSSL trust policies.

use std::{
    collections::{BTreeMap, HashSet, VecDeque},
    sync::Arc,
};

use der::{Decode, asn1::OctetString};

use rustls::{
    CertificateError, ClientConfig, DigitallySignedStruct, Error, RootCertStore, SignatureScheme,
    client::{
        WebPkiServerVerifier,
        danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
    },
    crypto::CryptoProvider,
    pki_types::{CertificateDer, ServerName, UnixTime},
};
use x509_cert::{
    Certificate,
    ext::pkix::{AuthorityKeyIdentifier, SubjectKeyIdentifier},
    time::Validity,
};

use super::ca_pem::{CaCertificate, ServerTrust};

pub(super) struct CaStore {
    pub roots: RootCertStore,
    seen: HashSet<CertificateDer<'static>>,
    rejected: HashSet<CertificateDer<'static>>,
    rejected_anchors: RootCertStore,
    intermediates: Vec<CertificateDer<'static>>,
    dated_anchors: Vec<(usize, Validity)>,
}

impl CaStore {
    pub fn new() -> Self {
        Self {
            roots: RootCertStore::empty(),
            seen: HashSet::new(),
            rejected: HashSet::new(),
            rejected_anchors: RootCertStore::empty(),
            intermediates: Vec::new(),
            dated_anchors: Vec::new(),
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
                self.roots.add(cert.der.clone())?;
                if let Some(validity) = cert.trusted_pem_validity {
                    self.dated_anchors.push((index, validity));
                }
            }
            ServerTrust::Rejected => {
                self.rejected_anchors.add(cert.der.clone())?;
                self.rejected.insert(cert.der.clone());
            }
            ServerTrust::Untrusted => self.intermediates.push(cert.der.clone()),
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
            && self.dated_anchors.is_empty())
            || self.roots.is_empty()
        {
            builder.with_root_certificates(self.roots)
        } else {
            let roots = Arc::new(self.roots);
            let verifier = WebPkiServerVerifier::builder_with_provider(
                Arc::clone(&roots),
                Arc::clone(&provider),
            )
            .build()
            .expect("nonempty root store and no CRLs");
            builder
                .dangerous()
                .with_custom_certificate_verifier(Arc::new(StoreVerifier {
                    verifier,
                    rejected: self.rejected,
                    intermediates: self.intermediates,
                    roots,
                    provider,
                    dated_anchors: self.dated_anchors,
                }))
        };
        let mut config = builder.with_no_client_auth();
        config.alpn_protocols = vec![b"http/1.1".to_vec()];
        config
    }
}

#[derive(Debug)]
struct StoreVerifier {
    verifier: Arc<WebPkiServerVerifier>,
    rejected: HashSet<CertificateDer<'static>>,
    intermediates: Vec<CertificateDer<'static>>,
    roots: Arc<RootCertStore>,
    provider: Arc<CryptoProvider>,
    dated_anchors: Vec<(usize, Validity)>,
}

impl StoreVerifier {
    fn verifier_at(&self, now: UnixTime) -> Result<Arc<WebPkiServerVerifier>, Error> {
        // WebPKI anchors omit validity. Use each certificate's position in the
        // root store so a different certificate with the same key stays eligible.
        let invalid: HashSet<_> = self
            .dated_anchors
            .iter()
            .filter_map(|(index, validity)| {
                let start = validity.not_before.to_unix_duration().as_secs();
                let end = validity.not_after.to_unix_duration().as_secs();
                (!(start..end).contains(&now.as_secs())).then_some(*index)
            })
            .collect();
        if invalid.is_empty() {
            return Ok(Arc::clone(&self.verifier));
        }
        let roots = RootCertStore::from_iter(
            self.roots
                .roots
                .iter()
                .enumerate()
                .filter(|(index, _)| !invalid.contains(index))
                .map(|(_, root)| root.clone()),
        );
        WebPkiServerVerifier::builder_with_provider(roots.into(), Arc::clone(&self.provider))
            .build()
            .map_err(|_| CertificateError::UnknownIssuer.into())
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
        ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, Error> {
        if self.rejected.contains(end_entity) {
            return Err(CertificateError::ApplicationVerificationFailure.into());
        }
        // A peer may send a rejected CA as an intermediate under a trusted root.
        let intermediates = prioritize_issuers(
            end_entity,
            intermediates
                .iter()
                .filter(|cert| !self.rejected.contains(*cert))
                .chain(&self.intermediates)
                .cloned(),
        );
        self.verifier_at(now)?.verify_server_cert(
            end_entity,
            &intermediates,
            server_name,
            ocsp_response,
            now,
        )
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        self.verifier.verify_tls12_signature(message, cert, dss)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        self.verifier.verify_tls13_signature(message, cert, dss)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.verifier.supported_verify_schemes()
    }
}
