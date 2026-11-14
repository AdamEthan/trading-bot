"""Pick a feed by name and decide which candles are final."""
from __future__ import annotations

from .coinbase import CoinbaseFeed
from .kraken import KrakenFeed
from .models import Bar

FEEDS = {"coinbase": CoinbaseFeed, "kraken": KrakenFeed}
BAR_SIZES = (60, 300, 900, 3600)

# Exchanges keep updating a candle for a few seconds after it ends.
SETTLE_SECONDS = 10


def make_feed(source: str, symbol: str):
    try:
        return FEEDS[source](symbol)
    except KeyError:
        raise ValueError(f"Unknown data source {source!r}. Use one of: {', '.join(FEEDS)}") from None


def closed_bars(bars: list[Bar], now: float) -> list[Bar]:
    return [b for b in bars if b.end + SETTLE_SECONDS <= now]
