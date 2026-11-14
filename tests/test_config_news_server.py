import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.helpers import ROOT
from src import config as config_mod
from src.data.news import NewsCalendar, parse_calendar
from src.web.server import App, serve

FIX = Path(__file__).parent / "fixtures"


class ConfigTests(unittest.TestCase):
    def test_example_is_valid(self):
        import yaml
        _, errors = config_mod.validate(yaml.safe_load(config_mod.EXAMPLE_PATH.read_text()))
        self.assertEqual(errors, [])

    def test_bad_values_are_reported(self):
        _, errors = config_mod.validate({"market": {"source": "tradingview", "bar_seconds": 7},
                                         "session": {"end": "25:00"}, "mode": "live"})
        text = " ".join(errors)
        for needle in ("market.source", "bar_seconds", "session.end", "mode"):
            self.assertIn(needle, text)

    def test_save_load_round_trip(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg, _ = config_mod.validate({"strategy": {"name": "rsi_reversion", "params": {"period": 7}}})
            config_mod.save(cfg, tmp / "c.yaml")
            loaded, errors = config_mod.load(tmp / "c.yaml")
            self.assertEqual(errors, [])
            self.assertEqual(loaded["strategy"]["params"], {"period": 7})
        finally:
            shutil.rmtree(tmp)


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs a git checkout")
class PrivacyTests(unittest.TestCase):
    def test_private_paths_are_git_ignored(self):
        for rel in ("config/config.yaml", "logs/2026-01-01_000000/trades.csv", "data/ff.json",
                    "src/strategy/private/my_strategy.py", ".env", ".venv/bin/python"):
            result = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", rel])
            self.assertEqual(result.returncode, 0, f"{rel} is not git-ignored")

    def test_example_and_template_are_not_ignored(self):
        for rel in ("config/config.example.yaml", "src/strategy/private_template.py",
                    "src/data/feed.py", "src/data/coinbase.py", "src/engine.py", "src/web/static/app.js"):
            result = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", rel])
            self.assertEqual(result.returncode, 1, f"{rel} should be committed")


class NewsTests(unittest.TestCase):
    def test_blackout_window(self):
        rows = [{"title": "CPI m/m", "country": "USD", "date": "2026-11-12T08:30:00-05:00", "impact": "High"},
                {"title": "Minor", "country": "USD", "date": "2026-11-12T09:00:00-05:00", "impact": "Low"},
                {"title": "ECB", "country": "EUR", "date": "2026-11-12T10:00:00-05:00", "impact": "High"}]
        events = parse_calendar(rows)
        t = events[0].time
        cal = NewsCalendar({"enabled": True, "currencies": ["USD"], "impact": ["High"],
                            "minutes_before": 15, "minutes_after": 10}, Path(tempfile.gettempdir()),
                           fetch=lambda url: rows)
        cal.events = events
        self.assertIsNone(cal.blackout(t - 16 * 60))
        self.assertEqual(cal.blackout(t - 14 * 60).title, "CPI m/m")
        self.assertEqual(cal.blackout(t + 9 * 60).title, "CPI m/m")
        self.assertIsNone(cal.blackout(t + 11 * 60))
        self.assertIsNone(cal.blackout(events[2].time))  # EUR not selected

    def test_real_feed_shape_parses(self):
        rows = json.loads((FIX / "ff_calendar.json").read_text())
        events = parse_calendar(rows)
        self.assertEqual(len(events), len(rows))
        self.assertTrue(all(e.country == "USD" for e in events))

    def test_refresh_caches_and_survives_errors(self):
        from src.data.http import DataError
        tmp = Path(tempfile.mkdtemp())
        try:
            rows = json.loads((FIX / "ff_calendar.json").read_text())
            calls = []
            cal = NewsCalendar({"enabled": True}, tmp, fetch=lambda url: calls.append(1) or rows, clock=lambda: 1000.0)
            cal.refresh()
            cal.refresh()
            self.assertEqual(len(calls), 1)  # second call served from cache
            self.assertTrue((tmp / "ff_calendar_thisweek.json").exists())

            def boom(url):
                raise DataError("down")
            cal2 = NewsCalendar({"enabled": True}, tmp, fetch=boom, clock=lambda: 1000.0 + 7 * 3600)
            cal2.refresh()
            self.assertEqual(len(cal2.events), len(rows))  # fell back to the cached copy
            self.assertIn("down", cal2.error)
        finally:
            shutil.rmtree(tmp)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg_path = cls.tmp / "config.yaml"
        run = cls.tmp / "logs" / "2026-11-14_090000"
        run.mkdir(parents=True)
        (run / "run.json").write_text("{}")
        (run / "status.json").write_text(json.dumps({"state": "running", "run_id": run.name}))
        cls.run_dir = run
        cls.server, cls.port = serve(App(cls.tmp / "logs", cls.cfg_path, live_run=run), 18765)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp)

    def req(self, path, data=None, host=None, ctype="application/json"):
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if data is not None:
            headers["Content-Type"] = ctype
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers,
                                         data=None if data is None else json.dumps(data).encode())
        try:
            with urllib.request.urlopen(request, timeout=5) as r:
                return r.status, json.loads(r.read() or b"null") if "json" in r.headers["Content-Type"] else r.read()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_status_and_pages(self):
        status, body = self.req("/api/status")
        self.assertEqual((status, body["state"]), (200, "running"))
        self.assertEqual(self.req("/")[0], 200)
        self.assertEqual(self.req("/settings")[0], 200)
        self.assertEqual(self.req("/static/../server.py")[0], 404)

    def test_rejects_foreign_host(self):
        self.assertEqual(self.req("/api/status", host="evil.example:80")[0], 403)

    def test_rejects_non_json_post(self):
        self.assertEqual(self.req("/api/kill", data={}, ctype="text/plain")[0], 415)

    def test_unknown_run_name_is_not_a_path(self):
        self.assertEqual(self.req("/api/status?run=../../etc")[0], 404)

    def test_config_round_trip_and_validation(self):
        status, body = self.req("/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(self.cfg_path.exists())
        cfg = body["config"]
        cfg["strategy"] = {"name": "sma_cross", "params": {"fast": 50, "slow": 10}}
        status, body = self.req("/api/config", data={"config": cfg})
        self.assertEqual(status, 422)
        self.assertTrue(any("fast" in e for e in body["errors"]))
        cfg["strategy"]["params"] = {"fast": 5, "slow": 10}
        status, body = self.req("/api/config", data={"config": cfg})
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertIn("fast: 5", self.cfg_path.read_text())

    def test_kill(self):
        status, _ = self.req("/api/kill", data={})
        self.assertEqual(status, 200)
        self.assertTrue((self.run_dir / "KILL").exists())


if __name__ == "__main__":
    unittest.main()
