use serde_json::{Value, json};

use crate::{
    errors::SaiError,
    trade::position::{Lcd, Read},
};

use super::decimal;

pub(super) struct QuoteLcd<'a> {
    lcd: Lcd<'a>,
    contract: String,
}

impl<'a> QuoteLcd<'a> {
    pub(super) fn new(endpoint: &'a str, contract: String) -> Self {
        Self {
            lcd: Lcd(endpoint),
            contract,
        }
    }

    pub(super) async fn smart(
        &self,
        query: Value,
        height: Option<&str>,
    ) -> Result<Value, SaiError> {
        self.lcd
            .smart(
                &self.contract,
                query,
                height.map_or(Read::Snapshot, Read::At),
            )
            .await
            .map(|(value, _)| value)
    }

    pub(super) async fn market(&self, index: &Value, read: Read<'_>) -> Option<Value> {
        let (result, _) = self
            .lcd
            .smart(
                &self.contract,
                json!({"get_market": {"index": index}}),
                read,
            )
            .await
            .ok()?;
        result.is_object().then_some(result)
    }

    pub(super) async fn group(&self, pair: &Value, height: Option<&str>) -> Option<Value> {
        self.object(
            json!({"get_group": {"index": pair.get("group_index")?}}),
            height,
        )
        .await
    }

    pub(super) async fn fees(&self, pair: &Value, height: Option<&str>) -> Option<Value> {
        self.object(
            json!({"get_fees": {"index": pair.get("fee_index")?}}),
            height,
        )
        .await
    }

    async fn object(&self, query: Value, height: Option<&str>) -> Option<Value> {
        let result = self.smart(query, height).await.ok()?;
        result.is_object().then_some(result)
    }

    pub(super) async fn pair_leverage_cap(&self, index: &Value) -> Option<u128> {
        let cap = self
            .smart(
                json!({"get_pair_custom_max_leverage": {"index": index}}),
                None,
            )
            .await
            .ok()?;
        decimal(&cap)
    }

    pub(super) async fn block_timestamp(&self, height: &str) -> Option<i64> {
        self.lcd.block_timestamp(height).await.ok()
    }
}
