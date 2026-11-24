use std::{
    io::{ErrorKind, Write},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::Duration,
};

use sai_core::{
    errors::Code,
    keeper::{POSITION, ROUTE, SUBMIT},
    storage::{Session, Storage},
    trade::{
        Confirmation, SubmitOptions, TradePreview, Trading, close_preview, leverage_preview,
        preview_digest, tpsl_preview,
    },
};
use serde_json::{Value, json};
use tempfile::TempDir;

use super::{EVENTS, listener, read_request};

const ACCOUNT: &str = "0x1111111111111111111111111111111111111111";
const OTHER_ACCOUNT: &str = "0x2222222222222222222222222222222222222222";
const NETWORK: &str = "fixture-chain-1";
const CONTRACT: &str = "nibi1fixturecontract";

struct ReplyState {
    keeper_owner: &'static str,
    contract_owner: &'static str,
    keeper_chain: &'static str,
    lcd_chain: &'static str,
    contract: &'static str,
    requests: Vec<String>,
}

struct PositionServer {
    url: String,
    state: Arc<Mutex<ReplyState>>,
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl PositionServer {
    fn start(keeper_owner: &'static str, contract_owner: &'static str) -> Self {
        let listener = listener();
        let url = format!("http://{}", listener.local_addr().unwrap());
        listener.set_nonblocking(true).unwrap();
        let state = Arc::new(Mutex::new(ReplyState {
            keeper_owner,
            contract_owner,
            keeper_chain: NETWORK,
            lcd_chain: NETWORK,
            contract: CONTRACT,
            requests: Vec::new(),
        }));
        let stop = Arc::new(AtomicBool::new(false));
        let replies = Arc::clone(&state);
        let stopped = Arc::clone(&stop);
        let worker = thread::spawn(move || {
            while !stopped.load(Ordering::Relaxed) {
                let mut stream = match listener.accept() {
                    Ok((stream, _)) => stream,
                    Err(error) if error.kind() == ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(1));
                        continue;
                    }
                    Err(error) => panic!("loopback listener: {error}"),
                };
                let (headers, bytes) = read_request(&mut stream);
                let response = replies
                    .lock()
                    .unwrap()
                    .response(&headers[0], &bytes)
                    .to_string();
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response}",
                    response.len(),
                )
                .unwrap();
            }
        });
        Self {
            url,
            state,
            stop,
            worker: Some(worker),
        }
    }

    fn requests(&self) -> Vec<String> {
        self.state.lock().unwrap().requests.clone()
    }
}

impl Drop for PositionServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let result = self.worker.take().unwrap().join();
        if !thread::panicking() {
            result.unwrap();
        }
    }
}

impl ReplyState {
    fn response(&mut self, request: &str, bytes: &[u8]) -> Value {
        if request.starts_with("POST /query ") {
            let request: Value = serde_json::from_slice(bytes).unwrap();
            let data = match request["query"].as_str().unwrap() {
                POSITION => {
                    assert_eq!(request["variables"], json!({"trader": ACCOUNT, "id": 7}));
                    self.requests.push("position".into());
                    json!({"perp": {"trade": {"id": 7, "trader": self.keeper_owner}}})
                }
                ROUTE => {
                    self.requests.push("route".into());
                    json!({"oneClickTrading": {
                        "enabled": true, "chainId": self.keeper_chain,
                        "contractAddress": self.contract,
                    }})
                }
                SUBMIT => {
                    self.requests.push("admission".into());
                    json!({"recordUserIntent": {
                        "intentKey": request["variables"]["input"]["intentKey"],
                        "execActor": "server", "lifecycleStep": "jwt_pass", "evmAddress": ACCOUNT,
                    }})
                }
                query => panic!("Unexpected GraphQL document {query}"),
            };
            return json!({"data": data});
        }
        if request.starts_with("GET /cosmos/base/tendermint/v1beta1/node_info ") {
            self.requests.push("node_info".into());
            return json!({"default_node_info": {"network": self.lcd_chain}});
        }
        assert!(
            request.starts_with(&format!(
                "GET /cosmwasm/wasm/v1/contract/{}/smart/",
                self.contract
            )),
            "{request}"
        );
        self.requests.push("contract_position".into());
        json!({"data": {"user_trade_index": "UserTradeIndex(7)", "user": self.contract_owner}})
    }
}

fn fixture_storage<'a>(home: &'a TempDir, endpoint: &str) -> Storage<'a> {
    let storage = Storage::new(home.path());
    storage
        .save_session(Session::new(
            "synthetic-position-session".to_owned().into(),
            ACCOUNT.into(),
            "2099-01-01T00:00:00Z".into(),
            endpoint.into(),
        ))
        .unwrap();
    storage
}

#[tokio::test]
async fn cancel_preview_rejects_a_close_before_network_reads() {
    let server = PositionServer::start(ACCOUNT, ACCOUNT);
    let endpoint = format!("{}/query", server.url);
    let home = tempfile::tempdir().unwrap();
    let storage = fixture_storage(&home, &endpoint);
    let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
    let session = trading.load_session(None).unwrap();
    let close = close_preview("7", false).unwrap();

    let error = trading.cancel_preview(&session, &close).await.unwrap_err();
    assert_eq!(error.code, Code::InternalError);
    assert!(server.requests().is_empty());
    assert!(!home.path().join("trader/intents.sqlite3").exists());
}

#[tokio::test]
async fn position_update_checks_owners_with_display_quote() {
    for (keeper_owner, contract_owner, message, requests) in [
        (
            OTHER_ACCOUNT,
            ACCOUNT,
            "Keeper position belongs to a different account",
            &["position"][..],
        ),
        (
            ACCOUNT,
            OTHER_ACCOUNT,
            "Contract position belongs to a different account",
            &["position", "route", "node_info", "contract_position"][..],
        ),
    ] {
        let server = PositionServer::start(keeper_owner, contract_owner);
        let endpoint = format!("{}/query", server.url);
        let home = tempfile::tempdir().unwrap();
        let storage = fixture_storage(&home, &endpoint);
        let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
        let session = trading.load_session(None).unwrap();
        let mut preview = leverage_preview("7", "10.00").unwrap();
        preview.quote = Some(json!({"display": "10× leverage"}));
        let digest = preview_digest(&preview, Some(ACCOUNT));

        let result = trading
            .submit(
                &session,
                &preview,
                SubmitOptions {
                    confirmation: Some(Confirmation::new(&digest, None).unwrap()),
                    ..Default::default()
                },
            )
            .await;
        assert_eq!(server.requests(), requests);
        let error = result.err().expect("foreign position must be refused");
        assert_eq!(error.code, Code::InvalidRequest);
        assert_eq!(error.message, message);
        assert!(!home.path().join("trader/intents.sqlite3").exists());
    }
}

async fn confirmed_digest(
    trading: &Trading<'_>,
    session: &Session,
    preview: &TradePreview,
) -> String {
    let verified = trading
        .position_update_preview(preview.clone(), Some(session), true)
        .await
        .unwrap();
    assert_eq!(
        verified["positionRoute"],
        json!({"chainId": NETWORK, "contractAddress": CONTRACT})
    );
    verified["previewDigest"].as_str().unwrap().to_owned()
}

async fn assert_fresh_submit_route(
    chain: &'static str,
    contract: &'static str,
    preview: TradePreview,
) {
    for display_quote in [None, Some(json!({"display": "position update"}))] {
        let server = PositionServer::start(ACCOUNT, ACCOUNT);
        let endpoint = format!("{}/query", server.url);
        let home = tempfile::tempdir().unwrap();
        let storage = fixture_storage(&home, &endpoint);
        let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
        let session = trading.load_session(None).unwrap();
        let digest = confirmed_digest(&trading, &session, &preview).await;
        {
            let mut state = server.state.lock().unwrap();
            state.keeper_chain = chain;
            state.lcd_chain = chain;
            state.contract = contract;
            state.requests.clear();
        }
        let mut submitted = preview.clone();
        submitted.quote = display_quote;
        let error = trading
            .submit(
                &session,
                &submitted,
                SubmitOptions {
                    confirmation: Some(Confirmation::new(&digest, None).unwrap()),
                    ..Default::default()
                },
            )
            .await
            .err()
            .expect("route switch must require a new confirmation");
        assert_eq!(
            server.requests(),
            ["position", "route", "node_info", "contract_position"]
        );
        assert_eq!(error.code, Code::PreviewMismatch);
        let replacement = &error.details.unwrap()["preview"];
        assert_eq!(
            replacement["positionRoute"],
            json!({"chainId": chain, "contractAddress": contract})
        );
        assert_ne!(replacement["previewDigest"], digest);
        assert!(!home.path().join("trader/intents.sqlite3").exists());
    }
}

#[tokio::test]
async fn confirmed_position_submit_refuses_chain_switch() {
    assert_fresh_submit_route(
        "fixture-chain-2",
        CONTRACT,
        leverage_preview("7", "10.00").unwrap(),
    )
    .await;
}

#[tokio::test]
async fn confirmed_position_submit_refuses_contract_switch() {
    assert_fresh_submit_route(
        NETWORK,
        "nibi1differentfixturecontract",
        tpsl_preview("7", Some("110"), None, false, true).unwrap(),
    )
    .await;
}

#[tokio::test]
async fn confirmed_position_submit_reports_lcd_only_chain_drift() {
    let server = PositionServer::start(ACCOUNT, ACCOUNT);
    let endpoint = format!("{}/query", server.url);
    let home = tempfile::tempdir().unwrap();
    let storage = fixture_storage(&home, &endpoint);
    let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
    let session = trading.load_session(None).unwrap();
    let preview = leverage_preview("7", "10.00").unwrap();
    let digest = confirmed_digest(&trading, &session, &preview).await;
    server.state.lock().unwrap().lcd_chain = "fixture-chain-2";

    for (confirmation, expected) in [
        (
            Some(Confirmation::new(&digest, None).unwrap()),
            Code::PreviewMismatch,
        ),
        (None, Code::InvalidRequest),
    ] {
        server.state.lock().unwrap().requests.clear();
        let error = trading
            .submit(
                &session,
                &preview,
                SubmitOptions {
                    confirmation,
                    ..Default::default()
                },
            )
            .await
            .err()
            .expect("LCD chain mismatch must be refused");
        assert_eq!(error.code, expected);
        if expected == Code::PreviewMismatch {
            assert_eq!(
                error.message,
                "Keeper trading route and LCD network do not match. Use an LCD on Keeper's chain and request a new preview."
            );
        }
        assert_eq!(server.requests(), ["position", "route", "node_info"]);
        assert!(!home.path().join("trader/intents.sqlite3").exists());
    }
}
