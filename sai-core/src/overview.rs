//! Read one account without signing in or reconciling the intent ledger.

mod chain;
mod trades;

use std::{collections::HashSet, path::Path};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::{DateTime, FixedOffset, SecondsFormat, Timelike, Utc};
use serde_json::{Map, Value, json};
use tokio::sync::OnceCell;

use crate::{
    endpoint::Endpoint,
    errors::{Code, SaiError},
    keeper::{
        self, Keeper,
        stream::{self, Subscription},
    },
    log::{self, EventContext},
    session::parse_expiry,
    settings::Settings,
    storage::{Session, Storage, StorageError},
};

use chain::Chain;
use trades::TradeSections;

const PAGE_SIZE: usize = 100;
const MAX_PAGES: usize = 20;
const PAGINATION_ERROR: &str = "The paginated list could not be confirmed and may be incomplete";
const BALANCE_KEYS: [(&str, &str); 3] = [
    ("USDC on Sai", "balance"),
    ("USDC Credits on Sai", "credits"),
    ("USDC Unsettled on Sai", "unsettled"),
];

type Object = Map<String, Value>;
type Stamp = DateTime<FixedOffset>;

pub async fn read(
    settings: &Settings,
    home: &Path,
    addr: Option<&str>,
    events: EventContext<'_>,
    interrupt: impl Future<Output = SaiError>,
) -> Result<Value, SaiError> {
    let storage = Storage::new(home);
    let source = if addr.is_some() {
        "argument"
    } else {
        "session"
    };
    let unavailable = |message: &str| SaiError::new(Code::SessionUnavailable, message);
    let accounts = storage.load_accounts();
    let (addr, session) = match accounts {
        Ok(accounts) => {
            log::remember(
                accounts
                    .iter()
                    .map(|(session, _)| session.token().to_owned()),
            );
            let (addr, section) = match addr {
                Some(addr) => {
                    let section = match accounts.selected(Some(addr)) {
                        Some((key, stored)) if stored.checked_address(key).is_err() => {
                            failed(incomplete("Stored session has an invalid address"))
                        }
                        selected => session_section(
                            selected.map(|(_, stored)| stored),
                            settings.endpoint(),
                            &storage,
                        ),
                    };
                    (addr, section)
                }
                None => {
                    let (key, active) = accounts
                        .selected(None)
                        .ok_or_else(|| unavailable("No active Sai account"))?;
                    if active.endpoint() != Some(settings.endpoint()) {
                        return Err(unavailable(
                            "The active session belongs to another Keeper endpoint",
                        ));
                    }
                    let addr = active
                        .checked_address(key)
                        .map_err(|_| unavailable("Stored session has an invalid address"))?;
                    (
                        addr,
                        session_section(Some(active), settings.endpoint(), &storage),
                    )
                }
            };
            (addr.to_owned(), section)
        }
        Err(error) => {
            let addr = addr.ok_or_else(|| unavailable(&error.to_string()))?;
            (addr.to_owned(), failed(incomplete(error.to_string())))
        }
    };

    let keeper = Keeper::new(settings.endpoint(), events);
    let view = Overview {
        settings,
        keeper,
        addr: &addr,
        chain: OnceCell::new(),
    };
    tokio::pin!(interrupt);
    let mut balances = view.balances(interrupt.as_mut()).await?;
    tokio::select! {
        biased;
        error = &mut interrupt => Err(error),
        result = view.read_sections(&storage, &mut balances, session, source) => Ok(result),
    }
}

impl Overview<'_> {
    async fn read_sections(
        &self,
        storage: &Storage<'_>,
        balances: &mut Value,
        session: Value,
        source: &str,
    ) -> Value {
        let TradeSections {
            positions,
            orders,
            prices,
        } = trades::read(self).await;
        self.balance_sources(balances).await;
        let queued = self.queued().await;
        let intents = blocking_intents(storage, self.settings.endpoint(), self.addr);
        json!({
            "addr": self.addr,
            "addrSource": source,
            "balances": balances,
            "positions": positions,
            "pendingOrders": orders,
            "queuedMarketOrders": queued,
            "prices": prices,
            "blockingIntents": intents,
            "session": session,
        })
    }
}

struct Overview<'a> {
    settings: &'a Settings,
    keeper: Keeper<'a>,
    addr: &'a str,
    chain: OnceCell<Option<Chain<'a>>>,
}

struct Pages {
    rows: Vec<Object>,
    complete: bool,
    paginated: bool,
    read_failed: bool,
}

impl Overview<'_> {
    async fn chain(&self) -> Option<&Chain<'_>> {
        self.chain
            .get_or_init(|| Chain::open(self.settings, &self.keeper))
            .await
            .as_ref()
    }

    async fn block_source(
        &self,
        height: &Value,
        time: &Value,
        fetched: Stamp,
        lookup: bool,
    ) -> Value {
        let mut at = instant(time);
        if lookup
            && at.is_none()
            && let Some(height) = height.as_u64().filter(|height| *height > 0)
            && let Some(chain) = self.chain().await
        {
            at = chain.block_time(height).await;
        }
        let mut source = json!({"sourceBlock": height});
        add_age(&mut source, at, fetched);
        source
    }

    fn same_keeper(&self) -> bool {
        Endpoint::split(self.settings.endpoint())
            .is_some_and(|url| url.websocket() == self.settings.ws_endpoint())
    }

    async fn balances(&self, interrupt: impl Future<Output = SaiError>) -> Result<Value, SaiError> {
        let endpoint = self.settings.ws_endpoint();
        let payload = stream::snapshot(
            endpoint,
            &Subscription::BalancesWithBlock(self.addr.to_owned()),
            || {},
            interrupt,
        )
        .await;
        let mut section = match payload {
            Ok(payload) => match parse_balances(payload) {
                Ok(rows) => success(rows, now()),
                Err(error) => failed(error),
            },

            Err(error) if error.code != Code::StreamError => return Err(error),
            Err(error) => failed(error),
        };
        let matches = self.same_keeper();
        section["source"] = json!({"endpoint": log::origin(endpoint), "matchesKeeper": matches});
        if !matches {
            section["warning"] =
                json!("Balance WebSocket is independent. These funds may be on a different chain.");
        }
        Ok(section)
    }

    async fn balance_sources(&self, section: &mut Value) {
        let Some(rows) = section.get_mut("data").map(Value::take) else {
            return;
        };
        let fetched = instant(&section["fetchedAt"]).expect("section time");
        if rows.as_object().is_some_and(Object::is_empty) {
            section["error"] = error_value(incomplete(
                "Keeper returned no Sai balance rows. Funds are unknown.",
            ));
        }
        let mut data = Object::new();
        for (_, key) in BALANCE_KEYS {
            let mut row = json!({"usd": rows[key]["usd"]});
            merge(
                &mut row,
                self.block_source(
                    &rows[key]["block"],
                    &Value::Null,
                    fetched,
                    self.same_keeper(),
                )
                .await,
            );
            data.insert(key.into(), row);
        }
        section["data"] = json!(data);
    }

    async fn scan(&self, query: &str, field: &str, id: &str) -> Result<Pages, SaiError> {
        let keep_malformed_rows = query == keeper::PENDING_MARKET_ORDERS;
        let mut rows = Vec::new();
        let mut seen = HashSet::new();
        for page in 0..MAX_PAGES {
            let vars =
                variables(json!({"trader": self.addr, "limit": PAGE_SIZE, "offset": rows.len()}));
            let result = self
                .keeper
                .read_until(query, vars, keeper::TIMEOUT, std::future::pending())
                .await
                .and_then(|data| {
                    data.get("perp")
                        .and_then(|perp| perp[field].as_array())
                        .ok_or_else(invalid)?
                        .iter()
                        .map(|row| {
                            let key = row[id].as_u64();
                            if keep_malformed_rows && (key.is_none() || !valid_queued_order(row)) {
                                // Keep one slot per response row so offset pagination
                                // advances past malformed queued orders.
                                return Ok((None, Object::new()));
                            }
                            let fields = row
                                .as_object()
                                .filter(|_| key.is_some())
                                .ok_or_else(invalid)?;
                            Ok((key, fields.clone()))
                        })
                        .collect::<Result<Vec<_>, SaiError>>()
                });
            let entries = match result {
                Ok(entries) => entries,
                Err(error) if rows.is_empty() => return Err(error),
                Err(_) => {
                    return Ok(Pages {
                        rows,
                        complete: false,
                        paginated: true,
                        read_failed: true,
                    });
                }
            };
            let count = entries.len();
            let fetched = json!(iso(now()));
            for (key, mut row) in entries {
                if key.is_some_and(|key| !seen.insert(key)) {
                    return Ok(Pages {
                        rows,
                        complete: false,
                        paginated: true,
                        read_failed: false,
                    });
                }
                row.insert("fetchedAt".into(), fetched.clone());
                rows.push(row);
            }
            if count < PAGE_SIZE {
                return Ok(Pages {
                    rows,
                    complete: true,
                    paginated: page > 0,
                    read_failed: false,
                });
            }
        }
        Ok(Pages {
            rows,
            complete: false,
            paginated: true,
            read_failed: false,
        })
    }

    async fn pages(&self, query: &str, field: &str, id: &str) -> Result<Pages, SaiError> {
        let first = self.scan(query, field, id).await?;
        if !first.paginated || first.read_failed {
            return Ok(first);
        }
        let ids = |scan: &Pages| {
            scan.rows
                .iter()
                .map(|row| row[id].as_u64())
                .collect::<Vec<_>>()
        };
        let mut previous_ids = ids(&first);
        let mut previous_complete = first.complete;
        let mut best = first;
        // Keeper uses offsets. Confirm consecutive scans agree before trusting a paginated list.
        for _ in 0..2 {
            let Ok(current) = self.scan(query, field, id).await else {
                break;
            };
            let current_ids = ids(&current);
            if previous_complete && current.complete && previous_ids == current_ids {
                return Ok(current);
            }
            previous_ids = current_ids;
            previous_complete = current.complete;
            let read_failed = current.read_failed;
            if current.complete || (!best.complete && current.rows.len() > best.rows.len()) {
                best = current;
            }
            if read_failed {
                break;
            }
        }
        best.complete = false;
        Ok(best)
    }

    async fn queued(&self) -> Value {
        let pages = match self
            .pages(keeper::PENDING_MARKET_ORDERS, "marketOrders", "sequence")
            .await
        {
            Ok(pages) => pages,
            Err(error) => return failed(error),
        };
        let fields = [
            ("sequence", "sequence"),
            ("tradeId", "tradeId"),
            ("action", "side"),
            ("marketId", "perpMarketId"),
            ("status", "status"),
            ("collateralBaseUnits", "collateralAmount"),
            ("requestTxHash", "orderReqTxHash"),
            ("requestEvmTxHash", "orderReqEvmTxHash"),
            ("fetchedAt", "fetchedAt"),
        ];
        let mut rows = Vec::new();
        let mut malformed = false;
        for row in &pages.rows {
            let Some(mapped) = fields
                .iter()
                .map(|(out, key)| Some((out.to_string(), row.get(*key)?.clone())))
                .collect::<Option<Object>>()
            else {
                malformed = true;
                continue;
            };
            rows.push((Value::Object(mapped), row));
        }
        let fetched = now();
        let mut projected = Vec::with_capacity(rows.len());
        for (mut mapped, raw) in rows {
            merge(
                &mut mapped,
                self.block_source(
                    &raw["orderReqBlock"],
                    &raw["orderReqBlockTs"],
                    instant(&raw["fetchedAt"]).expect("page time"),
                    true,
                )
                .await,
            );
            projected.push(mapped);
        }
        let mut section = success(json!(projected), fetched);
        const MALFORMED_ROWS: &str =
            "Keeper returned invalid queued orders. Invalid rows were omitted.";
        let error = match (malformed, pages.complete) {
            (true, true) => Some(incomplete(MALFORMED_ROWS)),
            (true, false) => Some(incomplete(format!("{MALFORMED_ROWS} {PAGINATION_ERROR}"))),
            (false, false) => Some(incomplete(PAGINATION_ERROR)),
            (false, true) => None,
        };
        if let Some(error) = error {
            section["error"] = error_value(error);
        }
        section
    }
}

fn valid_queued_order(row: &Value) -> bool {
    let valid_hash = |key| {
        row.get(key)
            .is_some_and(|value| !keeper::request_hash(value).1)
    };
    row["tradeId"].as_u64().is_some()
        && row["perpMarketId"].as_u64().is_some()
        && matches!(
            row["side"].as_str(),
            Some("open" | "close" | "increase" | "decrease" | "update_leverage")
        )
        && row["status"] == "pending_exec"
        && row
            .get("collateralAmount")
            .is_some_and(|value| value.is_null() || valid_base_units(value))
        && valid_hash("orderReqTxHash")
        && valid_hash("orderReqEvmTxHash")
        && row["orderReqBlock"].as_u64().is_some()
        && instant(&row["orderReqBlockTs"]).is_some()
}

fn parse_balances(payload: Value) -> Result<Value, SaiError> {
    let mut selected = Object::new();
    for row in payload["data"]["userBalances"]
        .as_array()
        .ok_or_else(invalid)?
    {
        let token = &row["token_info"];
        let kind = token["type"].as_str().ok_or_else(invalid)?;
        if kind != "sai" {
            continue;
        }
        let name = token["name"].as_str().ok_or_else(invalid)?;
        if let Some((_, key)) = BALANCE_KEYS.iter().find(|(label, _)| name == *label) {
            let usd = number(&row["usd_value"]).ok_or_else(invalid)?;
            let height = &row["lastUpdatedBlock"];
            if !valid_block(height, &Value::Null) {
                return Err(invalid());
            }
            selected.insert((*key).to_owned(), json!({"usd": usd, "block": height}));
        }
    }
    Ok(Value::Object(selected))
}

fn session_section(stored: Option<&Session>, endpoint: &str, storage: &Storage<'_>) -> Value {
    let fetched = now();
    let Some(stored) = stored else {
        return success(json!({"connected": false}), fetched);
    };
    let Some(expiry) = parse_expiry(stored.expires_at()) else {
        return failed(invalid());
    };
    let matches = stored.endpoint() == Some(endpoint);
    let expired = expiry <= fetched;
    let mut data = json!({"connected": true, "expiresAt": stored.expires_at(), "expired": expired, "endpointMatches": matches});
    let issued_at = stored
        .token()
        .split('.')
        .nth(1)
        .and_then(|part| URL_SAFE_NO_PAD.decode(part.trim_end_matches('=')).ok())
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .and_then(|claims| unix_instant(&claims["iat"]));
    add_age(&mut data, issued_at, fetched);
    if matches
        && let Some(notice) = storage.expiry_notice(stored, expiry.signed_duration_since(fetched))
    {
        merge(&mut data, json!(notice));
    }
    success(data, fetched)
}

fn blocking_intents(storage: &Storage<'_>, endpoint: &str, addr: &str) -> Value {
    let rows = match storage.unresolved_intents(endpoint, addr) {
        Ok(rows) => rows,
        Err(StorageError::NewerLedger) => return failed(invalid()),
        Err(_) => {
            return failed(SaiError::new(
                Code::InternalError,
                "Could not read local Sai data",
            ));
        }
    };
    let fetched = now();
    let rows: Vec<_> = rows.into_iter().filter_map(|row| {
        let trade_id = row.entity.filter(|id| !id.is_empty());
        let scope = match row.status.as_str() {
            "unknown" | "pending" => "allTrades",
            _ if trade_id.is_some() => "sameTrade",
            _ if row.kind == "OPEN_TRADE" => "openTrades",
            _ => return None,
        };
        let mut item = json!({"intentKey": row.key, "kind": row.kind, "status": row.status, "scope": scope, "tradeId": trade_id});
        add_age(&mut item, unix_instant(&json!(row.created_at)), fetched);
        Some(item)
    }).collect();
    success(json!(rows), fetched)
}

fn variables(value: Value) -> Object {
    match value {
        Value::Object(object) => object,
        _ => unreachable!("variables are an object"),
    }
}

fn merge(into: &mut Value, from: Value) {
    into.as_object_mut()
        .expect("overview row")
        .extend(variables(from));
}

fn number(value: &Value) -> Option<f64> {
    value.as_f64().filter(|value| value.is_finite())
}

fn valid_base_units(value: &Value) -> bool {
    value
        .as_str()
        .or_else(|| value.as_number().map(serde_json::Number::as_str))
        .is_some_and(|text| {
            text.bytes().all(|byte| byte.is_ascii_digit()) && text.parse::<u128>().is_ok()
        })
}

fn valid_block(height: &Value, timestamp: &Value) -> bool {
    (height.is_null() || height.as_u64().is_some())
        && (timestamp.is_null() || timestamp.is_string())
}

fn now() -> Stamp {
    let stamp = Utc::now();
    stamp
        .with_nanosecond(stamp.timestamp_subsec_micros() * 1000)
        .expect("valid microseconds")
        .fixed_offset()
}

fn iso(stamp: Stamp) -> String {
    let precision = if stamp.timestamp_subsec_micros() == 0 {
        SecondsFormat::Secs
    } else {
        SecondsFormat::Micros
    };
    stamp.to_rfc3339_opts(precision, false)
}

fn instant(value: &Value) -> Option<Stamp> {
    let text = value.as_str()?;
    let stamp = DateTime::parse_from_rfc3339(text)
        .ok()
        .or_else(|| parse_expiry(text).map(|stamp| stamp.fixed_offset()))?;
    let stamp = stamp.with_nanosecond(stamp.timestamp_subsec_micros() * 1000)?;
    (stamp.timestamp_micros() > 0).then_some(stamp)
}

fn unix_instant(value: &Value) -> Option<Stamp> {
    let value = number(value)?;
    if !(-62_135_596_800.0..253_402_300_800.0).contains(&value) {
        return None;
    }
    let seconds = value.floor();
    DateTime::from_timestamp(
        seconds as i64,
        ((value - seconds) * 1e6).round_ties_even() as u32 * 1000,
    )
    .or_else(|| DateTime::from_timestamp(seconds as i64 + 1, 0))
    .filter(|stamp| (-62_135_596_800..253_402_300_800).contains(&stamp.timestamp()))
    .map(|stamp| stamp.fixed_offset())
}

fn add_age(row: &mut Value, at: Option<Stamp>, fetched: Stamp) {
    row["sourceAt"] = json!(at.map(iso));
    row["sourceAgeSeconds"] = json!(at.map(|at| {
        let micros = fetched
            .signed_duration_since(at)
            .num_microseconds()
            .expect("overview timestamp range");
        (micros as f64 / 1000.0).round_ties_even() / 1000.0
    }));
}

fn error_value(error: SaiError) -> Value {
    let mut data =
        json!({"code": error.code, "message": error.message, "retryable": error.retryable()});
    if let Some(hint) = error.hint {
        data["hint"] = json!(hint);
    }
    if let Some(key) = error.intent_key {
        data["intentKey"] = json!(key);
    }
    if let Some(details) = error.details {
        data["details"] = json!(details);
    }
    data
}

fn incomplete(message: impl Into<String>) -> SaiError {
    SaiError::new(Code::IncompleteData, message)
}

fn invalid() -> SaiError {
    incomplete("The source returned invalid data")
}

fn failed(error: SaiError) -> Value {
    json!({"fetchedAt": null, "error": error_value(error)})
}

fn success(data: Value, at: Stamp) -> Value {
    json!({"fetchedAt": iso(at), "data": data})
}
