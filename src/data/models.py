"""Plain data records shared by feeds, strategies, and logs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def utc_iso(ts: float) -> str:
    """Unix seconds -> ISO-8601 UTC string with a Z suffix, to the second."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Bar:
    """One OHLCV candle. `start` is the open time in Unix seconds (UTC)."""

    start: int
    seconds: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def end(self) -> int:
        return self.start + self.seconds


@dataclass(frozen=True)
class Quote:
    """Latest traded price from a feed. `time` is Unix seconds."""

    price: float
    time: float
    source: str
    bid: float | None = None
    ask: float | None = None
