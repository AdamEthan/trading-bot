import json
import unittest
from pathlib import Path

from tests.helpers import ROOT  # noqa: F401  (puts the repo on sys.path)
from src.data.coinbase import CoinbaseFeed, parse_iso
from src.data.feed import closed_bars, make_feed
from src.data.kraken import KrakenFeed, kraken_pair
from src.data.models import Bar

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIX / name).read_text())


class CoinbaseTests(unittest.TestCase):
    def test_candles_are_sorted_and_mapped(self):
        raw = fixture("coinbase_candles.json")
        feed = CoinbaseFeed(fetch=lambda url, params=None: raw)
        bars = feed.candles(60)
        self.assertEqual([b.start for b in bars], sorted(r[0] for r in raw))
        newest = max(raw, key=lambda r: r[0])
        bar = bars[-1]
        # Coinbase rows are [time, low, high, open, close, volume]
        self.assertEqual((bar.low, bar.high, bar.open, bar.close), tuple(newest[1:5]))
        self.assertTrue(all(b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close) for b in bars))

    def test_ticker(self):
        raw = fixture("coinbase_ticker.json")
        quote = CoinbaseFeed(fetch=lambda url, params=None: raw).quote()
        self.assertAlmostEqual(quote.price, float(raw["price"]))
        self.assertGreater(quote.time, 1_700_000_000)

    def test_parse_iso_nanoseconds(self):
        self.assertAlmostEqual(parse_iso("2026-11-14T20:32:42.260710745Z") % 60, 42.26071, places=4)
        self.assertEqual(parse_iso("2026-11-14T20:32:42Z") % 60, 42)

    def test_history_pages_past_300(self):
        calls = []

        def fetch(url, params=None):
            calls.append(params)
            return []

        CoinbaseFeed(fetch=fetch).history(60, 0, 60 * 700)
        self.assertEqual(len(calls), 3)

    def test_latest_candles_request_sends_a_range(self):
        # The bare URL is served from a stale cache; a ranged request is current.
        seen = {}
        CoinbaseFeed(fetch=lambda url, params=None: seen.update(params) or [], clock=lambda: 1_794_689_236).candles(60)
        self.assertIn("start", seen)
        self.assertEqual(seen["end"], "2026-11-14T20:48:16Z")

    def test_rejects_unsupported_bar_size(self):
        with self.assertRaises(ValueError):
            CoinbaseFeed(fetch=lambda *a, **k: []).candles(120)


class KrakenTests(unittest.TestCase):
    def test_pair(self):
        self.assertEqual(kraken_pair("BTC-USD"), "XBTUSD")
        self.assertEqual(kraken_pair("ETH-USD"), "ETHUSD")

    def test_candles(self):
        raw = fixture("kraken_ohlc.json")
        bars = KrakenFeed(fetch=lambda url, params=None: raw).candles(60)
        rows = raw["result"]["XXBTZUSD"]
        self.assertEqual(len(bars), len(rows))
        # Kraken rows are [time, open, high, low, close, vwap, volume, count]
        self.assertEqual(bars[0].open, float(rows[0][1]))
        self.assertEqual(bars[0].close, float(rows[0][4]))
        self.assertEqual(bars[0].volume, float(rows[0][6]))

    def test_ticker(self):
        raw = fixture("kraken_ticker.json")
        quote = KrakenFeed(fetch=lambda url, params=None: raw, clock=lambda: 123.0).quote()
        self.assertEqual(quote.price, float(raw["result"]["XXBTZUSD"]["c"][0]))
        self.assertEqual(quote.time, 123.0)


class FeedTests(unittest.TestCase):
    def test_closed_bars_drops_forming_candle(self):
        bars = [Bar(0, 60, 1, 1, 1, 1, 1), Bar(60, 60, 1, 1, 1, 1, 1)]
        self.assertEqual(len(closed_bars(bars, 100)), 1)
        self.assertEqual(len(closed_bars(bars, 131)), 2)

    def test_unknown_source(self):
        with self.assertRaises(ValueError):
            make_feed("tradingview", "BTC-USD")


if __name__ == "__main__":
    unittest.main()
