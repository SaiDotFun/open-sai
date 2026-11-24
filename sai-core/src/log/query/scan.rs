use std::{
    collections::{BTreeMap, VecDeque},
    fs::File,
    io::{self, Read, Seek, SeekFrom},
    path::Path,
};

use chrono::NaiveDate;
use serde_json::Value;

use super::{Row, decode_row, order};
use crate::log::{
    MAX_LINE,
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
        observe: &mut impl FnMut(&Row),
    ) -> Result<Option<&(u64, u64, Row)>, ReadError> {
        while self.ready.is_empty() && !self.exhausted {
            let Some(block) = self.reverse.block(budget)? else {
                self.exhausted = true;
                break;
            };
            self.ready.extend(block.into_iter().filter_map(|raw| {
                let row = decode_row(&raw.bytes)?;
                observe(&row);
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
    mut observe: impl FnMut(&Row),
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
                match reader.head(&mut budget, window, &mut observe) {
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
    pub rows: Vec<Row>,
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
    for name in names.into_iter().rev().flatten() {
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
        for row in data
            .split_inclusive(|&byte| byte == b'\n')
            .filter_map(|row| row.strip_suffix(b"\n"))
            .filter(|row| row.len() <= MAX_LINE)
            .filter_map(decode_row)
            .filter(&accept)
        {
            if budget.rows == 0 {
                result.truncated = true;
                return result;
            }
            budget.rows -= 1;
            result.rows.push(row);
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
