use std::{
    collections::{BTreeMap, VecDeque},
    fs::File,
    io::{self, BufRead, BufReader, Read, Seek, SeekFrom},
    path::Path,
};

use chrono::NaiveDate;
use serde_json::Value;

use super::{Row, day, decode_row, order};
use crate::log::{
    MAX_LINE, Surface,
    files::{LogDir, parse_name},
};

const CHUNK: usize = 64 * 1024;
const SURFACES: usize = 3;

pub(super) fn files(directory: &LogDir) -> BTreeMap<NaiveDate, [Option<String>; SURFACES]> {
    let mut days = BTreeMap::<NaiveDate, [Option<String>; SURFACES]>::new();
    for name in directory.names().unwrap_or_default() {
        if let Some((day, slot)) = parse_name(&name) {
            days.entry(day).or_default()[slot] = Some(name);
        }
    }
    days
}

#[derive(Clone, Copy)]
pub(super) struct Position {
    pub day: NaiveDate,
    pub offsets: Option<[u64; SURFACES]>,
}

#[derive(Clone, Copy, Eq, Ord, PartialEq, PartialOrd)]
pub(super) struct Line {
    day: NaiveDate,
    surface: usize,
    offset: u64,
}

impl Line {
    pub fn parse(value: &str) -> Option<Self> {
        let (date, position) = value.split_once(':')?;
        let (surface, offset) = position.split_once(':')?;
        let surface = surface.parse().ok()?;
        (surface < SURFACES).then_some(Self {
            day: day(date)?,
            surface,
            offset: offset.parse().ok()?,
        })
    }
}

impl std::fmt::Display for Line {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}:{}:{}", self.day, self.surface, self.offset)
    }
}

#[derive(Clone, Copy)]
pub(super) struct AnchorRange {
    first: Line,
    last: u64,
}

impl AnchorRange {
    pub fn parse(value: &str) -> Option<Self> {
        let (line, last) = value.rsplit_once(':')?;
        if let Some(first) = Line::parse(line) {
            let last = last.parse().ok()?;
            (first.offset <= last).then_some(Self { first, last })
        } else {
            Some(Line::parse(value)?.into())
        }
    }

    pub fn cover(mut lines: Vec<Line>) -> Vec<Self> {
        lines.sort_unstable();
        let mut ranges = Vec::<Self>::new();
        for line in lines {
            // Seek past sparse gaps. A merged range costs no more to read
            // than the worst-case size of the individual lines.
            if let Some(range) = ranges.last_mut()
                && line.day == range.first.day
                && line.surface == range.first.surface
                && line.offset - range.last <= (MAX_LINE + 1) as u64
            {
                range.last = line.offset;
            } else {
                ranges.push(line.into());
            }
        }
        ranges
    }

    fn read_size(self) -> u64 {
        (self.last - self.first.offset).saturating_add((MAX_LINE + 1) as u64)
    }

    pub fn fits_replay_budget(ranges: &[Self]) -> bool {
        ranges
            .iter()
            .map(|range| range.read_size())
            .fold(0, u64::saturating_add)
            <= 64 * 1024 * 1024
    }
}

impl From<Line> for AnchorRange {
    fn from(first: Line) -> Self {
        Self {
            first,
            last: first.offset,
        }
    }
}

impl std::fmt::Display for AnchorRange {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.first)?;
        if self.first.offset != self.last {
            write!(f, ":{}", self.last)?;
        }
        Ok(())
    }
}

pub(super) fn read_anchors(
    root: &Path,
    ranges: &[AnchorRange],
    accept: impl Fn(&Row) -> bool,
) -> Vec<(Line, Row)> {
    let Ok(directory) = LogDir::read_only(root) else {
        return Vec::new();
    };
    let mut rows = Vec::new();
    for &range in ranges {
        let _ = read_anchor_range(&directory, range, &accept, &mut rows);
    }
    rows
}

fn read_anchor_range(
    directory: &LogDir,
    range: AnchorRange,
    accept: &impl Fn(&Row) -> bool,
    rows: &mut Vec<(Line, Row)>,
) -> io::Result<()> {
    let mut line = range.first;
    let surface = Surface::ALL[line.surface].as_str();
    let mut file = directory.read_file(&format!("{surface}-{}.jsonl", line.day))?;
    if line.offset != 0 {
        file.seek(SeekFrom::Start(line.offset - 1))?;
        let mut preceding = [0];
        file.read_exact(&mut preceding)?;
        if preceding != *b"\n" {
            return Ok(());
        }
    }

    // Last is the byte offset of the last remembered line, not the current EOF.
    let line_limit = (MAX_LINE + 1) as u64;
    let mut reader = BufReader::new(file).take(range.read_size());
    let mut raw = Vec::new();
    while line.offset <= range.last {
        raw.clear();
        let size = reader
            .by_ref()
            .take(line_limit)
            .read_until(b'\n', &mut raw)?;
        if size == 0 {
            break;
        }
        let skipped = if let Some(bytes) = raw.strip_suffix(b"\n") {
            if let Some(row) = decode_row(bytes).filter(accept) {
                rows.push((line, row));
            }
            0
        } else {
            reader.skip_until(b'\n')?
        };
        line.offset += size as u64 + skipped as u64;
    }
    Ok(())
}

pub(super) struct Budget {
    bytes: usize,
    rows: usize,
    files: usize,
}

impl Budget {
    pub const PAGE: Self = Self::new(16 * 1024 * 1024, 20_000, 128);
    pub const RUN: Self = Self::new(16 * 1024 * 1024 - 512 * 1024, 20_000 - 512, 128 - 6);
    pub const CONTEXT: Self = Self::new(256 * 1024, 256, 3);

    const fn new(bytes: usize, rows: usize, files: usize) -> Self {
        Self { bytes, rows, files }
    }

    fn read(&mut self, file: &mut File, size: usize) -> Result<Vec<u8>, ReadError> {
        if size > self.bytes {
            return Err(ReadError::Budget);
        }
        let mut data = vec![0; size];
        file.read_exact(&mut data)?;
        self.bytes -= size;
        Ok(data)
    }
}

enum ReadError {
    Budget,
    Io,
}

impl From<io::Error> for ReadError {
    fn from(_: io::Error) -> Self {
        Self::Io
    }
}

struct RawRow {
    start: u64,
    bytes: Vec<u8>,
}

struct Reverse {
    file: File,
    end: u64,
    position: u64,
    resume: u64,
    rest: Option<Vec<u8>>,
    max_line: usize,
    checked_end: bool,
}

impl Reverse {
    fn new(file: File, limit: Option<u64>, max_line: usize) -> io::Result<Self> {
        let end = file.metadata()?.len();
        let end = limit.map_or(end, |limit| end.min(limit));
        Ok(Self {
            file,
            end,
            position: end,
            resume: end,
            rest: None,
            max_line,
            checked_end: false,
        })
    }

    fn block(&mut self, budget: &mut Budget) -> Result<Option<Vec<RawRow>>, ReadError> {
        if self.position == 0 {
            return Ok(None);
        }
        if !self.checked_end {
            self.file.seek(SeekFrom::Start(self.end - 1))?;
            if budget.read(&mut self.file, 1)? == b"\n" {
                self.rest = Some(Vec::new());
            }
            self.checked_end = true;
        }
        let length = self.position.min(CHUNK as u64) as usize;
        let start = self.position - length as u64;
        self.file.seek(SeekFrom::Start(start))?;
        let data = budget.read(&mut self.file, length)?;
        self.position = start;

        let mut pieces = data.rsplit(|&byte| byte == b'\n').peekable();
        let mut end = start + length as u64;
        let mut block = Vec::new();
        let mut rest = self.rest.take();
        while let Some(piece) = pieces.next() {
            let offset = end - piece.len() as u64;
            if let Some(suffix) = rest.take()
                && piece.len() + suffix.len() <= self.max_line
            {
                let mut raw = piece.to_vec();
                raw.extend(suffix);
                if pieces.peek().is_some() || start == 0 {
                    if !raw.is_empty() {
                        block.push(RawRow {
                            start: offset,
                            bytes: raw,
                        });
                    }
                } else {
                    self.rest = Some(raw);
                }
            }
            if pieces.peek().is_none() {
                if end < start + length as u64 {
                    self.resume = end + 1;
                } else if self.rest.is_none() {
                    self.resume = start;
                }
                break;
            }
            end = offset - 1;
            rest = Some(Vec::new());
        }
        if start == 0 {
            self.resume = 0;
        }
        Ok(Some(block))
    }
}

#[derive(Default)]
pub(super) struct Scan {
    pub start: Option<Position>,
    pub truncated: bool,
    pub resume: Option<Position>,
}

struct Reader {
    reverse: Reverse,
    ready: VecDeque<(u64, u64, Row)>,
    exhausted: bool,
}

impl Reader {
    fn head(
        &mut self,
        budget: &mut Budget,
        window: Window<'_>,
        observe: &mut impl FnMut(u64, &Row),
    ) -> Result<Option<&(u64, u64, Row)>, ReadError> {
        while self.ready.is_empty() && !self.exhausted {
            let Some(block) = self.reverse.block(budget)? else {
                self.exhausted = true;
                break;
            };
            self.ready.extend(block.into_iter().filter_map(|raw| {
                let row = decode_row(&raw.bytes)?;
                observe(raw.start, &row);
                window.includes(&row).then_some((
                    raw.start,
                    raw.start + raw.bytes.len() as u64 + 1,
                    row,
                ))
            }));
        }
        Ok(self.ready.front())
    }

    fn offset(&self) -> u64 {
        self.ready
            .front()
            .map_or(self.reverse.resume, |(_, end, _)| *end)
    }
}

#[derive(Clone, Copy, Default)]
pub(super) struct Window<'a> {
    pub start: Option<Position>,
    pub first_day: Option<NaiveDate>,
    pub last_day: Option<NaiveDate>,
    pub since: Option<&'a str>,
    pub before: Option<&'a str>,
}

impl Window<'_> {
    fn includes(self, row: &Row) -> bool {
        row.get("ts").and_then(Value::as_str).is_some_and(|ts| {
            self.since.is_none_or(|since| ts >= since)
                && self.before.is_none_or(|before| ts < before)
        })
    }
}

pub(super) fn page(
    root: &Path,
    mut budget: Budget,
    window: Window<'_>,
    mut observe: impl FnMut(Line, &Row),
    mut consume: impl FnMut(Position, Row) -> bool,
) -> Scan {
    let mut scan = Scan::default();
    let Ok(directory) = LogDir::read_only(root) else {
        return scan;
    };
    for (day, names) in files(&directory).into_iter().rev() {
        if window.first_day.is_some_and(|first| day < first)
            || window.last_day.is_some_and(|last| day > last)
            || window.start.is_some_and(|start| day > start.day)
        {
            continue;
        }
        let count = names.iter().flatten().count();
        if count > budget.files {
            return Scan {
                truncated: true,
                resume: Some(Position { day, offsets: None }),
                ..scan
            };
        }
        budget.files -= count;
        let limits = window
            .start
            .filter(|start| start.day == day)
            .and_then(|start| start.offsets);
        let mut readers: [Option<Reader>; SURFACES] = [const { None }; SURFACES];
        for (slot, name) in names.into_iter().enumerate().rev() {
            let Some(name) = name else {
                continue;
            };
            match directory
                .read_file(&name)
                .and_then(|file| Reverse::new(file, limits.map(|limits| limits[slot]), MAX_LINE))
            {
                Ok(reverse) => {
                    readers[slot] = Some(Reader {
                        reverse,
                        ready: VecDeque::new(),
                        exhausted: false,
                    })
                }
                Err(_) => scan.truncated = true,
            }
        }
        scan.start.get_or_insert_with(|| Position {
            day,
            offsets: Some(std::array::from_fn(|index| {
                readers[index]
                    .as_ref()
                    .map_or(0, |reader| reader.reverse.end)
            })),
        });
        let position = |readers: &[Option<Reader>; SURFACES]| Position {
            day,
            offsets: Some(std::array::from_fn(|index| {
                readers[index].as_ref().map_or(0, Reader::offset)
            })),
        };
        loop {
            for slot in (0..SURFACES).rev() {
                let Some(reader) = &mut readers[slot] else {
                    continue;
                };
                let mut observe_line = |offset, row: &Row| {
                    observe(
                        Line {
                            day,
                            surface: slot,
                            offset,
                        },
                        row,
                    );
                };
                match reader.head(&mut budget, window, &mut observe_line) {
                    Ok(_) => {}
                    Err(ReadError::Budget) => {
                        return Scan {
                            truncated: true,
                            resume: Some(position(&readers)),
                            ..scan
                        };
                    }
                    Err(ReadError::Io) => {
                        scan.truncated = true;
                        readers[slot] = None;
                    }
                }
            }
            let best = readers
                .iter()
                .enumerate()
                .filter_map(|(slot, reader)| {
                    reader
                        .as_ref()?
                        .ready
                        .front()
                        .map(|(_, _, row)| (order(row), slot))
                })
                .max();
            let Some((_, slot)) = best else {
                break;
            };
            let checkpoint = position(&readers);
            if budget.rows == 0 {
                return Scan {
                    truncated: true,
                    resume: Some(checkpoint),
                    ..scan
                };
            }
            budget.rows -= 1;
            let (_, _, row) = readers[slot].as_mut().unwrap().ready.pop_front().unwrap();
            if !consume(checkpoint, row) {
                return scan;
            }
        }
    }
    scan
}

pub(super) struct Context {
    pub rows: Vec<(Line, Row)>,
    pub truncated: bool,
}

pub(super) fn prefix(root: &Path, day: NaiveDate, accept: impl Fn(&Row) -> bool) -> Context {
    let mut result = Context {
        rows: Vec::new(),
        truncated: false,
    };
    let Ok(directory) = LogDir::read_only(root) else {
        return result;
    };
    let Some(names) = files(&directory).remove(&day) else {
        return result;
    };
    let mut budget = Budget::CONTEXT;
    let count = names.iter().flatten().count();
    if count == 0 {
        return result;
    }
    let max = (budget.bytes / count) as u64;
    for (surface, name) in names.into_iter().enumerate().rev() {
        let Some(name) = name else { continue };
        let Ok((mut file, size)) = directory
            .read_file(&name)
            .and_then(|file| file.metadata().map(|metadata| (file, metadata.len())))
        else {
            result.truncated = true;
            continue;
        };
        let length = size.min(max);
        result.truncated |= length != size;
        let Ok(data) = budget.read(&mut file, length as usize) else {
            result.truncated = true;
            continue;
        };
        let mut next_offset = 0;
        for raw in data.split_inclusive(|&byte| byte == b'\n') {
            let offset = next_offset;
            next_offset += raw.len() as u64;
            let Some(row) = raw
                .strip_suffix(b"\n")
                .filter(|row| row.len() <= MAX_LINE)
                .and_then(decode_row)
                .filter(&accept)
            else {
                continue;
            };
            if budget.rows == 0 {
                result.truncated = true;
                return result;
            }
            budget.rows -= 1;
            result.rows.push((
                Line {
                    day,
                    surface,
                    offset,
                },
                row,
            ));
        }
    }
    result
}

pub(super) fn tail(
    directory: &LogDir,
    name: &str,
    count: usize,
    accept: impl Fn(&Row) -> bool,
) -> Vec<Row> {
    let mut result = Vec::new();
    let Ok(mut reader) = directory
        .read_file(name)
        .and_then(|file| Reverse::new(file, None, usize::MAX))
    else {
        return result;
    };
    // Tail accepts a final record without a newline, as the Python CLI does.
    reader.checked_end = true;
    reader.rest = Some(Vec::new());
    let mut budget = Budget::new(usize::MAX, 0, 0);
    while let Ok(Some(block)) = reader.block(&mut budget) {
        for row in block
            .into_iter()
            .filter_map(|row| decode_row(&row.bytes))
            .filter(&accept)
        {
            result.push(row);
            if result.len() >= count {
                return result;
            }
        }
    }
    result
}
