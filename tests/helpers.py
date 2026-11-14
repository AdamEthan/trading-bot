"""Fake clock and fake exchange so engine tests run in milliseconds."""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.data.models import Bar, Quote  # noqa: E402


class FakeClock:
    def __init__(self, start: float = 1_794_600_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def wave_price(t: float, base: float = 60_000.0, amp: float = 600.0, period: float = 3_600.0) -> float:
    return round(base + amp * math.sin(2 * math.pi * t / period), 2)


class FakeFeed:
    """Deterministic market: a smooth sine wave. Candles are built from it."""

    name = "fake"

    def __init__(self, clock: FakeClock, price_fn=wave_price, fail_quotes: bool = False):
        self.clock = clock
        self.price_fn = price_fn
        self.fail_quotes = fail_quotes

    def _bar(self, start: int, seconds: int) -> Bar:
        samples = [self.price_fn(start + s) for s in range(0, seconds + 1, max(1, seconds // 6))]
        return Bar(start, seconds, samples[0], max(samples), min(samples), samples[-1], 1.0)

    def candles(self, seconds: int, start=None, end=None):
        now = int(self.clock())
        current = now // seconds * seconds
        first = current - 299 * seconds if start is None else int(start) // seconds * seconds
        last = current if end is None else min(current, int(end))
        return [self._bar(s, seconds) for s in range(first, last + 1, seconds)]

    def history(self, seconds, start, end):
        return [self._bar(s, seconds) for s in range(int(start) // seconds * seconds, int(end), seconds)
                if start <= s < end]

    def quote(self) -> Quote:
        from src.data.http import DataError
        if self.fail_quotes:
            raise DataError("simulated outage")
        return Quote(self.price_fn(self.clock()), self.clock(), self.name)


class NoNews:
    enabled = False
    error = ""

    def refresh(self, force=False):
        pass

    def blackout(self, now):
        return None

    def upcoming(self, now, hours=24):
        return []
