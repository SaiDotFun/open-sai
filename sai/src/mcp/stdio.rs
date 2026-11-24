use std::{collections::HashMap, io, sync::Arc};

use rmcp::{
    ErrorData, RoleServer,
    model::{
        ClientJsonRpcMessage, ClientNotification, ClientRequest, ErrorCode, JsonRpcMessage,
        RequestId, ServerJsonRpcMessage,
    },
    transport::{
        StdioReader, StdioWriter, Transport, async_rw::DEFAULT_MAX_LINE_LENGTH, stdio_pipes,
    },
};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    sync::{OwnedSemaphorePermit, Semaphore, mpsc},
    task::JoinHandle,
};

// A slot covers an admitted request through the flush of its reply.
pub(super) const MAX_IN_FLIGHT: usize = 64;
type Outbound = (ServerJsonRpcMessage, Option<OwnedSemaphorePermit>);

pub(super) struct Stdio {
    input: BufReader<StdioReader>,
    line: Vec<u8>,
    discarding: bool,
    output: Option<mpsc::Sender<Outbound>>,
    writer: Option<JoinHandle<io::Result<()>>>,
    slots: Arc<Semaphore>,
    overflow: Arc<Semaphore>,
    pending: HashMap<RequestId, OwnedSemaphorePermit>,
}

impl Stdio {
    pub(super) fn new() -> Self {
        let (input, stdout) = stdio_pipes();
        let (output, frames) = mpsc::channel(MAX_IN_FLIGHT + 1);
        let writer = tokio::spawn(write_frames(stdout, frames));
        Self {
            input: BufReader::new(input),
            line: Vec::new(),
            discarding: false,
            output: Some(output),
            writer: Some(writer),
            slots: Arc::new(Semaphore::new(MAX_IN_FLIGHT)),
            overflow: Arc::new(Semaphore::new(1)),
            pending: HashMap::new(),
        }
    }

    async fn read_frame(&mut self) -> io::Result<Option<Frame>> {
        loop {
            // Store buffered bytes and discard state on the transport so a
            // cancelled select in rmcp cannot lose part of a frame.
            let input = self.input.fill_buf().await?;
            if input.is_empty() {
                return Ok((!self.line.is_empty()).then_some(Frame::Complete));
            }
            let newline = input.iter().position(|byte| *byte == b'\n');
            let length = newline.unwrap_or(input.len());
            let consumed = length + usize::from(newline.is_some());
            if self.discarding {
                self.discarding = newline.is_none();
                self.input.consume(consumed);
                continue;
            }
            if self.line.len() + length > DEFAULT_MAX_LINE_LENGTH {
                self.line = Vec::new();
                self.discarding = newline.is_none();
                self.input.consume(consumed);
                return Ok(Some(Frame::Oversized));
            }
            self.line.extend_from_slice(&input[..length]);
            self.input.consume(consumed);
            if newline.is_some() {
                return Ok(Some(Frame::Complete));
            }
        }
    }

    fn invalid_frame_reply(&self) -> ServerJsonRpcMessage {
        match serde_json::from_slice::<Value>(&self.line) {
            Ok(value) => {
                if value["jsonrpc"] == "2.0"
                    && value["method"].is_string()
                    && (value["params"].is_null() || value["params"].is_object())
                    && let Ok(id) = serde_json::from_value(value["id"].clone())
                {
                    return invalid_params(id);
                }
                eprintln!("Invalid MCP JSON-RPC envelope");
            }
            Err(error) => {
                eprintln!(
                    "Invalid MCP JSON at line {}, column {}",
                    error.line(),
                    error.column()
                );
            }
        }
        parse_diagnostic()
    }
}

enum Frame {
    Complete,
    Oversized,
}

fn invalid_params(id: RequestId) -> ServerJsonRpcMessage {
    let error = ErrorData::invalid_params("Invalid request parameters", Some(json!("")));
    ServerJsonRpcMessage::error(error, Some(id))
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
        let slot = id.and_then(|id| self.pending.remove(id));
        let output = self.output.clone();
        async move {
            output
                .ok_or(io::ErrorKind::BrokenPipe)?
                .send((frame, slot))
                .await
                .map_err(|_| io::ErrorKind::BrokenPipe.into())
        }
    }

    async fn receive(&mut self) -> Option<ClientJsonRpcMessage> {
        let output = self.output.as_ref()?.clone();
        loop {
            // Leave one slot to read a cancellation when all handlers are
            // busy. An excess request gets one bounded reply on that slot.
            let (mut slot, mut overloaded) = match Arc::clone(&self.slots).try_acquire_owned() {
                Ok(slot) => (slot, false),
                Err(_) => tokio::select! {
                    biased;
                    _ = output.closed() => return None,
                    slot = Arc::clone(&self.slots).acquire_owned() => (slot.ok()?, false),
                    slot = Arc::clone(&self.overflow).acquire_owned() => (slot.ok()?, true),
                },
            };
            let kind = tokio::select! {
                _ = output.closed() => return None,
                kind = self.read_frame() => kind.ok()??,
            };
            let frame = match kind {
                Frame::Oversized => {
                    eprintln!("MCP input frame exceeds {DEFAULT_MAX_LINE_LENGTH} bytes");
                    Err(parse_diagnostic())
                }
                Frame::Complete => serde_json::from_slice::<ClientJsonRpcMessage>(&self.line)
                    .map_err(|_| self.invalid_frame_reply()),
            };
            self.line.clear();
            if overloaded && let Ok(available) = Arc::clone(&self.slots).try_acquire_owned() {
                slot = available;
                overloaded = false;
            }
            match frame {
                Err(reply) => output.try_send((reply, Some(slot))).ok()?,
                Ok(JsonRpcMessage::Request(request))
                    if matches!(request.request, ClientRequest::CustomRequest(_))
                        || self.pending.contains_key(&request.id) =>
                {
                    output
                        .try_send((invalid_params(request.id), Some(slot)))
                        .ok()?;
                }
                Ok(JsonRpcMessage::Request(request)) if overloaded => {
                    let error =
                        ErrorData::new(ErrorCode(-32000), "Too many pending requests", None);
                    let reply = ServerJsonRpcMessage::error(error, Some(request.id));
                    output.try_send((reply, Some(slot))).ok()?;
                }
                Ok(frame) => {
                    match &frame {
                        JsonRpcMessage::Request(request) => {
                            self.pending.insert(request.id.clone(), slot);
                        }
                        JsonRpcMessage::Notification(notification) => {
                            if let ClientNotification::CancelledNotification(cancel) =
                                &notification.notification
                                && let Some(id) = &cancel.params.request_id
                                && let Some(slot) = self.pending.remove(id)
                            {
                                // rmcp stops the handler and drops its reply.
                                // Python also replies on the original ID.
                                let reply =
                                    ServerJsonRpcMessage::error(cancelled(), Some(id.clone()));
                                output.try_send((reply, Some(slot))).ok()?;
                            }
                        }
                        _ => {}
                    }
                    return Some(frame);
                }
            }
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
    mut output: StdioWriter,
    mut frames: mpsc::Receiver<Outbound>,
) -> io::Result<()> {
    while let Some((frame, _slot)) = frames.recv().await {
        let mut bytes = serde_json::to_vec(&frame)?;
        bytes.push(b'\n');
        output.write_all(&bytes).await?;
        output.flush().await?;
    }
    Ok(())
}
