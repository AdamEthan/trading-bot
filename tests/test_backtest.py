"""Futures backtester: Databento reader, contract roll, fills, exits, sessions, verify."""
from __future__ import annotations

import csv
import io
import json
import math
import shutil
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from tests.helpers import ROOT  # noqa: F401  (puts the repo on sys.path)

from src.backtest import config as bt_config
from src.backtest.runner import Backtest, SessionClock
from src.backtest.verify import verify_backtest
from src.brokers.sim_futures import SimFuturesBroker, instrument_for
from src.data import databento
from src.risk.limits import RiskManager

HEADER = "ts_event,rtype,publisher_id,instrument_id,open,high,low,close,volume,symbol\n"
DAY = 86_400


def utc(y, m, d, hh=0, mm=0, ss=0) -> int:
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc).timestamp())


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def wave(ts: int) -> float:
    """A slow sine plus a trend, on the 0.25 tick grid."""
    p = 20_000 + 40 * math.sin(2 * math.pi * ts / 1800) + (ts % DAY) / 600
    return round(p * 4) / 4


def make_rows(days: list[tuple[int, str, str]], price=wave, step: int = 2, hours=(13, 18)) -> list[str]:
    """days: (day start UTC, busy contract, quiet contract). Writes `step`-second candles."""
    out = []
    for day0, busy, quiet in days:
        for ts in range(day0 + hours[0] * 3600, day0 + hours[1] * 3600, step):
            o = price(ts)
            c = price(ts + step - 1)
            out.append(f"{iso(ts)},32,1,1,{o:.9f},{max(o, c) + 0.25:.9f},{min(o, c) - 0.25:.9f},{c:.9f},50,{busy}\n")
            if ts % 600 == 0:
                q = o + 300
                out.append(f"{iso(ts)},32,1,2,{q:.9f},{q:.9f},{q:.9f},{q:.9f},1,{quiet}\n")
                out.append(f"{iso(ts)},32,1,3,-300.000000000,-300.000000000,-300.000000000,-300.000000000,1,"
                           f"{busy}-{quiet}\n")
    return out


def rising(ts: int) -> float:
    """Up 3 points a minute from the first test day."""
    return round((20_000 + (ts - D1 - 14 * 3600) / 20) * 4) / 4


def base_cfg(**over) -> dict:
    raw = {
        "data": {"root": "MNQ"},
        "session": {"timezone": "UTC", "start": "14:00", "end": "16:00", "flatten_at_end": True},
        "account": {"starting_cash": 10000, "contracts": 1, "commission_per_contract": 0.5, "slippage_ticks": 1},
        "strategy": {"name": "example", "params": {"hold_bars": 5}},
        "exits": {"stop_loss_points": 0, "take_profit_points": 0, "target_fill": "through"},
        "risk": {"max_contracts": 1, "max_daily_loss_usd": 0, "max_trades_per_day": 50, "allow_short": True,
                 "kill_switch": True},
    }
    for key, value in over.items():
        raw[key] = {**raw.get(key, {}), **value} if isinstance(value, dict) else value
    cfg, errors = bt_config.validate(raw)
    assert not errors, errors
    return cfg


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_csv(self, rows: list[str], name="glbx-mdp3.ohlcv-1s.csv") -> Path:
        path = self.tmp / name
        path.write_text(HEADER + "".join(rows), encoding="utf-8")
        return path

    def run_bt(self, cfg: dict, data: Path, name="run"):
        cfg = {**cfg, "data": {**cfg["data"], "file": str(data)}}
        return Backtest(cfg, data, self.tmp / "logs" / name, root_dir=ROOT, quiet=True).run()

    def csv_rows(self, run_dir: Path, name: str) -> list[dict]:
        with (run_dir / name).open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))


D1, D2, D3 = utc(2026, 10, 14), utc(2026, 10, 15), utc(2026, 10, 16)


class DatabentoReaderTests(TempDirCase):
    def test_reads_csv_zip_and_integer_timestamps(self):
        rows = make_rows([(D1, "MNQZ6", "MNQH7")], hours=(14, 15))
        plain = self.write_csv(rows)
        zpath = self.tmp / "GLBX-1.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.write(plain, "glbx-mdp3-x.ohlcv-1s.csv")
            zf.writestr("metadata.json", json.dumps({"query": {"schema": "ohlcv-1s"}}))
        a = list(databento.iter_rows(plain))
        b = list(databento.iter_rows(zpath))
        self.assertEqual(a, b)
        self.assertEqual(a[0].ts, D1 + 14 * 3600)
        self.assertEqual(databento.source_seconds(zpath), 1)
        raw = self.write_csv([f"{D1 * 10**9 + 5 * 10**9},32,1,1,20000250000000,20000500000000,"
                              f"20000000000000,20000250000000,3,MNQZ6\n"], "raw.csv")
        r = next(databento.iter_rows(raw))
        self.assertEqual((r.ts, r.open, r.high, r.volume), (D1 + 5, 20000.25, 20000.5, 3))

    def test_zstd_file(self):
        try:
            import zstandard
        except ModuleNotFoundError:
            self.skipTest("zstandard not installed")
        rows = make_rows([(D1, "MNQZ6", "MNQH7")], hours=(14, 15))
        path = self.tmp / "x.ohlcv-1s.csv.zst"
        path.write_bytes(zstandard.ZstdCompressor().compress((HEADER + "".join(rows)).encode()))
        self.assertEqual(len(list(databento.iter_rows(path))), len(rows))

    def test_bad_files_explain_themselves(self):
        bad = self.tmp / "bad.csv"
        bad.write_text("a,b\n1,2\n", encoding="utf-8")
        with self.assertRaisesRegex(databento.DataFileError, "missing column"):
            list(databento.iter_rows(bad))
        with self.assertRaisesRegex(databento.DataFileError, "DBN"):
            databento.open_text(self._touch("x.dbn.zst"))
        with self.assertRaisesRegex(databento.DataFileError, "not found"):
            databento.open_text(self.tmp / "missing.csv")

    def _touch(self, name):
        p = self.tmp / name
        p.write_bytes(b"")
        return p

    def test_trade_date_rolls_at_5pm_chicago(self):
        self.assertEqual(databento.trade_date(utc(2026, 10, 15, 21, 59)), date(2026, 10, 15))  # 16:59 CT
        self.assertEqual(databento.trade_date(utc(2026, 10, 15, 22, 0)), date(2026, 10, 16))   # 17:00 CT
        self.assertEqual(databento.trade_date(utc(2026, 11, 13, 23, 0)), date(2026, 11, 14))   # after DST ends

    def test_front_month_uses_previous_day_volume(self):
        rows = make_rows([(D1, "MNQZ6", "MNQH7"), (D2, "MNQH7", "MNQZ6"), (D3, "MNQH7", "MNQZ6")], hours=(14, 15))
        plan = databento.plan_contracts(self.write_csv(rows), "MNQ")
        self.assertEqual(plan.by_date, {date(2026, 10, 14): "MNQZ6", date(2026, 10, 15): "MNQZ6",
                                        date(2026, 10, 16): "MNQH7"})
        self.assertIn("MNQZ6-MNQH7", plan.skipped_symbols)  # spreads are never traded
        forced = databento.plan_contracts(self.write_csv(rows), "MNQ", only="MNQH7")
        self.assertEqual(set(forced.by_date.values()), {"MNQH7"})
        with self.assertRaisesRegex(databento.DataFileError, "not in the file"):
            databento.plan_contracts(self.write_csv(rows), "MNQ", only="MNQM7")


class MultiFileTests(TempDirCase):
    """A year downloaded as monthly files must give the same backtest as one big file."""

    def setUp(self):
        super().setUp()
        self.days = [(D1, "MNQZ6", "MNQH7"), (D2, "MNQH7", "MNQZ6"), (D3, "MNQH7", "MNQZ6")]
        self.whole = self.write_csv(make_rows(self.days, price=rising), "whole.ohlcv-1s.csv")
        parts = self.tmp / "parts"
        parts.mkdir()
        self.parts = []
        for k, day in enumerate(self.days):  # deliberately named so name order != time order
            path = parts / f"{'cba'[k]}-part.ohlcv-1s.csv"
            path.write_text(HEADER + "".join(make_rows([day], price=rising)), encoding="utf-8")
            self.parts.append(path)
        self.cfg = base_cfg(session={"start": "14:00", "end": "17:00", "flatten_at_end": False},
                            strategy={"name": "sma_cross", "params": {"fast": 1, "slow": 2}})

    def trades(self, data, name):
        rows = self.csv_rows(self.run_bt(self.cfg, data, name).run_dir, "trades.csv")
        return [{k: v for k, v in r.items() if k not in ("fill_id", "order_id")} for r in rows]

    def test_files_folder_and_zip_match_one_file(self):
        expected = self.trades(self.whole, "whole")
        self.assertEqual(len({r["symbol"] for r in expected}), 2)  # the roll happens across files
        self.assertEqual(self.trades(list(reversed(self.parts)), "list"), expected)
        self.assertEqual(self.trades(self.tmp / "parts", "folder"), expected)
        zpath = self.tmp / "split-job.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            for part in self.parts:
                zf.write(part, part.name)
        self.assertEqual(self.trades(zpath, "zip"), expected)

    def test_verify_checks_every_file(self):
        res = self.run_bt(self.cfg, self.parts, "multi")
        run = json.loads((res.run_dir / "run.json").read_text())
        self.assertEqual(len(run["data_files"]), 3)
        ok, report = verify_backtest(res.run_dir, self.parts, root_dir=ROOT)
        self.assertTrue(ok, report)
        ok, report = verify_backtest(res.run_dir, self.parts[:2], replay=False, root_dir=ROOT)
        self.assertFalse(ok)
        self.assertIn("missing, or changed", report)

    def test_overlapping_files_are_refused(self):
        with self.assertRaisesRegex(databento.DataFileError, "overlap"):
            list(databento.iter_rows([self.whole, self.parts[1]]))
        with self.assertRaisesRegex(databento.DataFileError, "candle sizes"):
            other = self.tmp / "x.ohlcv-1m.csv"
            other.write_text(HEADER + "".join(make_rows([(D3 + DAY, "MNQH7", "MNQZ6")], step=60)), encoding="utf-8")
            databento.DataSet([self.parts[0], other]).source_seconds()


class BrokerAndRiskTests(unittest.TestCase):
    def test_futures_pnl_commission_and_slippage(self):
        b = SimFuturesBroker(instrument_for("MNQ"), 10_000, 0.5, 1)
        f1 = b.submit(order_id="1", symbol="MNQZ6", side="buy", qty=2, market_price=20_000.0, basis="open",
                      time="t", source_time="t", decided_at="t", reason="entry")
        self.assertEqual(f1.price, 20_000.25)
        f2 = b.submit(order_id="2", symbol="MNQZ6", side="sell", qty=2, market_price=20_010.0, basis="target",
                      time="t", source_time="t", decided_at="t", reason="exit")
        self.assertEqual(f2.price, 20_010.0)                       # limit: no slippage
        self.assertAlmostEqual(f2.realized_pnl, 9.75 * 2 * 2.0)    # points x contracts x $2
        self.assertAlmostEqual(b.cash, 10_000 + 39.0 - 2.0)        # 4 contract-sides of $0.50
        self.assertEqual(b.position, 0)

    def test_unknown_root_needs_a_spec(self):
        with self.assertRaisesRegex(ValueError, "tick_size"):
            instrument_for("ZZZ")
        self.assertEqual(instrument_for("ZZZ", 0.5, 10).tick_value, 5.0)

    def test_risk_caps_contracts(self):
        r = RiskManager({"max_position_qty": 2, "max_daily_loss_usd": 0, "max_trades_per_day": 5,
                         "allow_short": True, "kill_switch": True})
        ok = dict(price=20_000, trades_today=0, day_pnl=0, data_age=0)
        self.assertTrue(r.check(side="buy", qty=2, position=0, **ok).ok)
        self.assertFalse(r.check(side="buy", qty=1, position=2, **ok).ok)
        self.assertTrue(r.check(side="sell", qty=3, position=3, **ok).ok)  # exits always allowed


class SessionClockTests(unittest.TestCase):
    def test_day_and_overnight_windows(self):
        day = SessionClock("America/New_York", "09:30", "16:00")
        d, s, e = day.next_window(utc(2026, 10, 15, 12))
        self.assertEqual((d, s, e), (date(2026, 10, 15), utc(2026, 10, 15, 13, 30), utc(2026, 10, 15, 20)))
        night = SessionClock("America/New_York", "18:00", "09:00")
        d, s, e = night.next_window(utc(2026, 10, 16, 3))  # 23:00 New York on the 15th
        self.assertEqual((d, s, e), (date(2026, 10, 15), utc(2026, 10, 15, 22), utc(2026, 10, 16, 13)))


class BacktestTests(TempDirCase):
    def test_fills_use_the_next_print_after_the_bar_closes(self):
        data = self.write_csv(make_rows([(D1, "MNQZ6", "MNQH7")]))
        res = self.run_bt(base_cfg(), data)
        fills = self.csv_rows(res.run_dir, "trades.csv")
        self.assertGreater(len(fills), 4)
        rows = {r.ts: r for r in databento.iter_rows(data, {"MNQZ6"})}
        for f in fills:
            src = int(datetime.strptime(f["source_time"], "%Y-%m-%dT%H:%M:%SZ")
                      .replace(tzinfo=timezone.utc).timestamp())
            decided = int(datetime.strptime(f["decided_at"], "%Y-%m-%dT%H:%M:%SZ")
                          .replace(tzinfo=timezone.utc).timestamp())
            self.assertGreaterEqual(src, decided)
            self.assertEqual(float(f["market_price"]), rows[src].open)
            slip = 0.25 if f["side"] == "buy" else -0.25
            self.assertEqual(float(f["price"]), rows[src].open + slip)
        last = fills[-1]
        self.assertEqual(last["position_after"], "0")
        self.assertLessEqual(last["source_time"], "2026-10-14T16:00:00Z")
        self.assertEqual(res.summary["final_position"], 0)

    def test_bar_closing_at_the_session_start_can_trade(self):
        data = self.write_csv(make_rows([(D1, "MNQZ6", "MNQH7")], price=rising))
        res = self.run_bt(base_cfg(strategy={"name": "sma_cross", "params": {"fast": 1, "slow": 2}}), data)
        first = self.csv_rows(res.run_dir, "trades.csv")[0]
        self.assertEqual((first["decided_at"], first["source_time"], first["side"]),
                         ("2026-10-14T14:00:00Z", "2026-10-14T14:00:00Z", "buy"))

    def test_no_trades_outside_the_session_or_date_range(self):
        data = self.write_csv(make_rows([(D1, "MNQZ6", "MNQH7"), (D2, "MNQZ6", "MNQH7")]))
        res = self.run_bt(base_cfg(data={"start": "2026-10-15"}), data)
        for f in self.csv_rows(res.run_dir, "trades.csv"):
            self.assertTrue(f["time"].startswith("2026-10-15"))
            self.assertTrue("14:00:00Z" <= f["time"][11:] <= "16:01:00Z", f["time"])
        self.assertEqual([d["session"] for d in self.csv_rows(res.run_dir, "daily.csv")], ["2026-10-15"])

    def test_stop_loss_fills_at_the_stop_and_waits_for_a_new_signal(self):
        def falling(ts):
            return round((20_000 - (ts - D1 - 14 * 3600) / 20) * 4) / 4  # down 3 points a minute
        data = self.write_csv(make_rows([(D1, "MNQZ6", "MNQH7")], price=falling, step=1))
        cfg = base_cfg(strategy={"name": "example", "params": {"hold_bars": 1000}},
                       exits={"stop_loss_points": 10})
        res = self.run_bt(cfg, data)
        fills = self.csv_rows(res.run_dir, "trades.csv")
        stop = [f for f in fills if f["basis"] == "stop"]
        self.assertEqual(len(stop), 1)
        entry = float(fills[0]["price"])
        self.assertEqual(float(stop[0]["market_price"]), entry - 10)
        self.assertEqual(float(stop[0]["price"]), entry - 10.25)  # one tick of slippage
        self.assertEqual(len(fills), 2)  # still "long" afterwards, but the latch keeps it out

    def test_take_profit_needs_a_trade_through(self):
        data = self.write_csv(make_rows([(D1, "MNQZ6", "MNQH7")], price=rising, step=1))
        cfg = base_cfg(strategy={"name": "example", "params": {"hold_bars": 1000}},
                       exits={"take_profit_points": 10})
        fills = self.csv_rows(self.run_bt(cfg, data).run_dir, "trades.csv")
        target = [f for f in fills if f["basis"] == "target"]
        self.assertEqual(len(target), 1)
        self.assertEqual(float(target[0]["price"]), float(fills[0]["price"]) + 10)

    def test_loss_limit_halts_the_session(self):
        def falling(ts):
            return round((20_000 - (ts - D1 - 14 * 3600) / 10) * 4) / 4
        data = self.write_csv(make_rows([(D1, "MNQZ6", "MNQH7")], price=falling, step=1))
        cfg = base_cfg(strategy={"name": "example", "params": {"hold_bars": 1000}},
                       risk={"max_daily_loss_usd": 50})
        res = self.run_bt(cfg, data)
        day = self.csv_rows(res.run_dir, "daily.csv")[0]
        self.assertIn("hit the $50.00 limit", day["halted"])
        self.assertLess(float(day["net_pnl"]), -50)
        self.assertGreater(float(day["net_pnl"]), -60)
        self.assertEqual(res.summary["counts"]["halts"], 1)

    def test_max_trades_per_session(self):
        data = self.write_csv(make_rows([(D1, "MNQZ6", "MNQH7")]))
        res = self.run_bt(base_cfg(strategy={"name": "example", "params": {"hold_bars": 1}},
                                   risk={"max_trades_per_day": 3}), data)
        fills = self.csv_rows(res.run_dir, "trades.csv")
        self.assertEqual(sum(1 for f in fills if f["reason"].startswith("entry")), 3)
        refusals = [json.loads(line) for line in (res.run_dir / "events.jsonl").read_text().splitlines()
                    if '"order_refused"' in line]
        self.assertEqual(len(refusals), 1)  # logged once, counted every time
        self.assertGreater(res.summary["counts"]["refused"], 1)

    def test_roll_closes_the_old_contract_and_trades_the_new_one(self):
        rows = make_rows([(D1, "MNQZ6", "MNQH7"), (D2, "MNQH7", "MNQZ6"), (D3, "MNQH7", "MNQZ6")])
        data = self.write_csv(rows)
        data = self.write_csv(make_rows([(D1, "MNQZ6", "MNQH7"), (D2, "MNQH7", "MNQZ6"), (D3, "MNQH7", "MNQZ6")],
                                        price=rising))
        cfg = base_cfg(session={"start": "14:00", "end": "17:00", "flatten_at_end": False},
                       strategy={"name": "sma_cross", "params": {"fast": 1, "slow": 2}})
        res = self.run_bt(cfg, data)
        fills = self.csv_rows(res.run_dir, "trades.csv")
        self.assertEqual({f["symbol"] for f in fills}, {"MNQZ6", "MNQH7"})
        roll = [f for f in fills if "roll" in f["reason"]]
        self.assertEqual(len(roll), 1)
        self.assertEqual(roll[0]["symbol"], "MNQZ6")
        self.assertEqual(res.summary["counts"]["rolls"], 1)
        self.assertTrue(verify_backtest(res.run_dir, data, root_dir=ROOT)[0])


class VerifyTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.data = self.write_csv(make_rows([(D1, "MNQZ6", "MNQH7")]))
        self.res = self.run_bt(base_cfg(exits={"stop_loss_points": 5, "take_profit_points": 5}), self.data)

    def test_clean_run_passes(self):
        ok, report = verify_backtest(self.res.run_dir, self.data, root_dir=ROOT)
        self.assertTrue(ok, report)
        self.assertNotIn("FAIL", report)

    def _tamper(self, column, value, index=1):
        path = self.res.run_dir / "trades.csv"
        rows = self.csv_rows(self.res.run_dir, "trades.csv")
        rows[index][column] = value
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    def test_edited_price_is_caught(self):
        rows = self.csv_rows(self.res.run_dir, "trades.csv")
        self._tamper("market_price", str(float(rows[1]["market_price"]) + 5))
        ok, report = verify_backtest(self.res.run_dir, self.data, replay=False, root_dir=ROOT)
        self.assertFalse(ok)
        self.assertIn("[FAIL] Fill prices", report)
        self.assertIn("[FAIL] Every market price is in the data", report)

    def test_look_ahead_is_caught(self):
        rows = self.csv_rows(self.res.run_dir, "trades.csv")
        opened = next(i for i, r in enumerate(rows) if r["basis"] == "open")
        later = datetime.strptime(rows[opened]["source_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self._tamper("decided_at", datetime.fromtimestamp(later.timestamp() + 60, timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"), opened)
        ok, report = verify_backtest(self.res.run_dir, self.data, replay=False, root_dir=ROOT)
        self.assertIn("[FAIL] No look-ahead", report)

    def test_different_data_file_is_caught(self):
        other = self.write_csv(make_rows([(D1, "MNQZ6", "MNQH7")], step=3), "other.csv")
        ok, report = verify_backtest(self.res.run_dir, other, root_dir=ROOT)
        self.assertFalse(ok)
        self.assertIn("different SHA-256", report)


class ConfigTests(TempDirCase):
    def test_example_is_valid_and_overrides_merge(self):
        cfg, errors = bt_config.load(bt_config.EXAMPLE_PATH)
        self.assertEqual(errors, [])
        path = self.tmp / "bt.yaml"
        path.write_text("strategy: {name: sma_cross, params: {fast: 5, slow: 30}}\n", encoding="utf-8")
        cfg, errors = bt_config.load(path, {"strategy": {"params": {"slow": "40"}}})
        self.assertEqual((errors, cfg["strategy"]["params"]), ([], {"fast": 5, "slow": "40"}))
        cfg, _ = bt_config.load(path, {"strategy": {"name": "rsi_reversion"}})
        self.assertEqual(cfg["strategy"]["params"], {})  # sma_cross params do not leak into another strategy

    def test_bad_values_are_reported(self):
        _, errors = bt_config.validate({"session": {"start": "9:30", "timezone": "Mars/Base"},
                                        "data": {"start": "15/10/2026"}})
        text = "\n".join(errors)
        for part in ("session.start", "time zone", "data.start"):
            self.assertIn(part, text)
        _, errors = bt_config.validate({"account": {"contracts": 3}})
        self.assertIn("max_contracts", "\n".join(errors))

    def test_local_backtest_config_is_git_ignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("config/backtest.yaml", ignore)


class CliTests(TempDirCase):
    def test_backtest_command_end_to_end(self):
        import contextlib

        import run
        data = self.write_csv(make_rows([(D1, "MNQZ6", "MNQH7")]))
        cfg_path = self.tmp / "backtest.yaml"
        cfg_path.write_text("session: {timezone: UTC, start: '14:00', end: '16:00'}\n", encoding="utf-8")
        old_logs = run.LOGS
        run.LOGS = self.tmp / "logs"
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = run.main(["backtest", "--data", str(data), "--backtest-config", str(cfg_path),
                                 "--strategy", "example", "--params", "hold_bars=3", "--quiet"])
                self.assertEqual(code, 0, out.getvalue())
                run_dir = next((self.tmp / "logs" / "backtests").iterdir())
                self.assertTrue((run_dir / "report.html").exists())
                code = run.main(["verify", str(run_dir)])
        finally:
            run.LOGS = old_logs
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("All checks passed.", out.getvalue())
        self.assertIn("{'hold_bars': 3}", out.getvalue())


if __name__ == "__main__":
    unittest.main()
