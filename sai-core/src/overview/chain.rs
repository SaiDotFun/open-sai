use std::{
    collections::{HashMap, HashSet},
    sync::{
        Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use serde_json::{Value, json};
use tokio::time::Instant;

use crate::{
    keeper::{self, Keeper},
    lcd::{self, ReadError},
    settings::{Key, Settings, Source},
};

use super::{MAX_PAGES, PAGE_SIZE, Stamp, add_age, instant, iso, now, variables};

const LCD_BUDGET: Duration = Duration::from_secs(8);
const MEMBERSHIP_MAX_AGE: i64 = 60;

pub(super) struct Chain<'a> {
    lcd: &'a str,
    chain_id: String,
    contract: String,
    pub(super) height: u64,
    pub(super) time: Stamp,
    deadline: Instant,
    block_times: Mutex<HashMap<u64, Option<Stamp>>>,
}

impl<'a> Chain<'a> {
    pub(super) async fn open(settings: &'a Settings, keeper: &Keeper<'_>) -> Option<Self> {
        if settings.endpoint() != Key::KeeperEndpoint.default_value()
            && settings.source(Key::LcdEndpoint) == Source::Default
        {
            return None;
        }
        let route = keeper
            .read_until(
                keeper::ROUTE,
                variables(json!({})),
                Duration::from_secs(3),
                std::future::pending(),
            )
            .await
            .ok()?;
        let deadline = Instant::now() + LCD_BUDGET;
        let lcd = settings.lcd_endpoint();
        let header = lcd::block(lcd, "latest", deadline).await.ok()?;
        let route = route.get("oneClickTrading")?;
        let chain_id = route["chainId"]
            .as_str()
            .filter(|id| !id.is_empty() && header["chain_id"] == *id)?;
        Some(Self {
            lcd,
            chain_id: chain_id.to_owned(),
            contract: contract_address(&route["contractAddress"])?.to_owned(),
            height: height(&header["height"]).filter(|height| *height > 0)?,
            time: instant(&header["time"])?,
            deadline,
            block_times: Mutex::new(HashMap::new()),
        })
    }

    async fn query(&self, contract: Option<&str>, message: Value) -> Result<Value, ReadError> {
        lcd::smart(
            self.lcd,
            contract.unwrap_or(&self.contract),
            message,
            self.height,
            self.deadline,
        )
        .await
    }

    pub(super) async fn block_time(&self, requested: u64) -> Option<Stamp> {
        if requested == self.height {
            return Some(self.time);
        }
        if let Some(at) = self
            .block_times
            .lock()
            .expect("block time cache")
            .get(&requested)
        {
            return *at;
        }
        let at = lcd::block(self.lcd, &requested.to_string(), self.deadline)
            .await
            .ok()
            .filter(|header| {
                header["chain_id"] == self.chain_id && height(&header["height"]) == Some(requested)
            })
            .and_then(|header| instant(&header["time"]));
        self.block_times
            .lock()
            .expect("block time cache")
            .insert(requested, at);
        at
    }

    async fn live_trade_ids(&self, addr: &str) -> Option<HashSet<u64>> {
        // The live index may omit older trades until backfill completes.
        if self
            .query(None, json!({"get_base_size_seed_status": {}}))
            .await
            .ok()?
            != "complete"
        {
            return None;
        }
        let mut ids = HashSet::new();
        let mut last = None;
        for _ in 0..MAX_PAGES {
            let data = self
                .query(
                    None,
                    json!({"live_trades_by_user": {
                        "trader": addr, "start_after": last, "limit": PAGE_SIZE,
                    }}),
                )
                .await
                .ok()?;
            let page = data.as_array()?;
            for row in page {
                let index = row["trade_index"].as_u64()?;
                if last.is_some_and(|last| index <= last) {
                    return None;
                }
                ids.insert(index);
                last = Some(index);
            }
            if page.len() < PAGE_SIZE {
                return Some(ids);
            }
        }
        None
    }
}

pub(super) async fn membership(
    chain: Option<&Chain<'_>>,
    addr: &str,
    indexed: HashSet<u64>,
) -> Value {
    if let Some(chain) = chain
        && let Some(live) = chain.live_trade_ids(addr).await
    {
        let fetched = now();
        let age = fetched.signed_duration_since(chain.time);
        let status = if !(chrono::TimeDelta::zero()
            ..=chrono::TimeDelta::seconds(MEMBERSHIP_MAX_AGE))
            .contains(&age)
        {
            "stale"
        } else if live == indexed {
            "verified"
        } else {
            "mismatch"
        };
        let mut data =
            json!({"status": status, "fetchedAt": iso(fetched), "sourceBlock": chain.height});
        add_age(&mut data, Some(chain.time), fetched);
        data
    } else {
        json!({"status": "unavailable", "fetchedAt": null, "sourceBlock": null, "sourceAt": null, "sourceAgeSeconds": null})
    }
}

fn height(value: &Value) -> Option<u64> {
    value.as_u64().or_else(|| value.as_str()?.parse().ok())
}

fn contract_address(value: &Value) -> Option<&str> {
    value.as_str().filter(|value| {
        value.starts_with("nibi1") && value.bytes().all(|byte| byte.is_ascii_alphanumeric())
    })
}

pub(super) struct ContractReads<'a> {
    pub(super) chain: &'a Chain<'a>,
    pub(super) risk_read_failed: bool,
    timed_out: AtomicBool,
    groups: HashMap<String, Option<u64>>,
    rates: HashMap<(u64, u64), Option<(Value, Stamp)>>,
    markets: HashMap<u64, Option<Value>>,
    oracle: Option<String>,
}

impl<'a> ContractReads<'a> {
    pub(super) fn new(chain: &'a Chain<'a>) -> Self {
        Self {
            chain,
            risk_read_failed: false,
            timed_out: AtomicBool::new(false),
            groups: HashMap::new(),
            rates: HashMap::new(),
            markets: HashMap::new(),
            oracle: None,
        }
    }

    pub(super) fn deadline_expired(&self) -> bool {
        self.timed_out.load(Ordering::Relaxed) || Instant::now() >= self.chain.deadline
    }

    pub(super) async fn trade(&self, addr: &str, index: &Value) -> Option<Value> {
        self.query(None, json!({"get_trade": {"trader": addr, "index": index}}))
            .await
            .filter(|position| position["trade"].is_object())
    }

    pub(super) async fn query(&self, contract: Option<&str>, message: Value) -> Option<Value> {
        match self.chain.query(contract, message).await {
            Ok(value) => Some(value),
            Err(error) => {
                if matches!(error, ReadError::Timeout) {
                    self.timed_out.store(true, Ordering::Relaxed);
                }
                None
            }
        }
    }

    pub(super) async fn group_expiration(&mut self, index: &str) -> Option<u64> {
        if let Some(expiration) = self.groups.get(index) {
            return *expiration;
        }
        let expiration = self
            .query(None, json!({"get_group": {"index": index}}))
            .await
            .and_then(|value| value["stale_price_expiration_secs"].as_u64());
        self.groups.insert(index.into(), expiration);
        expiration
    }

    pub(super) async fn market(&mut self, id: u64) -> Option<&Value> {
        if !self.markets.contains_key(&id) {
            let data = self
                .query(
                    None,
                    json!({"get_market": {"index": format!("MarketIndex({id})")}}),
                )
                .await;
            self.markets.insert(id, data);
        }
        self.markets.get(&id)?.as_ref()
    }

    async fn read_rate(&mut self, base: u64, quote: u64) -> Option<(Value, Stamp)> {
        if self.oracle.is_none() {
            let data = self.query(None, json!({"get_oracle_address": {}})).await;
            let address = data.as_ref().and_then(contract_address).unwrap_or_default();
            self.oracle = Some(address.to_owned());
        }
        let oracle = self
            .oracle
            .as_deref()
            .filter(|address| !address.is_empty())?;
        let data = self
            .query(
                Some(oracle),
                json!({"get_exchange_rate_stamped": {"base": base, "quote": quote}}),
            )
            .await?;
        Some((data, now()))
    }

    pub(super) async fn rate(&mut self, base: u64, quote: u64) -> Option<&(Value, Stamp)> {
        let key = (base, quote);
        if !self.rates.contains_key(&key) {
            let rate = self.read_rate(base, quote).await;
            self.rates.insert(key, rate);
        }
        self.rates.get(&key)?.as_ref()
    }
}
