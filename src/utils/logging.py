"""Structured logs for orders, fills, and decisions.

Every run gets its own folder under logs/ (git-ignored):

  run.json      config, strategy, and code fingerprint for this run
  events.jsonl  one JSON line per decision: signals, orders, refusals, errors
  trades.csv    every paper fill, with the exchange price and time it used
  bars.csv      every closed candle the strategy saw, as downloaded
  equity.csv    account value after each bar
  status.json   the live snapshot the dashboard reads
  summary.json  written when the run ends
  bot.log       the same lines printed to the terminal

`python run.py verify logs/<run>` rechecks all of it against the exchange.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from pathlib import Path

from ..brokers.base import FILL_FIELDS, Fill
from ..data.models import Bar, utc_iso

BAR_FIELDS = ["start", "start_utc", "open", "high", "low", "close", "volume"]
EQUITY_FIELDS = ["time", "price", "cash", "position", "equity", "day_pnl"]


def console_logger(run_dir: Path | None = None) -> logging.Logger:
    log = logging.getLogger("bot")
    log.setLevel(logging.INFO)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    log.addHandler(out)
    if run_dir is not None:
        file = logging.FileHandler(run_dir / "bot.log", encoding="utf-8")
        file.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
        log.addHandler(file)
    return log


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: the dashboard is reading the old file this instant
            if attempt == 49:
                raise
            time.sleep(0.02)


class Journal:
    def __init__(self, run_dir: Path, clock):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._csv_headers(self.dir / "trades.csv", FILL_FIELDS)
        self._csv_headers(self.dir / "bars.csv", BAR_FIELDS)
        self._csv_headers(self.dir / "equity.csv", EQUITY_FIELDS)

    @staticmethod
    def _csv_headers(path: Path, fields: list[str]) -> None:
        if not path.exists():
            with path.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(fields)

    def _append_csv(self, name: str, fields: list[str], row: dict) -> None:
        with (self.dir / name).open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=fields).writerow(row)

    def event(self, kind: str, **data) -> dict:
        record = {"time": utc_iso(self._clock()), "kind": kind, **data}
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return record

    def fill(self, fill: Fill) -> None:
        self._append_csv("trades.csv", FILL_FIELDS, fill.row())

    def bar(self, bar: Bar) -> None:
        self._append_csv("bars.csv", BAR_FIELDS, {
            "start": bar.start, "start_utc": utc_iso(bar.start), "open": bar.open, "high": bar.high,
            "low": bar.low, "close": bar.close, "volume": bar.volume})

    def equity(self, *, now: float, price: float, cash: float, position: float, equity: float, day_pnl: float) -> None:
        self._append_csv("equity.csv", EQUITY_FIELDS, {
            "time": utc_iso(now), "price": price, "cash": round(cash, 8), "position": position,
            "equity": round(equity, 8), "day_pnl": round(day_pnl, 8)})

    def write(self, name: str, data) -> None:
        write_json_atomic(self.dir / name, data)
