use std::{io, os::fd::AsFd, thread};

use tokio::sync::oneshot;

/// Keep the CLI responsive to SIGINT even when an inherited stdout pipe is full.
pub(super) async fn write_stdout(line: String) -> io::Result<()> {
    let (done, result) = oneshot::channel();
    thread::Builder::new().spawn(move || {
        // The CLI can exit while this worker is blocked. Write directly so
        // the worker never holds the Rust stdout lock during process cleanup.
        let stdout = io::stdout();
        let mut bytes = line.as_bytes();
        let status = loop {
            if bytes.is_empty() || done.is_closed() {
                break Ok(());
            }
            match rustix::io::write(stdout.as_fd(), bytes) {
                Ok(0) => break Err(io::ErrorKind::WriteZero.into()),
                Ok(count) => bytes = &bytes[count..],
                Err(rustix::io::Errno::INTR) => continue,
                Err(error) => break Err(error.into()),
            }
        };
        let _ = done.send(status);
    })?;
    result.await.map_err(io::Error::other)?
}
