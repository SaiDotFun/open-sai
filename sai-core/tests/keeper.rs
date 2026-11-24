use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    thread,
    time::Duration,
};

use sai_core::{
    errors::Code,
    keeper::{INTENTS, Keeper, SUBMIT},
    log::{EventContext, Surface},
};
use serde_json::{Map, Value};

#[path = "keeper/position_updates.rs"]
mod position_updates;

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
        .unwrap_or(0);
    let mut body = vec![0; length];
    reader.read_exact(&mut body).unwrap();
    (lines, body)
}

#[tokio::test]
async fn lcd_probe_appends_path_before_query() {
    let listener = listener();
    let endpoint = format!(
        "http://{}/prefix/?project=fixture",
        listener.local_addr().unwrap()
    );
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let (headers, _) = read_request(&mut stream);
        let body = r#"{"default_node_info":{"network":"fixture-chain"}}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
        headers[0].clone()
    });
    assert_eq!(
        sai_core::keeper::lcd_network(&endpoint, Duration::from_secs(1)).await,
        Ok(Some("fixture-chain".into()))
    );
    assert_eq!(
        server.join().unwrap(),
        "GET /prefix/cosmos/base/tendermint/v1beta1/node_info?project=fixture HTTP/1.1"
    );
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
