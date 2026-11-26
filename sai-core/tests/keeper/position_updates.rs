use std::{
    io::{ErrorKind, Write},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::Duration,
};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use sai_core::{
    errors::Code,
    keeper::{MARKET, MARKETS, POSITION, ROUTE, SUBMIT},
    storage::{Session, Storage},
    trade::{
        Confirmation, OpenTrade, OrderType, PreviewMode, Side, SubmitOptions, TradePreview,
        Trading, close_preview, leverage_preview, preview_digest, tpsl_preview,
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
    contract_available: bool,
    requests: Vec<String>,
    price: &'static str,
    market_available: bool,
    trade_type: &'static str,
    indexed_position_missing: bool,
    close_height_check_index: Option<u8>,
    target_missing: bool,
    market_close_only: bool,
    stale_oracle: bool,
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
            contract_available: true,
            requests: Vec::new(),
            price: "100",
            market_available: true,
            trade_type: "trade",
            indexed_position_missing: false,
            close_height_check_index: None,
            target_missing: false,
            market_close_only: false,
            stale_oracle: false,
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
                let mut replies = replies.lock().unwrap();
                let response = replies.response(&headers[0], &bytes).to_string();
                let status = if !replies.contract_available
                    && replies
                        .requests
                        .last()
                        .is_some_and(|label| label == "contract_position")
                {
                    "503 Service Unavailable"
                } else {
                    "200 OK"
                };
                let height = if replies.stale_oracle {
                    "grpc-metadata-x-cosmos-block-height: 120\r\n"
                } else {
                    ""
                };
                drop(replies);
                write!(
                    stream,
                    "HTTP/1.1 {status}\r\n{height}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response}",
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
                    let position = if self.indexed_position_missing {
                        Value::Null
                    } else if self.close_height_check_index.is_some() {
                        json!({
                            "id": 7, "trader": self.keeper_owner, "isOpen": true, "tradeType": "trade",
                            "collateralAmount": 2_000_000, "leverage": "2",
                            "perpBorrowing": {"marketId": 3, "collateralToken": {"id": 0, "symbol": "USDC"}}
                        })
                    } else {
                        json!({"id": 7, "trader": self.keeper_owner})
                    };
                    json!({"perp": {"trade": position}})
                }
                MARKET => {
                    assert_eq!(request["variables"], json!({"market": 3, "collateral": 0}));
                    self.requests.push("borrowing".into());
                    json!({"perp": {"borrowing": {
                        "marketId": 3, "isOpen": true, "collateralToken": {"id": 0}
                    }}})
                }
                MARKETS => {
                    self.requests.push("markets".into());
                    let borrowings = if self.market_available {
                        json!([{"marketId": 3, "price": self.price, "isOpen": true, "collateralToken": {"symbol": "USDC"}}])
                    } else {
                        json!([])
                    };
                    json!({"perp": {"borrowings": borrowings}})
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
        if request.starts_with("GET /cosmos/base/tendermint/v1beta1/blocks/120 ") {
            self.requests.push("block".into());
            return json!({"block": {"header": {"height": "120", "time": "1970-01-01T00:16:40Z"}}});
        }
        let path = request
            .strip_prefix(&format!(
                "GET /cosmwasm/wasm/v1/contract/{}/smart/",
                self.contract
            ))
            .unwrap_or_else(|| panic!("Unexpected LCD request: {request}"));
        let (target, _) = path.split_once(' ').unwrap();
        let path = target.split('?').next().unwrap();
        let (encoded, _) = url::form_urlencoded::parse(path.as_bytes()).next().unwrap();
        let message: Value =
            serde_json::from_slice(&STANDARD.decode(encoded.as_bytes()).unwrap()).unwrap();
        if message == json!({"get_market": {"index": "MarketIndex(3)"}}) {
            self.requests.push("market_state".into());
            return json!({"data": {
                "market_trading_state": if self.market_close_only { "close_only" } else { "open" },
                "group_index": "GroupIndex(0)",
            }});
        }
        if self.stale_oracle {
            let (name, value) = match message.as_object().unwrap().keys().next().unwrap().as_str() {
                "get_perp_prices" => (
                    "prices",
                    json!({"market": {
                        "base_per_quote": "100", "base_last_update_time": 1
                    }}),
                ),
                "market_order_config" => (
                    "market_order_config",
                    json!({"fill_at_next_oracle_price": false}),
                ),
                "get_group" => ("group", json!({"stale_price_expiration_secs": 30})),
                _ => ("", Value::Null),
            };
            if !name.is_empty() {
                self.requests.push(name.into());
                return json!({"data": value});
            }
        }
        if let Some(latest_index) = self.close_height_check_index {
            if message.get("get_exchange_balance").is_some() {
                assert_eq!(
                    message,
                    json!({"get_exchange_balance": {"user": ACCOUNT, "collateral_index": "TokenIndex(0)"}})
                );
                self.requests.push("exchange_balance".into());
                return json!({"data": {"collateral_index": "TokenIndex(0)"}});
            }
            assert_eq!(
                message,
                json!({"get_trade_raw": {"trader": ACCOUNT, "index": 7}})
            );
            let index = if self
                .requests
                .iter()
                .any(|label| label == "exchange_balance")
            {
                self.requests.push("height_check".into());
                latest_index
            } else {
                self.requests.push("contract_position".into());
                7
            };
            return json!({"data": {
                "user": self.contract_owner, "user_trade_index": format!("UserTradeIndex({index})"),
                "market_index": "MarketIndex(3)", "collateral_index": "TokenIndex(0)",
                "is_open": true, "trade_type": "trade", "collateral_amount": "2000000",
                "leverage": "2", "long": true, "open_price": "100"
            }});
        }
        self.requests.push("contract_position".into());
        if self.target_missing {
            return json!({"data": null});
        }
        json!({"data": {
            "user_trade_index": "UserTradeIndex(7)", "user": self.contract_owner,
            "trade_type": self.trade_type, "is_open": true,
        }})
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
async fn close_preview_requires_verified_ownership_to_issue_a_digest() {
    for (chain, available, verified) in [
        (NETWORK, false, false),
        ("other-chain", true, false),
        (NETWORK, true, true),
    ] {
        let server = PositionServer::start(ACCOUNT, ACCOUNT);
        {
            let mut state = server.state.lock().unwrap();
            state.lcd_chain = chain;
            state.contract_available = available;
        }
        let endpoint = format!("{}/query", server.url);
        let home = tempfile::tempdir().unwrap();
        let storage = fixture_storage(&home, &endpoint);
        let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
        let session = trading.load_session(None).unwrap();
        let preview = trading
            .prepare_preview(
                close_preview("7", false).unwrap(),
                Some(&session),
                PreviewMode::Mcp,
            )
            .await
            .unwrap();

        let output = preview.confirmed_output(Some(session.addr()));
        // The fixture has no borrowing data. Ownership can still be confirmed.
        assert_eq!(output["estimates"]["fees"]["status"], "unavailable");
        if verified {
            assert!(output.get("positionOwnerVerified").is_none());
            assert_eq!(
                output["previewDigest"],
                preview_digest(&preview, Some(session.addr()))
            );
        } else {
            assert!(output.get("previewDigest").is_none());
            assert_eq!(output["positionOwnerVerified"], false);
        }
        let mut requests = vec!["position", "route", "node_info"];
        if chain == NETWORK {
            requests.push("contract_position");
        }
        assert_eq!(server.requests(), requests);
        assert!(!home.path().join("trader/intents.sqlite3").exists());
    }
}

#[tokio::test]
async fn close_preview_withholds_digest_when_the_height_check_returns_a_different_index() {
    for (latest_index, verified) in [(8, false), (7, true)] {
        let server = PositionServer::start(ACCOUNT, ACCOUNT);
        // Neither response serves a block height. Only a conflicting index
        // invalidates the earlier ownership check.
        server.state.lock().unwrap().close_height_check_index = Some(latest_index);
        let endpoint = format!("{}/query", server.url);
        let home = tempfile::tempdir().unwrap();
        let storage = fixture_storage(&home, &endpoint);
        let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
        let session = trading.load_session(None).unwrap();

        let output = trading
            .confirmed_trade_preview(&session, close_preview("7", false).unwrap())
            .await
            .unwrap();

        assert_eq!(
            server.requests(),
            [
                "position",
                "route",
                "node_info",
                "contract_position",
                "borrowing",
                "exchange_balance",
                "height_check"
            ]
        );
        assert_eq!(output["estimates"]["fees"]["status"], "unavailable");
        if verified {
            assert!(output["previewDigest"].is_string());
            assert_eq!(output["positionRoute"]["contractAddress"], CONTRACT);
            assert!(output.get("positionOwnerVerified").is_none());
        } else {
            assert!(output.get("previewDigest").is_none());
            assert!(output.get("positionRoute").is_none());
            assert_eq!(output["positionOwnerVerified"], false);
        }
        assert!(!home.path().join("trader/intents.sqlite3").exists());
    }
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

fn market_open() -> TradePreview {
    OpenTrade {
        market: "3",
        side: Side::Long,
        order_type: OrderType::Market,
        amount: "20",
        leverage: "2",
        price: None,
        slippage: "1",
        take_profit: None,
        stop_loss: None,
    }
    .preview()
    .unwrap()
}

#[tokio::test]
async fn shared_market_submit_refreshes_before_reserving() {
    let server = PositionServer::start(ACCOUNT, ACCOUNT);
    let endpoint = format!("{}/query", server.url);
    let home = tempfile::tempdir().unwrap();
    let storage = fixture_storage(&home, &endpoint);
    let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
    let session = trading.load_session(None).unwrap();
    let preview = market_open();
    let issued = trading
        .confirmed_trade_preview(&session, preview.clone())
        .await
        .unwrap();
    assert_eq!(issued["referencePrice"], "100");
    assert_eq!(issued["estimates"]["worstCaseFill"]["price"], "102.01");
    server.state.lock().unwrap().price = "102";
    let error = trading
        .submit(
            &session,
            &preview,
            SubmitOptions {
                confirmation: Some(
                    Confirmation::new(issued["previewDigest"].as_str().unwrap(), Some("100"))
                        .unwrap(),
                ),
                ..Default::default()
            },
        )
        .await
        .err()
        .unwrap();
    assert_eq!(error.code, Code::PriceMoved);
    let details = error.details.unwrap();
    let replacement = &details["preview"];
    assert_eq!(replacement["referencePrice"], "102");
    let fill = &replacement["estimates"]["worstCaseFill"];
    assert_eq!(fill["status"], "bound");
    assert_eq!(fill["price"], "104.0502");
    assert_eq!(fill["reference"]["price"], "102");
    assert_ne!(replacement["previewDigest"], issued["previewDigest"]);
    assert_eq!(replacement["positionRoute"], issued["positionRoute"]);
    assert_eq!(
        server.requests(),
        [
            "route",
            "node_info",
            "markets",
            "market_state",
            "route",
            "node_info",
            "markets",
            "market_state",
            "route",
            "node_info"
        ]
    );
    assert!(!home.path().join("trader/intents.sqlite3").exists());
}

#[tokio::test]
async fn confirmed_core_open_checks_market_state_before_reserving() {
    let server = PositionServer::start(ACCOUNT, ACCOUNT);
    let endpoint = format!("{}/query", server.url);
    let home = tempfile::tempdir().unwrap();
    let storage = fixture_storage(&home, &endpoint);
    let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
    let session = trading.load_session(None).unwrap();
    let preview = market_open();
    let issued = trading
        .confirmed_trade_preview(&session, preview.clone())
        .await
        .unwrap();
    server.state.lock().unwrap().market_close_only = true;

    let error = trading
        .submit(
            &session,
            &preview,
            SubmitOptions {
                confirmation: Some(
                    Confirmation::new(issued["previewDigest"].as_str().unwrap(), Some("100"))
                        .unwrap(),
                ),
                ..Default::default()
            },
        )
        .await
        .err()
        .unwrap();
    assert_eq!(error.code, Code::PreviewMismatch);
    assert_eq!(
        error.details.unwrap()["preview"]["estimates"]["execution"]["code"],
        "MarketClosed"
    );
    assert!(!server.requests().iter().any(|label| label == "admission"));
    assert!(!home.path().join("trader/intents.sqlite3").exists());
}

#[tokio::test]
async fn confirmed_core_close_checks_oracle_age_before_reserving() {
    let server = PositionServer::start(ACCOUNT, ACCOUNT);
    let endpoint = format!("{}/query", server.url);
    let home = tempfile::tempdir().unwrap();
    let storage = fixture_storage(&home, &endpoint);
    let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
    let session = trading.load_session(None).unwrap();
    let preview = close_preview("7", false).unwrap();
    let issued = trading
        .confirmed_trade_preview(&session, preview.clone())
        .await
        .unwrap();
    {
        let mut state = server.state.lock().unwrap();
        state.close_height_check_index = Some(7);
        state.stale_oracle = true;
    }

    let error = trading
        .submit(
            &session,
            &preview,
            SubmitOptions {
                confirmation: Some(
                    Confirmation::new(issued["previewDigest"].as_str().unwrap(), None).unwrap(),
                ),
                ..Default::default()
            },
        )
        .await
        .err()
        .unwrap();
    assert_eq!(error.code, Code::PreviewMismatch);
    assert_eq!(
        error.details.unwrap()["preview"]["estimates"]["execution"]["code"],
        "OraclePriceStale"
    );
    assert!(!server.requests().iter().any(|label| label == "admission"));
    assert!(!home.path().join("trader/intents.sqlite3").exists());
}

#[tokio::test]
async fn confirmed_core_leverage_checks_execution_before_reserving() {
    let server = PositionServer::start(ACCOUNT, ACCOUNT);
    let endpoint = format!("{}/query", server.url);
    let home = tempfile::tempdir().unwrap();
    let storage = fixture_storage(&home, &endpoint);
    let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
    let session = trading.load_session(None).unwrap();
    let preview = leverage_preview("7", "10").unwrap();
    let digest = confirmed_digest(&trading, &session, &preview).await;
    {
        let mut state = server.state.lock().unwrap();
        state.close_height_check_index = Some(7);
        state.stale_oracle = true;
    }

    let error = trading
        .submit(
            &session,
            &preview,
            SubmitOptions {
                confirmation: Some(Confirmation::new(&digest, None).unwrap()),
                ..Default::default()
            },
        )
        .await
        .err()
        .unwrap();
    assert_eq!(error.code, Code::PreviewMismatch);
    assert_eq!(
        error.details.unwrap()["preview"]["estimates"]["execution"]["code"],
        "OraclePriceStale"
    );
    assert!(!server.requests().iter().any(|label| label == "admission"));
    assert!(!home.path().join("trader/intents.sqlite3").exists());
}

#[tokio::test]
async fn basic_trade_confirmation_checks_live_chain_and_contract() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../tests/parity/route_trade_digest.json"
    ))
    .unwrap();
    for (index, preview) in [
        market_open(),
        close_preview("7", false).unwrap(),
        close_preview("7", true).unwrap(),
    ]
    .into_iter()
    .enumerate()
    {
        let server = PositionServer::start(ACCOUNT, ACCOUNT);
        if index == 2 {
            server.state.lock().unwrap().trade_type = "limit";
        }
        let endpoint = format!("{}/query", server.url);
        let home = tempfile::tempdir().unwrap();
        let storage = fixture_storage(&home, &endpoint);
        let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
        let session = trading.load_session(None).unwrap();
        let issued = trading
            .confirmed_trade_preview(&session, preview.clone())
            .await
            .unwrap();
        let case = &fixture["cases"][index];
        let digest = issued["previewDigest"].as_str().unwrap();
        assert_eq!(digest, case["original"]);
        assert_eq!(
            issued["positionRoute"],
            json!({"chainId": NETWORK, "contractAddress": CONTRACT})
        );
        for (change, chain, contract) in [
            ("chain", "fixture-chain-2", CONTRACT),
            ("contract", NETWORK, "nibi1differentcontract"),
        ] {
            {
                let mut state = server.state.lock().unwrap();
                state.keeper_chain = chain;
                state.lcd_chain = chain;
                state.contract = contract;
            }
            let error = trading
                .submit(
                    &session,
                    &preview,
                    SubmitOptions {
                        confirmation: Some(
                            Confirmation::new(digest, (index == 0).then_some("100")).unwrap(),
                        ),
                        ..Default::default()
                    },
                )
                .await
                .err()
                .unwrap();
            assert_eq!(error.code, Code::PreviewMismatch);
            assert!(error.details.is_none());
            let refreshed = trading
                .confirmed_trade_preview(&session, preview.clone())
                .await
                .unwrap();
            assert_eq!(
                refreshed["positionRoute"],
                json!({"chainId": chain, "contractAddress": contract})
            );
            assert_eq!(refreshed["previewDigest"], case[change]);
            assert!(!home.path().join("trader/intents.sqlite3").exists());
            assert!(
                !server
                    .requests()
                    .iter()
                    .any(|request| request == "admission")
            );
        }
        let accepted = trading
            .confirmed_trade_preview(&session, preview.clone())
            .await
            .unwrap();
        assert!(
            trading
                .submit(
                    &session,
                    &preview,
                    SubmitOptions {
                        confirmation: Some(
                            Confirmation::new(
                                accepted["previewDigest"].as_str().unwrap(),
                                (index == 0).then_some("100")
                            )
                            .unwrap()
                        ),
                        ..Default::default()
                    }
                )
                .await
                .is_ok()
        );
        assert_eq!(server.requests().last().unwrap(), "admission");
    }
}

#[tokio::test]
async fn basic_confirmation_reports_keeper_only_chain_drift() {
    for (preview, reference, preflight) in [
        (market_open(), Some("100"), &["route", "node_info"][..]),
        (close_preview("7", false).unwrap(), None, &["route"][..]),
        (
            close_preview("7", true).unwrap(),
            None,
            &["route", "node_info"][..],
        ),
    ] {
        let server = PositionServer::start(ACCOUNT, ACCOUNT);
        server.state.lock().unwrap().trade_type = "limit";
        let endpoint = format!("{}/query", server.url);
        let home = tempfile::tempdir().unwrap();
        let storage = fixture_storage(&home, &endpoint);
        let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
        let session = trading.load_session(None).unwrap();
        let issued = trading
            .confirmed_trade_preview(&session, preview.clone())
            .await
            .unwrap();
        let digest = issued["previewDigest"].as_str().unwrap();
        {
            let mut state = server.state.lock().unwrap();
            state.keeper_chain = "fixture-chain-2";
            state.requests.clear();
        }
        let error = trading
            .submit(
                &session,
                &preview,
                SubmitOptions {
                    confirmation: Some(Confirmation::new(digest, reference).unwrap()),
                    ..Default::default()
                },
            )
            .await
            .err()
            .unwrap();
        assert_eq!(error.code, Code::PreviewMismatch);
        assert_eq!(server.requests(), preflight);
        assert!(!home.path().join("trader/intents.sqlite3").exists());
    }
}

#[tokio::test]
async fn market_confirmation_reports_contract_drift_before_reading_missing_market() {
    let server = PositionServer::start(ACCOUNT, ACCOUNT);
    let endpoint = format!("{}/query", server.url);
    let home = tempfile::tempdir().unwrap();
    let storage = fixture_storage(&home, &endpoint);
    let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
    let session = trading.load_session(None).unwrap();
    let preview = market_open();
    let issued = trading
        .confirmed_trade_preview(&session, preview.clone())
        .await
        .unwrap();
    let digest = issued["previewDigest"].as_str().unwrap();
    {
        let mut state = server.state.lock().unwrap();
        state.contract = "nibi1differentcontract";
        state.market_available = false;
        state.requests.clear();
    }

    let error = trading
        .submit(
            &session,
            &preview,
            SubmitOptions {
                confirmation: Some(Confirmation::new(digest, Some("100")).unwrap()),
                ..Default::default()
            },
        )
        .await
        .err()
        .unwrap();

    assert_eq!(error.code, Code::PreviewMismatch);
    assert!(error.details.is_none());
    assert!(error.message.contains("Request a fresh preview"));
    assert_eq!(server.requests(), ["route", "node_info"]);
    assert!(!home.path().join("trader/intents.sqlite3").exists());
}

#[tokio::test]
async fn close_confirmation_checks_route_before_reading_the_indexed_position() {
    let server = PositionServer::start(ACCOUNT, ACCOUNT);
    let endpoint = format!("{}/query", server.url);
    let home = tempfile::tempdir().unwrap();
    let storage = fixture_storage(&home, &endpoint);
    let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
    let session = trading.load_session(None).unwrap();
    let preview = close_preview("7", false).unwrap();
    let issued = trading
        .confirmed_trade_preview(&session, preview.clone())
        .await
        .unwrap();
    let digest = issued["previewDigest"].as_str().unwrap();

    for (contract, missing, owner, code, message, requests) in [
        (
            "nibi1differentcontract",
            true,
            ACCOUNT,
            Code::PreviewMismatch,
            "Request a fresh preview",
            &["route"][..],
        ),
        (
            CONTRACT,
            true,
            ACCOUNT,
            Code::InvalidRequest,
            "Position not found for this account",
            &["route", "position"][..],
        ),
        (
            CONTRACT,
            false,
            OTHER_ACCOUNT,
            Code::InvalidRequest,
            "Keeper position belongs to a different account",
            &["route", "position"][..],
        ),
    ] {
        {
            let mut state = server.state.lock().unwrap();
            state.contract = contract;
            state.indexed_position_missing = missing;
            state.keeper_owner = owner;
            state.requests.clear();
        }

        let error = trading
            .submit(
                &session,
                &preview,
                SubmitOptions {
                    confirmation: Some(Confirmation::new(digest, None).unwrap()),
                    ..Default::default()
                },
            )
            .await
            .err()
            .unwrap();

        assert_eq!(error.code, code);
        assert!(error.message.contains(message));
        assert!(error.details.is_none());
        assert_eq!(server.requests(), requests);
        assert!(!home.path().join("trader/intents.sqlite3").exists());
    }
}

#[tokio::test]
async fn cancel_confirmation_checks_route_before_reading_replacement_contract() {
    let server = PositionServer::start(ACCOUNT, ACCOUNT);
    server.state.lock().unwrap().trade_type = "limit";
    let endpoint = format!("{}/query", server.url);
    let home = tempfile::tempdir().unwrap();
    let storage = fixture_storage(&home, &endpoint);
    let trading = Trading::new(&endpoint, &storage, EVENTS).with_lcd_endpoint(&server.url);
    let session = trading.load_session(None).unwrap();
    let preview = close_preview("7", true).unwrap();
    let issued = trading
        .confirmed_trade_preview(&session, preview.clone())
        .await
        .unwrap();
    let digest = issued["previewDigest"].as_str().unwrap();
    {
        let mut state = server.state.lock().unwrap();
        state.contract = "nibi1differentcontract";
        state.target_missing = true;
        state.requests.clear();
    }
    let error = trading
        .submit(
            &session,
            &preview,
            SubmitOptions {
                confirmation: Some(Confirmation::new(digest, None).unwrap()),
                ..Default::default()
            },
        )
        .await
        .err()
        .unwrap();
    assert_eq!(error.code, Code::PreviewMismatch);
    assert!(error.details.is_none());
    assert!(error.message.contains("Request a fresh preview"));
    assert_eq!(server.requests(), ["route", "node_info"]);
    assert!(!home.path().join("trader/intents.sqlite3").exists());
}
