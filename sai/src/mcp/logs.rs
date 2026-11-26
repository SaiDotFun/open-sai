use std::time::Duration;

use sai_core::{
    errors::{Code, SaiError},
    log,
};
use serde_json::{Map, Value, json};

use super::SaiMcp;

impl SaiMcp {
    pub(super) async fn logs_intent(&self, args: &Map<String, Value>) -> Result<Value, SaiError> {
        let key = args["key"]
            .as_str()
            .ok_or_else(SaiError::internal)?
            .to_owned();
        let Ok(slot) = self.log_slots.clone().try_acquire_owned() else {
            return Ok(json!({
                "error": "Log queries are busy. Retry shortly.",
                "code": Code::InternalError,
            }));
        };
        let offline = args
            .get("offline")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let addr = args.get("addr").and_then(Value::as_str).map(str::to_owned);
        let before = args
            .get("before")
            .and_then(Value::as_str)
            .map(str::to_owned);
        let endpoint = (!offline).then(|| self.settings.endpoint().to_owned());
        let home = self.env.home.clone();
        let writer = self.writer.clone();
        let runtime = tokio::runtime::Handle::current();
        let (_cancel_on_drop, cancel) = tokio::sync::oneshot::channel::<()>();
        tokio::task::spawn_blocking(move || {
            let _slot = slot;
            if let Some(writer) = writer {
                writer.flush(Duration::from_secs(1));
            }
            runtime.block_on(async {
                tokio::select! {
                    biased;
                    _ = cancel => Err(SaiError::internal()),
                    result = log::query::intent(
                        &home, &key, endpoint.as_deref(), addr.as_deref(), offline, before.as_deref(),
                    ) => result,
                }
            })
        })
        .await
        .map_err(|_| SaiError::internal())?
    }
}
