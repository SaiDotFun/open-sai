use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    thread,
    time::Duration,
};

use sai_core::{
    errors::Code,
    keeper::{INTENTS, Keeper, MARKETS, SUBMIT},
    log::{EventContext, Surface},
    storage::{Session, Storage},
    trade::{Confirmation, OpenTrade, OrderType, Side, SubmitOptions, Trading, preview_digest},
};
use serde_json::{Map, Value, json};

const EVENTS: EventContext<'_> = EventContext::new(None, Surface::Cli, None);

fn listener() -> TcpListener {
    TcpListener::bind(("127.0.0.1", 0)).unwrap()
}

fn read_request(stream: &mut TcpStream) -> (Vec<String>, Vec<u8>) {
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let mut reader = BufReader::new(stream);
    let mut lines = Vec::new();
    loop {
        let mut line = String::new();
        assert_ne!(reader.read_line(&mut line).unwrap(), 0);
        if line == "\r\n" {
            break;
        }
        lines.push(line.trim_end().into());
    }
    let length = lines
        .iter()
        .find_map(|line: &String| {
            line.to_ascii_lowercase()
                .strip_prefix("content-length: ")?
                .parse()
                .ok()
        })
        .unwrap();
    let mut body = vec![0; length];
    reader.read_exact(&mut body).unwrap();
    (lines, body)
}

#[tokio::test]
async fn shared_market_submit_refreshes_before_reserving() {
    let listener = listener();
    let endpoint = format!("http://{}/query", listener.local_addr().unwrap());
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let (_, bytes) = read_request(&mut stream);
        let req: Value = serde_json::from_slice(&bytes).unwrap();
        let response = json!({"data": {"perp": {"borrowings": [
            {"marketId": 3, "price": "102", "isOpen": true, "collateralToken": {"symbol": "USDC"}}
        ]}}})
        .to_string();
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response}",
            response.len()
        )
        .unwrap();
        req["query"].clone()
    });
    let home = tempfile::tempdir().unwrap();
    let storage = Storage::new(home.path());
    let addr = "0x1111111111111111111111111111111111111111";
    let session = Session::new(
        "fixture-token".to_owned().into(),
        addr.into(),
        "2099-01-01T00:00:00Z".into(),
        endpoint.clone(),
    );
    storage.save_session(session).unwrap();
    let trading = Trading::new(&endpoint, &storage, EVENTS);
    let session = trading.load_session(None).unwrap();
    let mut preview = OpenTrade {
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
    .unwrap();
    preview.reference_price = Some("100".into());
    let digest = preview_digest(&preview, Some(addr));
    let result = trading
        .submit(
            &session,
            &preview,
            SubmitOptions {
                confirmation: Some(Confirmation::new(&digest, Some("100")).unwrap()),
                ..Default::default()
            },
        )
        .await;
    assert_eq!(server.join().unwrap(), MARKETS);
    assert_eq!(result.err().unwrap().code, Code::PriceMoved);
    assert!(!home.path().join("trader/intents.sqlite3").exists());
}

#[tokio::test]
async fn tls_connect_timeout_is_not_sent() {
    let listener = listener();
    let endpoint = format!("https://{}/query", listener.local_addr().unwrap());
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut hello = [0];
        stream.read_exact(&mut hello).unwrap();
        assert_eq!(hello[0], 0x16, "TLS handshake record");
        thread::sleep(Duration::from_millis(400));
    });
    let error = Keeper::new(&endpoint, EVENTS)
        .request(INTENTS, Map::new(), None, Duration::from_millis(150))
        .await
        .unwrap_err();
    assert_eq!(error.code, Code::NotSent);
    server.join().unwrap();
}

#[tokio::test]
async fn timeout_after_sending_is_a_keeper_error() {
    let listener = listener();
    let endpoint = format!("http://{}/query", listener.local_addr().unwrap());
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        read_request(&mut stream);
        thread::sleep(Duration::from_millis(400));
    });
    let error = Keeper::new(&endpoint, EVENTS)
        .request(INTENTS, Map::new(), None, Duration::from_millis(150))
        .await
        .unwrap_err();
    assert_eq!(error.code, Code::KeeperError);
    server.join().unwrap();
}

#[tokio::test]
async fn stalled_request_write_is_a_keeper_error() {
    let listener = listener();
    let endpoint = format!("http://{}/query", listener.local_addr().unwrap());
    let (release, wait) = std::sync::mpsc::channel();
    let server = thread::spawn(move || {
        let (_stream, _) = listener.accept().unwrap();
        // Leave the socket open without draining the send buffer.
        wait.recv_timeout(Duration::from_secs(5)).unwrap();
    });
    let variables =
        Map::from_iter([("payload".into(), Value::String("x".repeat(8 * 1024 * 1024)))]);
    let error = Keeper::new(&endpoint, EVENTS)
        .request(INTENTS, variables, None, Duration::from_millis(200))
        .await
        .unwrap_err();
    release.send(()).unwrap();
    assert_eq!(error.code, Code::KeeperError);
    server.join().unwrap();
}

#[tokio::test]
async fn body_progress_resets_the_read_timeout() {
    let listener = listener();
    let endpoint = format!("http://{}/query", listener.local_addr().unwrap());
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        read_request(&mut stream);
        let body = b"{\"data\": {\"count\": 12345}}";
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .unwrap();
        for chunk in body.chunks(4) {
            thread::sleep(Duration::from_millis(80));
            stream.write_all(chunk).unwrap();
        }
    });
    let data = Keeper::new(&endpoint, EVENTS)
        .request(INTENTS, Map::new(), None, Duration::from_millis(350))
        .await
        .unwrap();
    assert_eq!(data["count"], 12345);
    server.join().unwrap();
}

#[tokio::test]
async fn admission_requires_explicit_rejection_and_no_data() {
    for (body, expected) in [
        (
            r#"{"errors":[{"extensions":{"code":"INTENT_NOT_ACCEPTED"}}]}"#,
            Code::AdmissionRejected,
        ),
        (
            r#"{"errors":[{"extensions":{"code":"INTENT_NOT_ACCEPTED"}},{}]}"#,
            Code::KeeperError,
        ),
        (
            r#"{"errors":[{"extensions":{"code":"INTENT_NOT_ACCEPTED"}}],"data":{"recordUserIntent":null}}"#,
            Code::KeeperError,
        ),
    ] {
        let listener = listener();
        let endpoint = format!("http://{}/query", listener.local_addr().unwrap());
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let (headers, bytes) = read_request(&mut stream);
            assert!(headers[0] == "POST /query HTTP/1.1");
            assert!(
                headers
                    .iter()
                    .any(|line| line.eq_ignore_ascii_case("authorization: Bearer fixture-token"))
            );
            let request: Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(request["query"], SUBMIT);
            assert_eq!(request["variables"], serde_json::json!({}));
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
        });
        let error = Keeper::new(&endpoint, EVENTS)
            .request(
                SUBMIT,
                Map::new(),
                Some("fixture-token"),
                Duration::from_secs(2),
            )
            .await
            .unwrap_err();
        assert_eq!(error.code, expected);
        server.join().unwrap();
    }
}
