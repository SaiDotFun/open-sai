//! Cooperative cancellation for file locks shared by interactive commands.

use std::{
    fs::{File, TryLockError},
    io,
    sync::atomic::{AtomicBool, Ordering},
    thread,
    time::Duration,
};

pub fn check(flag: Option<&AtomicBool>) -> io::Result<()> {
    if flag.is_some_and(|flag| flag.load(Ordering::Relaxed)) {
        Err(io::ErrorKind::Interrupted.into())
    } else {
        Ok(())
    }
}

pub fn lock(file: &File, flag: Option<&AtomicBool>) -> io::Result<()> {
    if flag.is_none() {
        return file.lock();
    }
    loop {
        check(flag)?;
        match file.try_lock() {
            Ok(()) => return check(flag),
            Err(TryLockError::WouldBlock) => thread::sleep(Duration::from_millis(20)),
            Err(TryLockError::Error(error)) => return Err(error),
        }
    }
}
