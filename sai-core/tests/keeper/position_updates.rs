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
    trade::{Confirmation, SubmitOptions, Trading, leverage_preview, preview_digest},
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
                        "enabled": true, "chainId": NETWORK, "contractAddress": CONTRACT,
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
            return json!({"default_node_info": {"network": NETWORK}});
        }
        assert!(request.starts_with(&format!("GET /cosmwasm/wasm/v1/contract/{CONTRACT}/smart/")));
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
