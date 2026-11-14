"""Textbook RSI mean reversion. A public example, not a private rule."""
from __future__ import annotations

from .indicators import rsi
from .interface import Param, PositionView, Signal, Strategy, Target


class RsiReversion(Strategy):
    name = "rsi_reversion"
    description = "Buy when RSI is oversold, sell short when overbought, exit when RSI returns to the middle."
    params = {
        "period": Param("int", 14, "RSI period", min=2),
        "oversold": Param("float", 30.0, "Oversold level", "Go long below this.", min=0, max=100),
        "overbought": Param("float", 70.0, "Overbought level", "Go short above this.", min=0, max=100),
        "exit_level": Param("float", 50.0, "Exit level", "Close the trade when RSI crosses back over this.", min=0, max=100),
    }

    def validate(self) -> None:
        if not self.p["oversold"] < self.p["exit_level"] < self.p["overbought"]:
            raise ValueError("rsi_reversion: need oversold < exit level < overbought")

    def warmup_bars(self) -> int:
        return self.p["period"] + 1

    def on_bar(self, bars, position: PositionView) -> Signal:
        value = rsi([b.close for b in bars], self.p["period"])
        if value is None:
            return Signal.flat("warming up")
        label = f"RSI {value:.1f}"
        if value < self.p["oversold"]:
            return Signal.long(f"{label} below {self.p['oversold']:g}")
        if value > self.p["overbought"]:
            return Signal.short(f"{label} above {self.p['overbought']:g}")
        if position.side == Target.LONG and value >= self.p["exit_level"]:
            return Signal.flat(f"{label} back above {self.p['exit_level']:g}")
        if position.side == Target.SHORT and value <= self.p["exit_level"]:
            return Signal.flat(f"{label} back below {self.p['exit_level']:g}")
        return Signal(position.side, f"{label}, holding")
