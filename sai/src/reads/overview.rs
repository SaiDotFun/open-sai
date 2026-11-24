//! Human-readable output for the account overview.

use std::io::{self, IsTerminal, Write as _};

use sai_core::{errors::SaiError, session::ExpiryNotice};
use serde_json::Value;

const UNKNOWN: &str = "unknown";

pub(super) fn show(result: &Value) -> Result<(), SaiError> {
    let color = io::stdout().is_terminal()
        && std::env::var_os("NO_COLOR").is_none_or(|value| value.is_empty());
    let mut display = Display {
        out: String::new(),
        color,
    };
    let session = &result["session"]["data"];
    let renewal = session["expiryWarning"].as_str().map(|warning| {
        ExpiryNotice {
            expiry_warning: warning.to_owned(),
            renewal_command: session["renewalCommand"].as_str().map(str::to_owned),
            renewal_guidance: session["renewalGuidance"].as_str().map(str::to_owned),
        }
        .text()
    });
    if io::stdin().is_terminal()
        && io::stderr().is_terminal()
        && let Some(renewal) = &renewal
    {
        eprintln!("{renewal}");
    }
    display.line(format!(
        "Sai account {} ({})",
        text(&result["addr"]),
        text(&result["addrSource"])
    ));

    if let Some(balances) = display.section(result, "balances", "Sai Funds") {
        display.table(
            &["Funds", "USD", "Source (Age)"],
            [
                ("Balance", "balance"),
                ("Credits", "credits"),
                ("Unsettled Teller", "unsettled"),
            ]
            .into_iter()
            .map(|(label, key)| {
                let row = &balances[key];
                vec![label.into(), number(&row["usd"], ""), source(row)]
            }),
        );
    }
    if let Some(positions) = display.section(result, "positions", "Open Positions") {
        display.positions(positions);
    }
    if let Some(orders) = display.section(result, "pendingOrders", "Pending Limit and Stop Orders")
    {
        display.table(
            &[
                "ID",
                "Market",
                "Side",
                "Type",
                "Trigger USD",
                "Size USD",
                "Collateral USD",
                "Leverage",
                "TP USD",
                "SL USD",
                "Source (Age)",
            ],
            items(orders).iter().map(|row| {
                vec![
                    text(&row["id"]),
                    market(row),
                    text(&row["side"]),
                    text(&row["orderType"]),
                    number(&row["triggerPriceUsd"], ""),
                    number(&row["sizeUsd"], ""),
                    collateral(row),
                    number(&row["leverage"], "x"),
                    number(&row["takeProfitPriceUsd"], ""),
                    number(&row["stopLossPriceUsd"], ""),
                    source(row),
                ]
            }),
        );
    }
    if let Some(queued) = display.section(result, "queuedMarketOrders", "Queued Market Orders") {
        display.table(
            &[
                "Sequence",
                "Trade ID",
                "Market ID",
                "Action",
                "Status",
                "Collateral Base Units",
                "Source (Age)",
            ],
            items(queued).iter().map(|row| {
                vec![
                    text(&row["sequence"]),
                    text(&row["tradeId"]),
                    text(&row["marketId"]),
                    text(&row["action"]),
                    text(&row["status"]),
                    number(&row["collateralBaseUnits"], ""),
                    source(row),
                ]
            }),
        );
        for row in items(queued) {
            for (field, network) in [("requestTxHash", "Cosmos"), ("requestEvmTxHash", "EVM")] {
                if let Some(hash) = row[field].as_str().filter(|hash| !hash.is_empty()) {
                    display.line(format!(
                        "  Order {} {network} request tx {hash}",
                        text(&row["sequence"])
                    ));
                }
            }
        }
    }
    if let Some(prices) = display.section(result, "prices", "Oracle Prices") {
        display.table(
            &["Token", "USD", "Source (Age)"],
            items(prices)
                .iter()
                .map(|row| vec![text(&row["token"]), number(&row["usd"], ""), source(row)]),
        );
    }
    if let Some(intents) =
        display.section(result, "blockingIntents", "Local Intents Blocking Trades")
    {
        display.table(
            &[
                "Intent Key",
                "Kind",
                "Status",
                "Scope",
                "Trade ID",
                "Reserved (Age)",
            ],
            items(intents).iter().map(|row| {
                vec![
                    text(&row["intentKey"]),
                    text(&row["kind"]),
                    text(&row["status"]),
                    text(&row["scope"]),
                    row["tradeId"].as_str().unwrap_or("-").to_owned(),
                    source(row),
                ]
            }),
        );
    }
    if display
        .section(result, "session", "Trading Session")
        .is_some()
    {
        if session["connected"] == true {
            let status = if session["endpointMatches"] == false {
                "Belongs to another endpoint"
            } else if session["expired"] == true {
                "Expired"
            } else {
                "Connected"
            };
            display.line(format!("  {status} until {}", text(&session["expiresAt"])));
            display.line(format!("  Issued at {}", source(session)));
            if let Some(renewal) = renewal {
                display.line(format!("  {renewal}"));
            }
        } else {
            display.line("  No local session for this account");
        }
    }
    io::stdout()
        .lock()
        .write_all(display.out.as_bytes())
        .map_err(|_| SaiError::internal())
}

struct Display {
    out: String,
    color: bool,
}

impl Display {
    fn line(&mut self, text: impl AsRef<str>) {
        self.out.push_str(text.as_ref());
        self.out.push('\n');
    }

    fn section<'a>(&mut self, result: &'a Value, key: &str, title: &str) -> Option<&'a Value> {
        let section = &result[key];
        self.line("");
        self.line(format!("  {}", self.paint("38;5;75;1", title)));
        self.line(format!(
            "  {}",
            self.paint("38;5;240", &"─".repeat(title.len()))
        ));
        if let Some(fetched) = section["fetchedAt"].as_str() {
            self.line(format!("  Fetched at {fetched}"));
        }
        if let Some(endpoint) = section["source"]["endpoint"].as_str() {
            self.line(format!("  Source {endpoint}"));
        }
        if let Some(warning) = section["warning"].as_str() {
            self.line(format!("  {warning}"));
        }
        if let Some(membership) = section.get("membership") {
            if membership["sourceBlock"].is_null() {
                self.line("  Live position membership unavailable");
            } else {
                self.line(format!(
                    "  Live position membership {}. Block {}, {}",
                    text(&membership["status"]),
                    text(&membership["sourceBlock"]),
                    source(membership)
                ));
            }
        }
        if let Some(error) = section.get("error") {
            self.line(format!(
                "  {} ({})",
                text(&error["message"]),
                text(&error["code"])
            ));
        }
        section.get("data")
    }

    fn paint(&self, style: &str, text: &str) -> String {
        if self.color {
            let style = style.strip_suffix(";1").map_or_else(
                || format!("\x1b[{style}m"),
                |color| format!("\x1b[{color}m\x1b[1m"),
            );
            format!("{style}{text}\x1b[0m")
        } else {
            text.into()
        }
    }

    fn table(&mut self, headers: &[&str], rows: impl IntoIterator<Item = Vec<String>>) {
        let rows: Vec<_> = rows.into_iter().collect();
        if rows.is_empty() {
            self.line("  None");
            return;
        }
        let widths: Vec<_> = headers
            .iter()
            .enumerate()
            .map(|(i, header)| {
                rows.iter()
                    .map(|row| row[i].chars().count())
                    .max()
                    .unwrap_or(0)
                    .max(header.len())
                    .min(72)
            })
            .collect();
        let separator = self.paint("38;5;240", " │ ");
        let format_cells = |cells: &[&str], style| {
            cells
                .iter()
                .zip(&widths)
                .map(|(cell, width)| {
                    let part: String = cell.chars().take(*width).collect();
                    self.paint(style, &format!("{part:<width$}"))
                })
                .collect::<Vec<_>>()
                .join(&separator)
        };
        let mut lines = vec![format!("  {}", format_cells(headers, "38;5;80;1"))];
        let rule = widths
            .iter()
            .map(|width| "─".repeat(*width))
            .collect::<Vec<_>>()
            .join("───");
        lines.push(self.paint("38;5;240", &format!("  {rule}")));
        for row in rows {
            let cells: Vec<_> = row.iter().map(String::as_str).collect();
            lines.push(format!("  {}", format_cells(&cells, "38;5;250")));
        }
        for line in lines {
            self.line(line);
        }
    }

    fn positions(&mut self, positions: &Value) {
        let rows = items(&positions["items"]);
        self.table(
            &[
                "ID",
                "Market",
                "Side",
                "Size USD",
                "Collateral USD",
                "Leverage",
                "Gross PnL USD",
                "Gross PnL %",
            ],
            rows.iter().map(|row| {
                vec![
                    text(&row["id"]),
                    market(row),
                    text(&row["side"]),
                    number(&row["sizeUsd"], ""),
                    collateral(row),
                    number(&row["leverage"], "x"),
                    number(&row["unrealizedPnlUsd"], ""),
                    number(&row["unrealizedPnlPercent"], "%"),
                ]
            }),
        );
        if rows.is_empty() {
            return;
        }
        self.table(
            &[
                "ID",
                "Entry USD",
                "Mark USD",
                "Liquidation USD (%)",
                "TP USD (%)",
                "SL USD (%)",
                "Trade Source (Age)",
            ],
            rows.iter().map(|row| {
                vec![
                    text(&row["id"]),
                    number(&row["entryPriceUsd"], ""),
                    number(&row["markPriceUsd"], ""),
                    trigger(row, "liquidation"),
                    trigger(row, "takeProfit"),
                    trigger(row, "stopLoss"),
                    source(row),
                ]
            }),
        );
        for row in rows {
            let id = text(&row["id"]);
            if let Some(risk) = row.get("riskSource") {
                self.line(format!(
                    "  Trade {id} contract risk {}. Fetched at {}",
                    source(risk),
                    text(&risk["fetchedAt"])
                ));
                if row["liquidationPriceUsd"].is_null() {
                    self.line(format!(
                        "    Contract liquidation price {} {}",
                        number(&row["liquidationPriceQuote"], ""),
                        text(&row["quoteToken"])
                    ));
                }
            }
            if let Some(mark) = row.get("markSource") {
                let block = if mark["sourceBlock"].is_null() {
                    UNKNOWN.into()
                } else {
                    text(&mark["sourceBlock"])
                };
                self.line(format!("  Trade {id} oracle mark {}. Publish block {block}. Read at block {}, fetched at {}",
                    source(mark), text(&mark["queryBlock"]), text(&mark["fetchedAt"])));
                if row["quoteTokenId"] != 0 {
                    self.line(format!(
                        "    Base price {}. Quote price {}",
                        source(&mark["base"]),
                        source(&mark["quote"])
                    ));
                }
            }
            if let Some(collateral) = row.get("collateralPriceSource") {
                self.line(format!(
                    "  Trade {id} pinned collateral price {}",
                    source(collateral)
                ));
            }
            if row["riskStatus"] != "verified" {
                self.line(format!(
                    "  Trade {id} risk {}. {}",
                    text(&row["riskStatus"]),
                    text(&row["riskReason"])
                ));
                if let Some(estimate) = row.get("keeperLiquidationEstimatePriceQuote") {
                    self.line(format!(
                        "    Keeper liquidation estimate {} {}. Source time unknown.",
                        number(estimate, ""),
                        text(&row["quoteToken"])
                    ));
                }
            }
        }
        self.line(format!(
            "  Total gross unrealized PnL before fees {}",
            number(&positions["unrealizedPnlUsd"], " USD")
        ));
    }
}

fn items(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}

fn text(value: &Value) -> String {
    if value.is_null() {
        "None".into()
    } else {
        value
            .as_str()
            .map_or_else(|| value.to_string(), str::to_owned)
    }
}

fn source(row: &Value) -> String {
    if let Some(at) = row["sourceAt"].as_str() {
        format!("{at} ({})", number(&row["sourceAgeSeconds"], " s"))
    } else if !row["sourceBlock"].is_null() {
        format!("block {} (time unknown)", text(&row["sourceBlock"]))
    } else {
        UNKNOWN.into()
    }
}

fn market(row: &Value) -> String {
    row["market"]
        .as_str()
        .filter(|symbol| !symbol.is_empty())
        .map_or_else(|| text(&row["marketId"]), str::to_owned)
}

fn collateral(row: &Value) -> String {
    if row["collateralUsd"].is_null() {
        format!(
            "{} {} base units",
            text(&row["collateralBaseUnits"]),
            text(&row["collateralToken"])
        )
    } else {
        number(&row["collateralUsd"], "")
    }
}

fn trigger(row: &Value, prefix: &str) -> String {
    let price = &row[format!("{prefix}PriceUsd")];
    if prefix == "liquidation"
        && let Some(warning) = row["liquidationWarning"].as_str()
    {
        format!("{} {warning}", number(price, ""))
    } else if price.is_null() {
        "-".into()
    } else {
        format!(
            "{} ({})",
            number(price, ""),
            number(&row[format!("{prefix}DistancePercent")], "%")
        )
    }
}

// Match Python's comma grouping and eight significant digits for overview values.
fn number(value: &Value, suffix: &str) -> String {
    let Some(value) = value.as_f64() else {
        return UNKNOWN.into();
    };
    let scientific = format!("{value:.7e}");
    let (mantissa, exponent) = scientific.split_once('e').expect("scientific notation");
    let exponent: i32 = exponent.parse().expect("valid decimal exponent");
    let trim = |text: String| {
        if text.contains('.') {
            text.trim_end_matches('0').trim_end_matches('.').to_owned()
        } else {
            text
        }
    };
    if !(-4..8).contains(&exponent) {
        return format!("{}e{exponent:+03}{suffix}", trim(mantissa.into()));
    }
    let fixed = trim(format!(
        "{value:.precision$}",
        precision = (7 - exponent) as usize
    ));
    let (sign, unsigned) = fixed
        .strip_prefix('-')
        .map_or(("", fixed.as_str()), |rest| ("-", rest));
    let (whole, fraction) = unsigned
        .split_once('.')
        .map_or((unsigned, ""), |(whole, fraction)| (whole, fraction));
    let mut grouped = sign.to_owned();
    for (index, digit) in whole.chars().enumerate() {
        if index != 0 && (whole.len() - index).is_multiple_of(3) {
            grouped.push(',');
        }
        grouped.push(digit);
    }
    if !fraction.is_empty() {
        grouped.push('.');
        grouped.push_str(fraction);
    }
    grouped.push_str(suffix);
    grouped
}
