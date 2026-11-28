use std::{
    io,
    os::fd::{AsFd, BorrowedFd},
    thread,
};

use tokio::sync::oneshot;

/// Keep the CLI responsive to SIGINT even when an inherited stdout pipe is full.
pub(super) async fn write_stdout(line: String) -> io::Result<()> {
    let (done, result) = oneshot::channel();
    thread::Builder::new().spawn(move || {
        // The CLI can exit while this worker is blocked. Write directly so
        // the worker never holds the Rust stdout lock during process cleanup.
        let stdout = io::stdout();
        let status = write_all(stdout.as_fd(), line.as_bytes(), || done.is_closed());
        let _ = done.send(status);
    })?;
    result.await.map_err(io::Error::other)?
}

/// Write a progress line to stderr without waiting for it, so a paused
/// terminal or a full pipe never holds up polling or Ctrl-C.
pub(crate) fn write_stderr_detached(line: String) {
    let _ = thread::Builder::new().spawn(move || {
        let stderr = io::stderr();
        let _ = write_all(stderr.as_fd(), line.as_bytes(), || false);
    });
}

fn write_all(fd: BorrowedFd<'_>, mut bytes: &[u8], stop: impl Fn() -> bool) -> io::Result<()> {
    loop {
        if bytes.is_empty() || stop() {
            return Ok(());
        }
        match rustix::io::write(fd, bytes) {
            Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
            Ok(count) => bytes = &bytes[count..],
            Err(rustix::io::Errno::INTR) => continue,
            Err(error) => return Err(error.into()),
        }
    }
}
