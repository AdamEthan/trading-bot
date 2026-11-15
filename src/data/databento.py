"""Read Databento OHLCV downloads (CSV) for CME futures.

Accepts the batch download as Databento delivers it: the .zip, the
.csv.zst inside it, or a plain .csv. Binary DBN files are not supported;
request CSV encoding when downloading.

Rows look like:
  ts_event,rtype,publisher_id,instrument_id,open,high,low,close,volume,symbol
  2026-10-15T00:00:00.000000000Z,32,1,42005282,29908.25,...,129,MNQZ6

`ts_event` is the start of the candle. A one-second file only has a row for
seconds in which something traded.

Futures roll, so a "parent" download (MNQ.FUT) mixes several contract months
and calendar spreads. `plan_contracts` picks one outright per CME trade date
(the session that opens at 17:00 Chicago time the evening before): the
contract that traded the most on the previous trade date. That only uses
information known before the session starts, so there is no look-ahead.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
SCHEMA_SECONDS = {"ohlcv-1s": 1, "ohlcv-1m": 60, "ohlcv-1h": 3600, "ohlcv-1d": 86400}
OUTRIGHT = re.compile(r"^[A-Z0-9]+[FGHJKMNQUVXZ]\d{1,2}$")


class DataFileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Row:
    ts: int          # candle start, Unix seconds UTC
    open: float
    high: float
    low: float
    close: float
    volume: int
    symbol: str


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_outright(symbol: str) -> bool:
    """MNQZ6 yes; MNQZ6-MNQH7 (a calendar spread) no."""
    return "-" not in symbol and bool(OUTRIGHT.match(symbol))


def _csv_member(zf: zipfile.ZipFile) -> str:
    names = [n for n in zf.namelist() if n.endswith((".csv", ".csv.zst"))]
    if not names:
        dbn = [n for n in zf.namelist() if ".dbn" in n]
        hint = " It holds DBN files; download again with CSV encoding." if dbn else ""
        raise DataFileError(f"No .csv or .csv.zst file inside the zip.{hint}")
    if len(names) > 1:
        raise DataFileError(f"The zip holds several data files ({', '.join(names)}); unzip it and pass one.")
    return names[0]


def _zstd_text(raw) -> io.TextIOBase:
    try:
        import zstandard
    except ModuleNotFoundError:
        raise DataFileError("Reading .zst files needs the zstandard package:  pip install -r requirements.txt") from None
    return io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(raw), encoding="utf-8", newline="")


def open_text(path: Path) -> io.TextIOBase:
    """A text stream over the CSV, whatever it is wrapped in. Caller closes it."""
    path = Path(path)
    if not path.is_file():
        raise DataFileError(f"Data file not found: {path}")
    name = path.name.lower()
    if name.endswith(".zip"):
        zf = zipfile.ZipFile(path)
        member = _csv_member(zf)
        raw = zf.open(member)
        return _zstd_text(raw) if member.endswith(".zst") else io.TextIOWrapper(raw, encoding="utf-8", newline="")
    if ".dbn" in name:
        raise DataFileError("DBN (binary) files are not supported; download again with CSV encoding.")
    if name.endswith(".zst"):
        return _zstd_text(open(path, "rb"))
    if name.endswith(".csv"):
        return open(path, encoding="utf-8", newline="")
    raise DataFileError(f"Unrecognised data file type: {path.name} (expected .zip, .csv.zst or .csv)")


def read_metadata(path: Path) -> dict:
    """metadata.json from a Databento zip, or {} when there is none."""
    path = Path(path)
    if path.name.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            for n in zf.namelist():
                if n.endswith("metadata.json"):
                    return json.loads(zf.read(n))
    side = path.parent / "metadata.json"
    if side.is_file():
        return json.loads(side.read_text(encoding="utf-8"))
    return {}


def source_seconds(path: Path) -> int:
    """Candle size of the file: from metadata.json, else from the file name, else 1."""
    schema = (read_metadata(path).get("query") or {}).get("schema", "")
    if schema in SCHEMA_SECONDS:
        return SCHEMA_SECONDS[schema]
    if schema:
        raise DataFileError(f"Schema {schema!r} is not OHLCV candles; download ohlcv-1s or ohlcv-1m.")
    for key, seconds in SCHEMA_SECONDS.items():
        if key in Path(path).name:
            return seconds
    return 1


class _TsParser:
    """Fast ISO-8601 -> Unix seconds for Databento's pretty timestamps."""

    def __init__(self):
        self.days: dict[str, int] = {}

    def __call__(self, text: str) -> int:
        if text.isdigit():  # pretty_ts off: integer nanoseconds
            return int(text) // 1_000_000_000
        day = text[:10]
        base = self.days.get(day)
        if base is None:
            base = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
            self.days[day] = base
        return base + int(text[11:13]) * 3600 + int(text[14:16]) * 60 + int(text[17:19])


def _price(text: str) -> float:
    if "." in text:
        return float(text)
    return int(text) / 1e9  # pretty_px off: fixed-point integer, 1e-9 units


def iter_rows(path: Path, symbols: set[str] | None = None) -> Iterator[Row]:
    """Every row in file order (time order), optionally only these symbols."""
    parse_ts = _TsParser()
    with open_text(path) as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise DataFileError(f"{Path(path).name} is empty") from None
        need = ("ts_event", "open", "high", "low", "close", "volume", "symbol")
        missing = [c for c in need if c not in header]
        if missing:
            raise DataFileError(f"{Path(path).name} is missing column(s) {', '.join(missing)}. "
                                "Download OHLCV as CSV with map_symbols on.")
        i = {c: header.index(c) for c in need}
        its, io_, ih, il, ic, iv, isym = (i[c] for c in need)
        for line_no, p in enumerate(reader, start=2):
            if not p:
                continue
            sym = p[isym]
            if symbols is not None and sym not in symbols:
                continue
            try:
                yield Row(parse_ts(p[its]), _price(p[io_]), _price(p[ih]), _price(p[il]), _price(p[ic]),
                          int(p[iv]), sym)
            except (ValueError, IndexError) as exc:
                raise DataFileError(f"{Path(path).name} line {line_no}: {exc}") from None


def trade_date(ts: int) -> date:
    """CME trade date: the session opening at 17:00 Chicago time belongs to the next day."""
    local = datetime.fromtimestamp(ts, CHICAGO)
    return (local + timedelta(days=1)).date() if local.hour >= 17 else local.date()


class TradeDates:
    """trade_date() with a per-hour cache; time-zone maths per row is slow."""

    def __init__(self):
        self.cache: dict[int, date] = {}

    def __call__(self, ts: int) -> date:
        hour = ts // 3600
        d = self.cache.get(hour)
        if d is None:
            d = self.cache[hour] = trade_date(hour * 3600)
        return d


@dataclass
class ContractPlan:
    """Which outright to trade on each trade date, and why."""

    by_date: dict[date, str]
    volumes: dict[date, dict[str, int]]
    rows: int
    first_ts: int | None
    last_ts: int | None
    skipped_symbols: dict[str, int]

    def symbol_for(self, d: date) -> str | None:
        return self.by_date.get(d)

    def to_dict(self) -> dict:
        return {
            "by_trade_date": {d.isoformat(): s for d, s in sorted(self.by_date.items())},
            "volume_by_trade_date": {d.isoformat(): dict(sorted(v.items(), key=lambda kv: -kv[1]))
                                     for d, v in sorted(self.volumes.items())},
            "rows": self.rows, "skipped_spread_rows": self.skipped_symbols,
        }


def plan_contracts(path: Path, root: str, only: str | None = None) -> ContractPlan:
    """First pass over the file: volume per outright per trade date, then pick the front month.

    `only` forces one contract (e.g. MNQZ6) for the whole file.
    """
    td = TradeDates()
    volumes: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    skipped: dict[str, int] = defaultdict(int)
    rows, first, last = 0, None, None
    for r in iter_rows(path):
        rows += 1
        first = r.ts if first is None else first
        last = r.ts
        if not is_outright(r.symbol) or not r.symbol.startswith(root):
            skipped[r.symbol] += 1
            continue
        volumes[td(r.ts)][r.symbol] += r.volume
    if not volumes:
        raise DataFileError(f"No {root} outright contracts found in {Path(path).name}")
    dates = sorted(volumes)
    by_date: dict[date, str] = {}
    for k, d in enumerate(dates):
        if only:
            by_date[d] = only
            continue
        basis = volumes[dates[k - 1]] if k else volumes[d]  # first day: nothing earlier to go on
        by_date[d] = max(basis.items(), key=lambda kv: kv[1])[0]
    if only and not any(only in v for v in volumes.values()):
        raise DataFileError(f"Contract {only} is not in the file. Found: "
                            f"{', '.join(sorted({s for v in volumes.values() for s in v}))}")
    return ContractPlan(by_date, {d: dict(v) for d, v in volumes.items()}, rows, first, last, dict(skipped))
