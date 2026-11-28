//! Match market symbols to the USDC market IDs that Keeper lists.

use std::{collections::BTreeSet, future::Future};

use serde_json::{Map, Value};

use super::{Keeper, MARKETS, TIMEOUT};
use crate::{
    errors::{Code, SaiError},
    settings::repr,
};

/// Select the USDC-collateral rows from a Keeper `MARKETS` response.
pub fn usdc_markets(data: &Map<String, Value>) -> Result<impl Iterator<Item = &Value>, SaiError> {
    let rows = data
        .get("perp")
        .and_then(|perp| perp["borrowings"].as_array())
        .ok_or_else(SaiError::internal)?;
    Ok(rows
        .iter()
        .filter(|row| row["collateralToken"]["symbol"] == "USDC"))
}

/// Report whether a market argument names a symbol rather than an ID.
pub fn is_market_symbol(market: &str) -> bool {
    market.is_ascii() && market.bytes().any(|byte| byte.is_ascii_alphabetic())
}

/// The USDC markets whose base token symbol matches, ignoring ASCII case.
pub struct MarketSymbol(BTreeSet<u64>);

impl MarketSymbol {
    pub fn find(data: &Map<String, Value>, symbol: &str) -> Result<Self, SaiError> {
        usdc_markets(data)?
            .filter(|row| {
                row["baseToken"]["symbol"]
                    .as_str()
                    .is_some_and(|base| base.eq_ignore_ascii_case(symbol))
            })
            .map(|row| row["marketId"].as_u64().ok_or_else(SaiError::internal))
            .collect::<Result<_, _>>()
            .map(Self)
    }

    /// Return the one matching market ID. `unknown` names the caller's input
    /// and where to find IDs. More than one match lists the candidate IDs.
    pub fn id(self, symbol: &str, unknown: &str) -> Result<u64, SaiError> {
        let mut ids = self.0.into_iter();
        match (ids.next(), ids.next()) {
            (None, _) => Err(SaiError::new(Code::InvalidRequest, unknown)),
            (Some(id), None) => Ok(id),
            (Some(first), Some(second)) => {
                let ids = [first, second]
                    .into_iter()
                    .chain(ids)
                    .map(|id| id.to_string())
                    .collect::<Vec<_>>()
                    .join(", ");
                Err(SaiError::new(
                    Code::InvalidRequest,
                    format!(
                        "Ambiguous market symbol {}. Pass a market ID: {ids}.",
                        repr(symbol)
                    ),
                ))
            }
        }
    }
}

impl Keeper<'_> {
    /// Read Keeper's markets and match `symbol` against their base tokens.
    pub async fn market_symbol(
        &self,
        symbol: &str,
        interrupt: impl Future<Output = SaiError>,
    ) -> Result<MarketSymbol, SaiError> {
        let response = self
            .read_until(MARKETS, Map::new(), TIMEOUT, interrupt)
            .await?;
        MarketSymbol::find(&response, symbol)
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn markets() -> Map<String, Value> {
        let row = |id: u64, base: &str, collateral: &str| {
            json!({
                "marketId": id,
                "baseToken": {"symbol": base},
                "collateralToken": {"symbol": collateral},
            })
        };
        json!({"perp": {"borrowings": [
            row(1, "ETH", "USDC"),
            row(2, "BTC", "USDC"),
            row(3, "btc", "USDC"),
            row(4, "SOL", "stNIBI"),
        ]}})
        .as_object()
        .unwrap()
        .clone()
    }

    fn resolve(symbol: &str) -> Result<u64, SaiError> {
        MarketSymbol::find(&markets(), symbol)
            .unwrap()
            .id(symbol, "unknown")
    }

    #[test]
    fn matches_one_usdc_market_ignoring_case() {
        assert_eq!(resolve("eth").unwrap(), 1);
        assert_eq!(resolve("ETH").unwrap(), 1);
    }

    #[test]
    fn lists_every_id_for_an_ambiguous_symbol() {
        let error = resolve("Btc").unwrap_err();
        assert_eq!(error.code, Code::InvalidRequest);
        assert_eq!(
            error.message,
            "Ambiguous market symbol 'Btc'. Pass a market ID: 2, 3."
        );
    }

    #[test]
    fn ignores_markets_without_usdc_collateral() {
        let error = resolve("SOL").unwrap_err();
        assert_eq!(error.code, Code::InvalidRequest);
        assert_eq!(error.message, "unknown");
    }

    #[test]
    fn treats_ascii_text_with_a_letter_as_a_symbol() {
        assert!(is_market_symbol("eth"));
        assert!(is_market_symbol("3e0"));
        assert!(!is_market_symbol("003"));
        assert!(!is_market_symbol("٣"));
        assert!(!is_market_symbol("Ä"));
    }
}
