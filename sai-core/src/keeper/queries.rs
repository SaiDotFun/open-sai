//! GraphQL documents shared with the Python CLI at 5b6e5fbf26975a837dfd0a0cad1d1fd40c5b2bdf.

pub const MARKETS: &str = "query { perp { borrowings { marketId price isOpen visible minLeverage maxLeverage minPositionSizeUSD openFeePct closeFeePct baseToken { id symbol name } quoteToken { symbol } collateralToken { id symbol } } } }";
pub const MARKET: &str = "query($market:Int!, $collateral:Int!) { perp { borrowing(marketId:$market,collateralId:$collateral) { marketId isOpen minLeverage maxLeverage collateralToken { id symbol } } } }";
pub const PRICES: &str = "query { oracle { tokenPricesUsd(limit: 100) { priceUsd token { id symbol name } lastUpdatedBlock { block block_ts } } } }";
pub const POSITIONS: &str = "query($trader:String!) { perp { trades(where:{trader:$trader,isOpen:true}, limit:100) { id trader isOpen isLong tradeType collateralAmount leverage openPrice tp sl state { liquidationPrice pnlCollateral pnlPct positionValue } perpBorrowing { marketId baseToken { symbol } collateralToken { symbol } } } } }";
pub const POSITION: &str = "query($trader:String!, $id:Int!) { perp { trade(trader:$trader,id:$id) { id trader isOpen tradeType collateralAmount leverage perpBorrowing { marketId collateralToken { id symbol } } } } }";
pub const ROUTE: &str = "query { oneClickTrading { enabled chainId contractAddress } }";
pub const HISTORY: &str = "query($trader:String!) { perp { tradeHistory(where:{trader:$trader}, limit:50, order_desc:true) { id intentKey tradeChangeType txHash evmTxHash block { block block_ts } trade { id trader } } } }";
pub const INTENTS: &str = "query($user:String!, $key:String) { userIntents(where:{user:$user, intentKey:$key}, limit:50) { intentKey kind lifecycleStep execActor evmAddress nibiAddress txHash execBlock execBlockTs errorCode errorMsg } }";
pub const CHALLENGE: &str = "mutation($address:String!) { createUserIntentAuthChallenge(evmAddress:$address) { nonce evmAddress expiresAt chainId typedData } }";
pub const SESSION: &str = "mutation($signature:String!, $challenge:String!) { createUserIntentSession(signature:$signature, challenge:$challenge) { token evmAddress expiresAt } }";
pub const SUBMIT: &str = "mutation($input:RecordUserIntentInput!) { recordUserIntent(input:$input) { intentKey kind lifecycleStep execActor evmAddress txHash errorCode errorMsg } }";

pub(super) fn operation(query: &str) -> &'static str {
    match query {
        CHALLENGE => "challenge",
        HISTORY => "history",
        INTENTS => "intents",
        MARKET => "market",
        MARKETS => "markets",
        POSITION => "position",
        POSITIONS => "positions",
        PRICES => "prices",
        ROUTE => "route",
        SESSION => "session",
        SUBMIT => "submit",
        _ => "custom",
    }
}

pub(crate) fn is_log_operation(name: &str) -> bool {
    // These names also occur in logs written by Python account overview.
    matches!(name, "overview_trades" | "pending_market_orders")
        || [
            CHALLENGE, HISTORY, INTENTS, MARKET, MARKETS, POSITION, POSITIONS, PRICES, ROUTE,
            SESSION, SUBMIT, "",
        ]
        .into_iter()
        .any(|query| operation(query) == name)
}
