"""Coinbase Exchange public market data. No account or API key needed.

Docs: https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles
Public endpoints allow roughly 10 requests per second; the bot uses well under 1.

Always send an explicit start/end: the bare candles URL is served from a cache
that can be several minutes old, while a ranged request returns current data.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from .http import DataError, get_json
from .models import Bar, Quote

BASE_URL = "https://api.exchange.coinbase.com"
GRANULARITIES = (60, 300, 900, 3600)
MAX_CANDLES = 300


def parse_iso(text: str) -> float:
    """Parse Coinbase timestamps such as 2026-11-14T20:32:42.260710745Z."""
    match = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$", text.strip())
    if not match:
        raise DataError(f"Unrecognised timestamp {text!r}")
    base, fraction, zone = match.groups()
    stamp = datetime.fromisoformat(base).replace(tzinfo=timezone.utc)
    if zone and zone != "Z":
        stamp = datetime.fromisoformat(base + zone)
    return stamp.timestamp() + (float(f"0.{fraction}") if fraction else 0.0)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CoinbaseFeed:
    name = "coinbase"

    def __init__(self, symbol: str = "BTC-USD", fetch=get_json, clock=time.time):
        self.symbol = symbol.upper()
        self._fetch = fetch
        self._clock = clock

    def candles(self, seconds: int, start: float | None = None, end: float | None = None) -> list[Bar]:
        """Candles oldest first. The newest one may still be forming."""
        if seconds not in GRANULARITIES:
            raise ValueError(f"Coinbase bar size must be one of {GRANULARITIES}")
        if start is None or end is None:
            end = self._clock() + seconds
            start = end - seconds * (MAX_CANDLES - 1)
        params = {"granularity": seconds, "start": _iso(start), "end": _iso(end)}
        rows = self._fetch(f"{BASE_URL}/products/{self.symbol}/candles", params)
        if not isinstance(rows, list):
            raise DataError(f"Unexpected candle payload from Coinbase: {str(rows)[:200]}")
        # Each row is [time, low, high, open, close, volume], newest first.
        bars = [
            Bar(start=int(r[0]), seconds=seconds, open=float(r[3]), high=float(r[2]),
                low=float(r[1]), close=float(r[4]), volume=float(r[5]))
            for r in rows
        ]
        return sorted(bars, key=lambda b: b.start)

    def history(self, seconds: int, start: float, end: float) -> list[Bar]:
        """All candles between start and end, paging past the 300-candle limit."""
        out: dict[int, Bar] = {}
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + seconds * MAX_CANDLES)
            for bar in self.candles(seconds, cursor, chunk_end):
                out[bar.start] = bar
            cursor = chunk_end
        return [out[k] for k in sorted(out) if start <= k < end]

    def quote(self) -> Quote:
        data = self._fetch(f"{BASE_URL}/products/{self.symbol}/ticker")
        try:
            return Quote(
                price=float(data["price"]), time=parse_iso(data["time"]), source=self.name,
                bid=float(data["bid"]) if data.get("bid") else None,
                ask=float(data["ask"]) if data.get("ask") else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataError(f"Unexpected ticker payload from Coinbase: {str(data)[:200]}") from exc
