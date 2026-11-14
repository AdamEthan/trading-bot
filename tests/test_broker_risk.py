import unittest

from tests.helpers import ROOT  # noqa: F401
from src.brokers.paper import PaperBroker
from src.data.models import Quote
from src.execution.orders import plan_orders
from src.risk.limits import RiskManager
from src.strategy.interface import Target

RISK = {"max_position_usd": 1000, "max_daily_loss_usd": 100, "max_trades_per_day": 3,
        "allow_short": False, "kill_switch": True, "max_data_age_seconds": 60}


def q(price):
    return Quote(price, 0.0, "test")


class PaperBrokerTests(unittest.TestCase):
    def test_round_trip_with_fees_and_slippage(self):
        b = PaperBroker(10_000, fee_bps=10, slippage_bps=10)
        buy = b.submit("o1", "buy", 0.01, q(50_000), "entry", 0)
        self.assertEqual(buy.price, 50_050.0)                 # 10 bps worse
        self.assertAlmostEqual(buy.fee, 0.5005)               # 10 bps of 500.50
        sell = b.submit("o2", "sell", 0.01, q(51_000), "exit", 60)
        self.assertEqual(sell.price, 50_949.0)
        self.assertAlmostEqual(sell.realized_pnl, (50_949 - 50_050) * 0.01)
        self.assertEqual(b.position, 0)
        self.assertAlmostEqual(b.cash, 10_000 + 8.99 - 0.5005 - 0.50949, places=6)

    def test_short_and_average_price(self):
        b = PaperBroker(10_000, 0, 0)
        b.submit("o1", "sell", 0.02, q(100), "entry", 0)
        self.assertEqual(b.position, -0.02)
        b.submit("o2", "sell", 0.02, q(200), "add", 0)
        self.assertAlmostEqual(b.avg_price, 150)
        fill = b.submit("o3", "buy", 0.04, q(120), "exit", 0)
        self.assertAlmostEqual(fill.realized_pnl, (150 - 120) * 0.04)
        self.assertEqual(b.position, 0)
        self.assertAlmostEqual(b.equity(999), 10_000 + 1.2)


class PlanTests(unittest.TestCase):
    def test_same_target_places_nothing(self):
        self.assertEqual(plan_orders(0.02, Target.LONG, 1000, 50_000), [])
        self.assertEqual(plan_orders(0.0, Target.FLAT, 1000, 50_000), [])

    def test_flip_is_exit_then_entry(self):
        plans = plan_orders(0.02, Target.SHORT, 1000, 50_000)
        self.assertEqual([(p.side, p.purpose) for p in plans], [("sell", "exit"), ("sell", "entry")])
        self.assertAlmostEqual(plans[1].qty, 0.02)


class RiskTests(unittest.TestCase):
    def check(self, risk, **kw):
        args = dict(side="buy", qty=0.01, price=50_000, position=0.0, trades_today=0, day_pnl=0.0, data_age=1)
        args.update(kw)
        return risk.check(**args)

    def test_entry_ok(self):
        self.assertTrue(self.check(RiskManager(RISK)).ok)

    def test_exits_allowed_even_when_halted(self):
        risk = RiskManager(RISK)
        risk.halt("test")
        self.assertFalse(self.check(risk).ok)
        self.assertTrue(self.check(risk, side="sell", position=0.01, trades_today=99, day_pnl=-1e6, data_age=999).ok)

    def test_limits(self):
        risk = RiskManager(RISK)
        self.assertIn("max trades", self.check(risk, trades_today=3).reason)
        self.assertIn("daily loss", self.check(risk, day_pnl=-100).reason)
        self.assertIn("short", self.check(risk, side="sell").reason)
        self.assertIn("over the", self.check(risk, qty=0.03).reason)
        self.assertIn("old", self.check(risk, data_age=61).reason)
        self.assertIn("news", self.check(risk, news="USD CPI").reason)

    def test_zero_loss_limit_means_off(self):
        risk = RiskManager({**RISK, "max_daily_loss_usd": 0})
        self.assertTrue(self.check(risk, day_pnl=-1e6).ok)
        self.assertFalse(risk.loss_limit_hit(-1e6))


if __name__ == "__main__":
    unittest.main()
