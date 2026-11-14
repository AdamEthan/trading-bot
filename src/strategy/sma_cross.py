"""Textbook moving-average crossover. A public example, not a private rule."""
from __future__ import annotations

from .indicators import ema, sma
from .interface import Param, PositionView, Signal, Strategy


class SmaCross(Strategy):
    name = "sma_cross"
    description = "Long while the fast average is above the slow one; short (or flat) below."
    params = {
        "fast": Param("int", 9, "Fast period", "Bars in the fast average.", min=1),
        "slow": Param("int", 21, "Slow period", "Bars in the slow average.", min=2),
        "average": Param("choice", "sma", "Average type", "Simple or exponential.", choices=("sma", "ema")),
    }

    def validate(self) -> None:
        if self.p["fast"] >= self.p["slow"]:
            raise ValueError("sma_cross: fast period must be smaller than slow period")

    def warmup_bars(self) -> int:
        return self.p["slow"]

    def on_bar(self, bars, position: PositionView) -> Signal:
        closes = [b.close for b in bars]
        avg = ema if self.p["average"] == "ema" else sma
        fast, slow = avg(closes, self.p["fast"]), avg(closes, self.p["slow"])
        if fast is None or slow is None:
            return Signal.flat("warming up")
        detail = f"fast {fast:,.2f} vs slow {slow:,.2f}"
        if fast > slow:
            return Signal.long(f"fast above slow ({detail})")
        if fast < slow:
            return Signal.short(f"fast below slow ({detail})")
        return Signal(position.side, f"averages equal ({detail})")
