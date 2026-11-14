"""Kraken public market data. No account or API key needed.

Docs: https://docs.kraken.com/api/docs/rest-api/get-ohlc-data
Kraken only returns the most recent 720 candles, so use Coinbase if you need
to verify runs older than about 12 hours at 1-minute bars.
"""
from __future__ import annotations

import time

from .http import DataError, get_json
from .models import Bar, Quote

BASE_URL = "https://api.kraken.com/0/public"
GRANULARITIES = (60, 300, 900, 3600)


def kraken_pair(symbol: str) -> str:
    base, _, quote = symbol.upper().partition("-")
    return ("XBT" if base == "BTC" else base) + (quote or "USD")


class KrakenFeed:
    name = "kraken"

    def __init__(self, symbol: str = "BTC-USD", fetch=get_json, clock=time.time):
        self.symbol = symbol.upper()
        self.pair = kraken_pair(symbol)
        self._fetch = fetch
        self._clock = clock

    def _result(self, payload) -> list:
        if not isinstance(payload, dict) or payload.get("error"):
            raise DataError(f"Kraken error: {str(payload)[:200]}")
        result = payload.get("result", {})
        keys = [k for k in result if k != "last"]
        if not keys:
            raise DataError("Kraken returned no data")
        return result[keys[0]]

    def candles(self, seconds: int, start: float | None = None, end: float | None = None) -> list[Bar]:
        if seconds not in GRANULARITIES:
            raise ValueError(f"Kraken bar size must be one of {GRANULARITIES}")
        params: dict = {"pair": self.pair, "interval": seconds // 60}
        if start is not None:
            params["since"] = int(start) - 1
        rows = self._result(self._fetch(f"{BASE_URL}/OHLC", params))
        # Each row is [time, open, high, low, close, vwap, volume, count], oldest first.
        bars = [
            Bar(start=int(r[0]), seconds=seconds, open=float(r[1]), high=float(r[2]),
                low=float(r[3]), close=float(r[4]), volume=float(r[6]))
            for r in rows
        ]
        if end is not None:
            bars = [b for b in bars if b.start < end]
        return bars

    def history(self, seconds: int, start: float, end: float) -> list[Bar]:
        return [b for b in self.candles(seconds, start, end) if start <= b.start < end]

    def quote(self) -> Quote:
        result = self._result(self._fetch(f"{BASE_URL}/Ticker", {"pair": self.pair}))
        try:
            return Quote(price=float(result["c"][0]), time=self._clock(), source=self.name,
                         bid=float(result["b"][0]), ask=float(result["a"][0]))
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise DataError(f"Unexpected ticker payload from Kraken: {str(result)[:200]}") from exc
