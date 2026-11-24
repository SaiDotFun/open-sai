//! One HTTP/1.1 request with timeouts on connection, socket reads, and writes.

use std::{
    env, ffi::OsStr, future::ready, io, io::Read as _, path::Path, sync::Arc, time::Duration,
};

use flate2::read::{DeflateDecoder, MultiGzDecoder, ZlibDecoder};
use http_body_util::{BodyExt, Full};
use hyper::{Request, StatusCode, Uri, body::Bytes, client::conn::http1, header};
use hyper_timeout::TimeoutConnector;
use hyper_util::{
    client::legacy::connect::{Connected, Connection, proxy::Tunnel},
    rt::TokioIo,
};
use rustls::{ClientConfig, pki_types::ServerName};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    net::{TcpStream, lookup_host},
    time,
};
use tokio_rustls::TlsConnector;
use tower::{ServiceExt, service_fn};
use url::Url;

use super::{ca_dir, ca_pem, ca_store::CaStore, not_sent, proxy, request_failed};
use crate::errors::SaiError;

trait Socket: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Socket for T {}
type SocketBox = Box<dyn Socket>;

impl Connection for SocketBox {
    fn connected(&self) -> Connected {
        Connected::new()
    }
}

pub(super) async fn post(
    endpoint: &str,
    body: Vec<u8>,
    token: Option<&str>,
    timeout: Duration,
) -> Result<(StatusCode, Vec<u8>), SaiError> {
    let mut url = Url::parse(endpoint).map_err(|_| request_failed())?;
    url.set_fragment(None);
    let destination = url.as_str().parse::<Uri>().map_err(|_| request_failed())?;
    let authority = destination.authority().ok_or_else(request_failed)?;
    let proxy = proxy::for_endpoint(&url, &destination);
    let forward = proxy.is_some() && destination.scheme_str() == Some("http");
    let target = if forward {
        destination.clone()
    } else {
        Uri::from(
            destination
                .path_and_query()
                .ok_or_else(request_failed)?
                .clone(),
        )
    };
    let mut request = Request::post(target)
        .header(header::HOST, authority.as_str())
        .header(header::ACCEPT, "*/*")
        .header(header::ACCEPT_ENCODING, "gzip, deflate")
        .header(header::CONNECTION, "keep-alive")
        .header(header::USER_AGENT, "python-httpx/0.28.1")
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::CONTENT_LENGTH, body.len());
    if let Some(token) = token {
        let mut authorization = header::HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|_| request_failed())?;
        authorization.set_sensitive(true);
        request = request.header(header::AUTHORIZATION, authorization);
    }
    if forward && let Some(auth) = proxy.as_ref().and_then(|proxy| proxy.basic_auth()) {
        request = request.header(header::PROXY_AUTHORIZATION, auth);
    }
    let request = request
        .body(Full::new(Bytes::from(body)))
        .map_err(|_| request_failed())?;
    let tls = Arc::new(keeper_tls_config()?);
    let socket = if let Some(proxy) = proxy {
        let proxy_tls = if proxy.uri().scheme_str() == Some("https") {
            Arc::new(proxy_tls_config())
        } else {
            Arc::clone(&tls)
        };
        let socket = connect(proxy.uri(), &proxy_tls, timeout).await?;
        if forward {
            socket
        } else {
            let mut socket = Some(with_inactivity(socket, timeout).await);
            let connector = service_fn(move |_| {
                ready(Ok::<_, io::Error>(
                    socket.take().expect("one proxy connection"),
                ))
            });
            let mut tunnel = Tunnel::new(proxy.uri().clone(), connector);
            if let Some(auth) = proxy.basic_auth() {
                tunnel = tunnel.with_auth(auth.clone());
            }
            let tunnel = tunnel
                .oneshot(destination.clone())
                .await
                .map_err(|_| request_failed())?;
            start_tls(Box::new(TokioIo::new(tunnel)), &destination, tls, timeout).await?
        }
    } else {
        connect(&destination, &tls, timeout).await?
    };
    exchange(socket, request, timeout).await
}

fn host(destination: &Uri) -> Result<&str, SaiError> {
    Ok(destination
        .host()
        .ok_or_else(request_failed)?
        .trim_matches(['[', ']']))
}

async fn connect(
    destination: &Uri,
    tls: &Arc<ClientConfig>,
    timeout: Duration,
) -> Result<SocketBox, SaiError> {
    let default_port = match destination.scheme_str() {
        Some("https") => 443,
        Some("http") => 80,
        _ => return Err(request_failed()),
    };
    let port = destination.port_u16().unwrap_or(default_port);
    let socket = connect_tcp(host(destination)?, port, timeout).await?;
    socket.set_nodelay(true).map_err(|_| not_sent())?;
    if default_port == 443 {
        start_tls(Box::new(socket), destination, Arc::clone(tls), timeout).await
    } else {
        Ok(Box::new(socket))
    }
}

async fn connect_tcp(host: &str, port: u16, timeout: Duration) -> Result<TcpStream, SaiError> {
    let addresses = time::timeout(timeout, lookup_host((host, port)))
        .await
        .map_err(|_| not_sent())?
        .map_err(|_| not_sent())?;
    for address in addresses {
        if let Ok(Ok(socket)) = time::timeout(timeout, TcpStream::connect(address)).await {
            return Ok(socket);
        }
    }
    Err(not_sent())
}

async fn start_tls(
    socket: SocketBox,
    destination: &Uri,
    config: Arc<ClientConfig>,
    timeout: Duration,
) -> Result<SocketBox, SaiError> {
    let name = ServerName::try_from(host(destination)?.to_owned()).map_err(|_| not_sent())?;
    let handshake = TlsConnector::from(config).connect(name, socket);
    let socket = time::timeout(timeout, handshake)
        .await
        .map_err(|_| not_sent())?
        .map_err(|_| not_sent())?;
    Ok(Box::new(socket))
}

async fn with_inactivity(
    socket: SocketBox,
    timeout: Duration,
) -> impl hyper::rt::Read + hyper::rt::Write + Send + Unpin {
    let mut socket = Some(TokioIo::new(socket));
    let connector =
        service_fn(move |_| ready(Ok::<_, io::Error>(socket.take().expect("one connection"))));
    let mut connector = TimeoutConnector::new(connector);
    connector.set_read_timeout(Some(timeout));
    connector.set_write_timeout(Some(timeout));
    connector.set_reset_reader_on_write(true);
    connector
        .oneshot(Uri::default())
        .await
        .expect("connected socket")
}

async fn exchange(
    socket: SocketBox,
    request: Request<Full<Bytes>>,
    timeout: Duration,
) -> Result<(StatusCode, Vec<u8>), SaiError> {
    // Like the top-level httpx.post, this exchange owns its connection. There
    // is no shared pool to wait on and no automatic retry.
    let io = with_inactivity(socket, timeout).await;
    let (mut sender, connection) = http1::Builder::new()
        .title_case_headers(true)
        .handshake(io)
        .await
        .map_err(|_| request_failed())?;
    let driver = tokio::spawn(connection);
    let result = receive(&mut sender, request).await;
    driver.abort();
    result
}

async fn receive(
    sender: &mut http1::SendRequest<Full<Bytes>>,
    request: Request<Full<Bytes>>,
) -> Result<(StatusCode, Vec<u8>), SaiError> {
    let response = sender
        .send_request(request)
        .await
        .map_err(|_| request_failed())?;
    let (parts, body) = response.into_parts();
    let bytes = body
        .collect()
        .await
        .map_err(|_| request_failed())?
        .to_bytes();
    let mut bytes = bytes.to_vec();
    let encodings: Vec<_> = parts
        .headers
        .get_all(header::CONTENT_ENCODING)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .collect();
    for encoding in encodings.into_iter().rev() {
        let mut decoded = Vec::new();
        match encoding.trim().to_ascii_lowercase().as_str() {
            "gzip" => MultiGzDecoder::new(bytes.as_slice()).read_to_end(&mut decoded),
            "deflate" => ZlibDecoder::new(bytes.as_slice())
                .read_to_end(&mut decoded)
                .or_else(|_| {
                    decoded.clear();
                    DeflateDecoder::new(bytes.as_slice()).read_to_end(&mut decoded)
                }),
            _ => continue,
        }
        .map_err(|_| request_failed())?;
        bytes = decoded;
    }
    Ok((parts.status, bytes))
}

fn keeper_tls_config() -> Result<ClientConfig, SaiError> {
    let mut store = CaStore::new();
    // HTTPX treats an explicit file or CA directory as the entire trust store.
    if let Some(file) = env::var_os("SSL_CERT_FILE").filter(|value| !value.is_empty()) {
        add_pem_file(&mut store, Path::new(&file))?;
    } else if let Some(dirs) = env::var_os("SSL_CERT_DIR").filter(|value| !value.is_empty()) {
        append_ca_dirs(&mut store, &dirs);
    } else {
        store
            .roots
            .add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
        if store.roots.is_empty() {
            store.add_bundled_roots();
        }
    }
    Ok(store.tls_config())
}

fn proxy_tls_config() -> ClientConfig {
    // HTTPcore creates the proxy's default SSL context with the OpenSSL
    // default paths, then adds its bundled Mozilla roots. The environment
    // can override the default file and directory independently.
    let mut store = CaStore::new();
    let file = env::var_os("SSL_CERT_FILE");
    let dirs = env::var_os("SSL_CERT_DIR");
    if file.is_none() && dirs.is_none() {
        store
            .roots
            .add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
        store.add_bundled_roots();
    } else {
        let defaults = openssl_probe::probe();
        if let Some(file) = file.map(std::path::PathBuf::from).or(defaults.cert_file) {
            let _ = add_pem_file(&mut store, &file);
        }
        store.add_bundled_roots();
        if let Some(dirs) = dirs {
            append_ca_dirs(&mut store, &dirs);
        } else {
            for dir in defaults.cert_dir {
                ca_dir::add_roots(&mut store, &dir);
            }
        }
    }
    store.tls_config()
}

fn append_ca_dirs(store: &mut CaStore, dirs: &OsStr) {
    for dir in env::split_paths(dirs).filter(|path| !path.as_os_str().is_empty()) {
        ca_dir::add_roots(store, &dir);
    }
}

fn add_pem_file(store: &mut CaStore, path: &Path) -> Result<(), SaiError> {
    let certs = ca_pem::certificates(path).map_err(|_| SaiError::internal())?;
    let mut loaded = false;
    for cert in certs {
        store
            .add(cert.map_err(|_| SaiError::internal())?)
            .map_err(|_| SaiError::internal())?;
        loaded = true;
    }
    if !loaded {
        return Err(SaiError::internal());
    }
    Ok(())
}
