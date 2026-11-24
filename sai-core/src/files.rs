use std::{fs::File, io, path::Path, sync::mpsc, thread, time::Instant};

/// Wait for a read without letting a stalled filesystem hold up its caller.
/// The detached worker must only inspect state, since the read may finish
/// after the caller has given up.
pub fn read_before<T: Send + 'static>(
    deadline: Instant,
    read: impl FnOnce() -> io::Result<T> + Send + 'static,
) -> io::Result<T> {
    if Instant::now() >= deadline {
        return Err(io::ErrorKind::TimedOut.into());
    }
    let (send, receive) = mpsc::sync_channel(1);
    thread::Builder::new().spawn(move || {
        let _ = send.send(read());
    })?;
    match receive.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
        Ok(result) => result,
        Err(mpsc::RecvTimeoutError::Timeout) => Err(io::ErrorKind::TimedOut.into()),
        Err(mpsc::RecvTimeoutError::Disconnected) => Err(io::Error::other("file read failed")),
    }
}

/// Open a regular file without waiting for a writer if the path is a FIFO.
pub fn open_regular(path: &Path) -> io::Result<File> {
    #[cfg(unix)]
    let file = {
        use rustix::fs::{Mode, OFlags, open};
        File::from(open(
            path,
            OFlags::RDONLY | OFlags::NONBLOCK | OFlags::CLOEXEC,
            Mode::empty(),
        )?)
    };
    #[cfg(not(unix))]
    let file = File::open(path)?;

    if !file.metadata()?.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "not a regular file",
        ));
    }
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn a_read_that_has_not_finished_releases_the_caller() {
        let (release, hold) = mpsc::channel();
        let result = read_before(Instant::now() + Duration::from_millis(50), move || {
            hold.recv().map_err(io::Error::other)
        });
        let _ = release.send(());
        assert_eq!(result.unwrap_err().kind(), io::ErrorKind::TimedOut);
    }
}
