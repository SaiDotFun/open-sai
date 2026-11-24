use sai_core::errors::{Code, ExitCode, SaiError};

#[test]
fn stable_error_codes_and_exit_statuses() {
    use Code::*;
    for (code, name, exit) in [
        (SignedOut, "SIGNED_OUT", 6),
        (SessionExpired, "SESSION_EXPIRED", 6),
        (SessionUnavailable, "SESSION_UNAVAILABLE", 1),
        (OutcomeUnknown, "OUTCOME_UNKNOWN", 5),
        (AdmissionRejected, "ADMISSION_REJECTED", 4),
        (NotSent, "NOT_SENT", 4),
        (PreviewMismatch, "PREVIEW_MISMATCH", 4),
        (PriceMoved, "PRICE_MOVED", 4),
        (RecentOpenPending, "RECENT_OPEN_PENDING", 1),
        (InvalidRequest, "INVALID_REQUEST", 1),
        (KeeperError, "KEEPER_ERROR", 1),
        (IncompleteData, "INCOMPLETE_DATA", 1),
        (StreamError, "STREAM_ERROR", 1),
        (InternalError, "INTERNAL_ERROR", 1),
    ] {
        assert_eq!(serde_json::to_value(code).unwrap(), name);
        let error = SaiError::new(code, "fixture message");
        assert_eq!(error.exit_code as u8, exit);
        assert_eq!(error.retryable(), exit == 4);
        assert_eq!(
            error.hint.as_deref(),
            (exit == 6).then_some("sai auth login NAME")
        );
    }
    assert_eq!(ExitCode::Ok as u8, 0);
    assert_eq!(SaiError::usage("fixture usage").exit_code as u8, 2);
    assert_eq!(ExitCode::Unsupported as u8, 3);
}
