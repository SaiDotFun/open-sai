//! Stable failure codes shared by commands, tools, and events.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Code {
    SignedOut,
    SessionExpired,
    SessionUnavailable,
    OutcomeUnknown,
    AdmissionRejected,
    NotSent,
    PreviewMismatch,
    PriceMoved,
    RecentOpenPending,
    InvalidRequest,
    KeeperError,
    IncompleteData,
    StreamError,
    InternalError,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum ExitCode {
    Ok = 0,
    Error = 1,
    Usage = 2,
    Unsupported = 3,
    Rejected = 4,
    Unknown = 5,
    SignIn = 6,
}

impl Code {
    pub const fn exit_code(self) -> ExitCode {
        match self {
            Self::SignedOut | Self::SessionExpired => ExitCode::SignIn,
            Self::OutcomeUnknown => ExitCode::Unknown,
            Self::AdmissionRejected | Self::NotSent | Self::PreviewMismatch | Self::PriceMoved => {
                ExitCode::Rejected
            }
            _ => ExitCode::Error,
        }
    }
}

#[derive(Debug, Error)]
#[error("{message}")]
pub struct SaiError {
    pub code: Code,
    pub message: String,
    pub exit_code: ExitCode,
    pub show_usage: bool,
    pub intent_key: Option<String>,
    pub hint: Option<String>,
    pub details: Option<Box<serde_json::Map<String, Value>>>,
    pub retryable_override: bool,
}

impl SaiError {
    pub fn new(code: Code, message: impl Into<String>) -> Self {
        let exit_code = code.exit_code();
        Self {
            code,
            message: message.into(),
            exit_code,
            show_usage: true,
            intent_key: None,
            hint: (exit_code == ExitCode::SignIn).then(|| "sai auth login NAME".into()),
            details: None,
            retryable_override: false,
        }
    }

    pub fn usage(message: impl Into<String>) -> Self {
        Self {
            exit_code: ExitCode::Usage,
            ..Self::new(Code::InvalidRequest, message)
        }
    }

    pub fn internal() -> Self {
        Self::new(Code::InternalError, "Unexpected error in Sai")
    }

    pub fn retryable(&self) -> bool {
        self.retryable_override || self.exit_code == ExitCode::Rejected
    }
}
