import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import FakeClock, FakeFeed, NoNews, ROOT
from src import config as config_mod
from src.engine import KILL_FILE, Engine
from src.verify import verify


def make_cfg(**overrides):
    cfg, errors = config_mod.validate({
        "market": {"bar_seconds": 60, "poll_seconds": 10},
        "strategy": {"name": "sma_cross", "params": {"fast": 3, "slow": 8}},
        "account": {"fee_bps": 10, "slippage_bps": 2},
        **overrides,
    })
    assert not errors, errors
    return cfg


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.clock = FakeClock()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def engine(self, cfg, minutes=180, feed=None):
        feed = feed or FakeFeed(self.clock)
        return Engine(cfg, self.tmp / "run", feed=feed, news=NoNews(), clock=self.clock,
                      sleep=self.clock.sleep, minutes=minutes), feed

    def test_full_session_trades_logs_and_verifies(self):
        engine, feed = self.engine(make_cfg())
        summary = engine.run()
        run = self.tmp / "run"
        for name in ("run.json", "events.jsonl", "trades.csv", "bars.csv", "equity.csv", "summary.json", "status.json"):
            self.assertTrue((run / name).exists(), name)
        self.assertGreater(summary["fills"], 2)
        self.assertEqual(summary["final_position"], 0)          # flattened at the end
        self.assertLessEqual(sum(1 for f in engine.broker.fills if f.reason.startswith("entry")), 10)
        self.assertEqual(json.loads((run / "status.json").read_text())["state"], "finished")
        ok, report = verify(run, online=True, feed=feed)
        self.assertTrue(ok, report)

    def test_tampered_log_fails_verification(self):
        engine, feed = self.engine(make_cfg())
        engine.run()
        trades = self.tmp / "run" / "trades.csv"
        with trades.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        rows[1]["price"] = str(float(rows[1]["price"]) - 50)
        with trades.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        ok, report = verify(self.tmp / "run", online=False)
        self.assertFalse(ok)
        self.assertIn("[FAIL]", report)

    def test_fake_market_price_fails_online_check(self):
        engine, feed = self.engine(make_cfg())
        engine.run()
        other = FakeFeed(self.clock, price_fn=lambda t: 10_000.0)
        ok, report = verify(self.tmp / "run", online=True, feed=other)
        self.assertFalse(ok)
        self.assertIn("outside", report)

    def test_small_candle_revision_warns_large_fails(self):
        engine, feed = self.engine(make_cfg())
        engine.run()
        bars = self.tmp / "run" / "bars.csv"
        original = bars.read_text()
        with bars.open(newline="") as fh:
            rows = list(csv.DictReader(fh))

        def rewrite(index, key, factor):
            changed = [dict(r) for r in rows]
            changed[index][key] = str(round(float(changed[index][key]) * factor, 2))
            with bars.open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=rows[0].keys())
                w.writeheader()
                w.writerows(changed)

        rewrite(50, "low", 0.999)        # 0.1%: looks like an exchange revision
        ok, report = verify(self.tmp / "run", online=True, feed=feed)
        self.assertTrue(ok, report)
        self.assertIn("[WARN]", report)
        rewrite(50, "close", 0.97)       # 3%: not a revision
        ok, report = verify(self.tmp / "run", online=True, feed=feed)
        self.assertFalse(ok)
        bars.write_text(original)

    def test_max_trades_per_day(self):
        engine, _ = self.engine(make_cfg(risk={"max_trades_per_day": 2}))
        engine.run()
        entries = [f for f in engine.broker.fills if f.reason.startswith("entry")]
        self.assertEqual(len(entries), 2)
        refused = [json.loads(l) for l in (self.tmp / "run" / "events.jsonl").read_text().splitlines()
                   if '"order_refused"' in l]
        self.assertTrue(any("max trades" in e["message"] for e in refused))

    def test_daily_loss_kill_switch(self):
        # Price only falls, strategy example goes long anyway -> losses -> halt.
        falling = lambda t: round(60_000 - (t - 1_794_600_000) * 0.5, 2)
        cfg = make_cfg(strategy={"name": "example", "params": {"hold_bars": 30}},
                       risk={"max_daily_loss_usd": 5, "max_position_usd": 1000})
        engine, _ = self.engine(cfg, feed=FakeFeed(self.clock, price_fn=falling))
        summary = engine.run()
        self.assertTrue(summary["halted"])
        self.assertIn("daily loss", summary["halt_reason"])
        self.assertEqual(summary["final_position"], 0)

    def test_kill_file_stops_run(self):
        engine, _ = self.engine(make_cfg(strategy={"name": "example", "params": {"hold_bars": 1000}}))
        original = engine.tick
        calls = {"n": 0}

        def tick():
            calls["n"] += 1
            if calls["n"] == 20:
                (self.tmp / "run" / KILL_FILE).write_text("x")
            original()

        engine.tick = tick
        summary = engine.run()
        self.assertTrue(summary["halted"])
        self.assertLess(calls["n"], 25)
        self.assertEqual(summary["final_position"], 0)

    def test_stop_loss_latches(self):
        falling = lambda t: round(60_000 - (t - 1_794_600_000) * 0.5, 2)
        cfg = make_cfg(strategy={"name": "example", "params": {"hold_bars": 1000}},
                       exits={"stop_loss_pct": 0.1}, risk={"max_daily_loss_usd": 0})
        engine, _ = self.engine(cfg, minutes=60, feed=FakeFeed(self.clock, price_fn=falling))
        engine.run()
        entries = [f for f in engine.broker.fills if f.reason.startswith("entry")]
        self.assertEqual(len(entries), 1)  # stopped out once, did not re-buy into the same signal

    def test_strategy_bug_skips_bars_then_halts(self):
        tmp = self.tmp / "broken.py"
        tmp.write_text(
            "from src.strategy.interface import Strategy\n"
            "class Broken(Strategy):\n"
            "    name = 'broken'\n"
            "    def on_bar(self, bars, position):\n"
            "        raise ZeroDivisionError('oops')\n", encoding="utf-8")
        engine, _ = self.engine(make_cfg(strategy={"name": "custom", "file": str(tmp)}), minutes=30)
        summary = engine.run()
        self.assertTrue(summary["halted"])
        self.assertIn("strategy failed", summary["halt_reason"])
        events = (self.tmp / "run" / "events.jsonl").read_text()
        self.assertIn("ZeroDivisionError", events)

    def test_feed_garbage_is_a_data_error(self):
        clock = self.clock

        class Garbage(FakeFeed):
            def candles(self, seconds, start=None, end=None):
                raise KeyError("result")

        engine, _ = self.engine(make_cfg(), minutes=10, feed=Garbage(clock))
        summary = engine.run()
        self.assertEqual(summary["fills"], 0)
        self.assertIn("result", engine.last_error)

    def test_data_outage_does_not_trade(self):
        engine, _ = self.engine(make_cfg(), minutes=30, feed=FakeFeed(self.clock, fail_quotes=True))
        summary = engine.run()
        self.assertEqual(summary["fills"], 0)
        self.assertGreater(engine.errors_in_row, 0)


class SessionTests(unittest.TestCase):
    def test_overnight_session(self):
        from src.engine import session_window
        start, end = session_window({"start": "22:00", "end": "06:00", "timezone": "UTC"}, 1_794_600_000)
        self.assertAlmostEqual(end - start, 8 * 3600)


if __name__ == "__main__":
    unittest.main()
