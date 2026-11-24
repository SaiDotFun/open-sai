use std::{collections::HashSet, future::ready, io};

use rmcp::{
    ErrorData, RoleServer,
    model::{
        ClientJsonRpcMessage, ClientNotification, ErrorCode, JsonRpcMessage, RequestId,
        ServerJsonRpcMessage,
    },
    transport::Transport,
};
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
            let frame = serde_json::from_slice(&self.line);
            self.line.clear();
            let Ok(frame) = frame else { continue };
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
