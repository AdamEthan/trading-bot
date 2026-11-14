"""Turn an approved signal into orders and track them.

The engine asks for a target (long / flat / short). This module works out the
orders needed to get there, runs each through the risk manager, and sends the
approved ones to the broker. Each order gets a unique id, and a target the
bot already holds produces no order, so repeated signals cannot double up.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..brokers.base import Broker, Fill
from ..data.models import Quote
from ..risk.limits import RiskManager
from ..strategy.interface import Target

QTY_EPS = 1e-8


@dataclass(frozen=True)
class OrderPlan:
    side: str
    qty: float
    purpose: str  # "exit" | "entry"


def side_of(qty: float) -> Target:
    if qty > QTY_EPS:
        return Target.LONG
    if qty < -QTY_EPS:
        return Target.SHORT
    return Target.FLAT


def plan_orders(position: float, target: Target, size_usd: float, price: float) -> list[OrderPlan]:
    current = side_of(position)
    if current == target:
        return []
    plans = []
    if current != Target.FLAT:
        plans.append(OrderPlan("sell" if position > 0 else "buy", abs(position), "exit"))
    if target != Target.FLAT:
        qty = round(size_usd / price, 8)
        plans.append(OrderPlan("buy" if target == Target.LONG else "sell", qty, "entry"))
    return plans


class OrderManager:
    def __init__(self, run_id: str, broker: Broker, risk: RiskManager, size_usd: float):
        self.run_id = run_id
        self.broker = broker
        self.risk = risk
        self.size_usd = size_usd
        self._seq = 0

    def next_order_id(self) -> str:
        self._seq += 1
        return f"{self.run_id}-{self._seq:04d}"

    def move_to(self, target: Target, quote: Quote, reason: str, *, now: float, trades_today: int,
                day_pnl: float, news: str | None = None) -> tuple[list[Fill], list[str]]:
        """Returns (fills, rejection messages)."""
        fills, rejected = [], []
        for plan in plan_orders(self.broker.position, target, self.size_usd, quote.price):
            decision = self.risk.check(
                side=plan.side, qty=plan.qty, price=quote.price, position=self.broker.position,
                trades_today=trades_today + sum(1 for f in fills if f.reason.startswith("entry")), day_pnl=day_pnl,
                data_age=max(0.0, now - quote.time), news=news,
            )
            if not decision.ok:
                rejected.append(f"{plan.purpose} {plan.side} {plan.qty:.8f} refused: {decision.reason}")
                break
            fills.append(self.broker.submit(self.next_order_id(), plan.side, plan.qty, quote,
                                            f"{plan.purpose}: {reason}", now))
        return fills, rejected
