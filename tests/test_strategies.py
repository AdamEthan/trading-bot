import unittest

from tests.helpers import ROOT
from src.data.models import Bar
from src.strategy.indicators import ema, rsi, sma
from src.strategy.interface import PositionView, Target
from src.strategy.registry import BUILTINS, build_strategy, catalog, load_class_from_file


def bars(closes):
    return [Bar(i * 60, 60, c, c, c, c, 1) for i, c in enumerate(closes)]


class IndicatorTests(unittest.TestCase):
    def test_sma_ema(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(sma([1], 2))
        self.assertAlmostEqual(ema([1, 1, 1, 1], 3), 1)

    def test_rsi_extremes(self):
        self.assertEqual(rsi(list(range(30)), 14), 100.0)
        self.assertLess(rsi(list(range(30, 0, -1)), 14), 1)


class StrategyTests(unittest.TestCase):
    def test_sma_cross(self):
        s = build_strategy({"name": "sma_cross", "params": {"fast": 3, "slow": 5}}, ROOT)
        self.assertEqual(s.on_bar(bars(range(1, 20)), PositionView()).target, Target.LONG)
        self.assertEqual(s.on_bar(bars(range(20, 1, -1)), PositionView()).target, Target.SHORT)

    def test_sma_cross_rejects_bad_periods(self):
        with self.assertRaises(ValueError):
            build_strategy({"name": "sma_cross", "params": {"fast": 10, "slow": 5}}, ROOT)

    def test_unknown_param_rejected(self):
        with self.assertRaises(ValueError):
            build_strategy({"name": "sma_cross", "params": {"fats": 3}}, ROOT)

    def test_rsi_reversion(self):
        s = build_strategy({"name": "rsi_reversion", "params": {}}, ROOT)
        self.assertEqual(s.on_bar(bars(range(60, 30, -1)), PositionView()).target, Target.LONG)
        self.assertEqual(s.on_bar(bars(range(30, 60)), PositionView()).target, Target.SHORT)

    def test_template_loads_as_custom(self):
        path = ROOT / "src" / "strategy" / "private_template.py"
        cls = load_class_from_file(path)
        self.assertEqual(cls.name, "my_strategy")
        s = build_strategy({"name": "custom", "file": str(path), "params": {"lookback": 5}}, ROOT)
        self.assertEqual(s.on_bar(bars(range(1, 20)), PositionView()).target, Target.LONG)
        entry = [c for c in catalog({"name": "custom", "file": str(path)}, ROOT) if c["name"] == "custom"][0]
        self.assertIn("lookback", entry["params"])

    def test_catalog_lists_builtins(self):
        names = {c["name"] for c in catalog({}, ROOT)}
        self.assertTrue(set(BUILTINS) < names)


if __name__ == "__main__":
    unittest.main()
