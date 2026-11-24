use std::{collections::HashSet, future::ready, io};

use rmcp::{
    ErrorData, RoleServer,
    model::{
        ClientJsonRpcMessage, ClientNotification, ClientRequest, ErrorCode, JsonRpcMessage,
        RequestId, ServerJsonRpcMessage,
    },
    transport::Transport,
};
use serde_json::{Value, json};
use tokio::{
    io::{self as tokio_io, AsyncBufReadExt, AsyncWriteExt, BufReader, Stdin, Stdout},
    sync::mpsc,
    task::JoinHandle,
};

pub(super) struct Stdio {
    input: BufReader<Stdin>,
    line: Vec<u8>,
    output: Option<mpsc::UnboundedSender<ServerJsonRpcMessage>>,
    writer: Option<JoinHandle<io::Result<()>>>,
    pending: HashSet<RequestId>,
}

impl Stdio {
    pub(super) fn new() -> Self {
        let (output, frames) = mpsc::unbounded_channel();
        let writer = tokio::spawn(write_frames(tokio_io::stdout(), frames));
        Self {
            input: BufReader::new(tokio_io::stdin()),
            line: Vec::new(),
            output: Some(output),
            writer: Some(writer),
            pending: HashSet::new(),
        }
    }

    fn enqueue(&self, frame: ServerJsonRpcMessage) -> io::Result<()> {
        self.output
            .as_ref()
            .ok_or(io::ErrorKind::BrokenPipe)?
            .send(frame)
            .map_err(|_| io::ErrorKind::BrokenPipe.into())
    }
    fn reject_request(&self, id: RequestId) -> io::Result<()> {
        let error = ErrorData::invalid_params("Invalid request parameters", Some(json!("")));
        self.enqueue(ServerJsonRpcMessage::error(error, Some(id)))
    }

    fn report_invalid_frame(&self) -> io::Result<()> {
        match serde_json::from_slice::<Value>(&self.line) {
            Ok(value) => {
                if value["jsonrpc"] == "2.0"
                    && value["method"].is_string()
                    && (value["params"].is_null() || value["params"].is_object())
                    && let Ok(id) = serde_json::from_value(value["id"].clone())
                {
                    return self.reject_request(id);
                }
                eprintln!("Invalid MCP JSON-RPC envelope");
                self.enqueue(parse_diagnostic())
            }
            Err(error) => {
                eprintln!(
                    "Invalid MCP JSON at line {}, column {}",
                    error.line(),
                    error.column()
                );
                self.enqueue(parse_diagnostic())
            }
        }
    }
}

// The pinned Python server uses the legacy MCP logging notification on parse errors.
#[allow(deprecated)]
fn parse_diagnostic() -> ServerJsonRpcMessage {
    use rmcp::model::{LoggingLevel, LoggingMessageNotification, LoggingMessageNotificationParam};

    let params =
        LoggingMessageNotificationParam::new(LoggingLevel::Error, json!("Internal Server Error"))
            .with_logger("mcp.server.exception_handler");
    ServerJsonRpcMessage::notification(LoggingMessageNotification::new(params).into())
}

pub(super) fn cancelled() -> ErrorData {
    ErrorData::new(ErrorCode(0), "Request cancelled", None)
}

impl Transport<RoleServer> for Stdio {
    type Error = io::Error;

    fn send(
        &mut self,
        frame: ServerJsonRpcMessage,
    ) -> impl Future<Output = io::Result<()>> + Send + 'static {
        let id = match &frame {
            JsonRpcMessage::Response(response) => Some(&response.id),
            JsonRpcMessage::Error(error) => error.id.as_ref(),
            _ => None,
        };
        if let Some(id) = id {
            self.pending.remove(id);
        }
        ready(self.enqueue(frame))
    }

    async fn receive(&mut self) -> Option<ClientJsonRpcMessage> {
        loop {
            // Keep the partial frame across rmcp's select loops.
            if self.input.read_until(b'\n', &mut self.line).await.ok()? == 0 && self.line.is_empty()
            {
                return None;
            }
            let frame = serde_json::from_slice::<ClientJsonRpcMessage>(&self.line);
            let frame = match frame {
                Ok(JsonRpcMessage::Request(request))
                    if matches!(request.request, ClientRequest::CustomRequest(_)) =>
                {
                    self.reject_request(request.id).ok()?;
                    None
                }
                Ok(frame) => Some(frame),
                Err(_) => {
                    self.report_invalid_frame().ok()?;
                    None
                }
            };
            self.line.clear();
            let Some(frame) = frame else { continue };
            match &frame {
                JsonRpcMessage::Request(request) => {
                    self.pending.insert(request.id.clone());
                }
                JsonRpcMessage::Notification(notification) => {
                    if let ClientNotification::CancelledNotification(cancel) =
                        &notification.notification
                        && let Some(id) = &cancel.params.request_id
                        && self.pending.remove(id)
                    {
                        // rmcp cancels the handler's token and drops its reply.
                        // Python also sends a cancellation error on the original ID.
                        self.enqueue(ServerJsonRpcMessage::error(cancelled(), Some(id.clone())))
                            .ok()?;
                    }
                }
                _ => {}
            }
            return Some(frame);
        }
    }

    async fn close(&mut self) -> io::Result<()> {
        self.output.take();
        match self.writer.take() {
            Some(writer) => writer.await.map_err(io::Error::other)?,
            None => Ok(()),
        }
    }
}

async fn write_frames(
    mut output: Stdout,
    mut frames: mpsc::UnboundedReceiver<ServerJsonRpcMessage>,
) -> io::Result<()> {
    while let Some(frame) = frames.recv().await {
        let mut bytes = serde_json::to_vec(&frame)?;
        bytes.push(b'\n');
        output.write_all(&bytes).await?;
        output.flush().await?;
    }
    Ok(())
}
