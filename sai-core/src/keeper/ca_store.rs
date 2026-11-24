//! Build a Rustls trust store from the CA files and OpenSSL trust policies.

use std::{collections::HashSet, sync::Arc};

use rustls::{
    CertificateError, ClientConfig, DigitallySignedStruct, Error, RootCertStore, SignatureScheme,
    client::{
        WebPkiServerVerifier,
        danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
    },
    pki_types::{CertificateDer, ServerName, UnixTime},
};

use super::ca_pem::{CaCertificate, ServerTrust};

pub(super) struct CaStore {
    pub roots: RootCertStore,
    seen: HashSet<CertificateDer<'static>>,
    rejected: HashSet<CertificateDer<'static>>,
    rejected_anchors: RootCertStore,
    intermediates: Vec<CertificateDer<'static>>,
}

impl CaStore {
    pub fn new() -> Self {
        Self {
            roots: RootCertStore::empty(),
            seen: HashSet::new(),
            rejected: HashSet::new(),
            rejected_anchors: RootCertStore::empty(),
            intermediates: Vec::new(),
        }
    }

    pub fn add(&mut self, cert: CaCertificate) -> Result<(), Error> {
        // OpenSSL retains the trust policy of the first copy of a certificate.
        if self.seen.contains(&cert.der) {
            return Ok(());
        }
        match cert.trust {
            ServerTrust::Anchor => self.roots.add(cert.der.clone())?,
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
        let builder = if (self.rejected.is_empty() && self.intermediates.is_empty())
            || self.roots.is_empty()
        {
            builder.with_root_certificates(self.roots)
        } else {
            let verifier = WebPkiServerVerifier::builder_with_provider(self.roots.into(), provider)
                .build()
                .expect("nonempty root store and no CRLs");
            builder
                .dangerous()
                .with_custom_certificate_verifier(Arc::new(StoreVerifier {
                    verifier,
                    rejected: self.rejected,
                    intermediates: self.intermediates,
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
        let intermediates: Vec<_> = intermediates
            .iter()
            .filter(|cert| !self.rejected.contains(*cert))
            .chain(&self.intermediates)
            .cloned()
            .collect();
        self.verifier.verify_server_cert(
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
