"""Enforce max position, max daily loss, max trades per day, and the kill switch.

Orders that only shrink the position are always allowed, so the bot can
always get flat. Everything that adds risk goes through `check`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: dict):
        self.max_position_usd = float(cfg["max_position_usd"])
        self.max_daily_loss_usd = float(cfg["max_daily_loss_usd"])
        self.max_trades = int(cfg["max_trades_per_day"])
        self.allow_short = bool(cfg["allow_short"])
        self.kill_switch = bool(cfg["kill_switch"])
        self.max_data_age = float(cfg.get("max_data_age_seconds", 60))
        self.halted = False
        self.halt_reason = ""

    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted, self.halt_reason = True, reason

    def loss_limit_hit(self, day_pnl: float) -> bool:
        return self.max_daily_loss_usd > 0 and day_pnl <= -self.max_daily_loss_usd

    def check(self, *, side: str, qty: float, price: float, position: float, trades_today: int,
              day_pnl: float, data_age: float, news: str | None = None) -> Decision:
        after = position + (qty if side == "buy" else -qty)
        reduces = abs(after) < abs(position) - 1e-12 and (after == 0 or (after > 0) == (position > 0))
        if reduces:
            return Decision(True, "reduces position")
        if self.halted:
            return Decision(False, f"trading halted: {self.halt_reason}")
        if trades_today >= self.max_trades:
            return Decision(False, f"max trades per day reached ({self.max_trades})")
        if self.loss_limit_hit(day_pnl):
            return Decision(False, f"daily loss limit reached (${self.max_daily_loss_usd:,.2f})")
        if after < 0 and not self.allow_short:
            return Decision(False, "short selling is disabled")
        if abs(after) * price > self.max_position_usd * 1.0001:
            return Decision(False, f"position would be ${abs(after) * price:,.2f}, over the ${self.max_position_usd:,.2f} limit")
        if data_age > self.max_data_age:
            return Decision(False, f"price is {data_age:.0f}s old (limit {self.max_data_age:.0f}s)")
        if news:
            return Decision(False, f"news blackout: {news}")
        return Decision(True, "ok")
