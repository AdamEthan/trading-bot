"""Template for a private strategy. Copy it, do not edit it in place.

1. Copy this file to  src/strategy/private/my_strategy.py  (that folder is
   git-ignored), or anywhere outside the repo.
2. In the settings page pick "custom" and enter the file's path,
   or set  strategy.name: custom  and  strategy.file: <path>  in config/config.yaml.
3. Your parameters show up in the settings page automatically.

Rules the engine relies on:
- on_bar gets every closed bar so far (oldest first) and the current position.
- Return Signal.long / Signal.flat / Signal.short. The engine places orders only
  when the target changes, and the risk limits still apply.
- No network calls, no broker calls, no randomness you cannot reproduce.
"""
from __future__ import annotations

from src.strategy.indicators import sma
from src.strategy.interface import Param, PositionView, Signal, Strategy


class MyStrategy(Strategy):
    name = "my_strategy"
    description = "Describe your idea here."
    params = {
        "lookback": Param("int", 20, "Lookback bars", min=2),
        "threshold_pct": Param("float", 0.10, "Threshold %", "Distance from the average, in percent.", min=0),
    }

    def warmup_bars(self) -> int:
        return self.p["lookback"]

    def on_bar(self, bars, position: PositionView) -> Signal:
        closes = [b.close for b in bars]
        average = sma(closes, self.p["lookback"])
        if average is None:
            return Signal.flat("warming up")
        gap = (closes[-1] - average) / average * 100
        if gap > self.p["threshold_pct"]:
            return Signal.long(f"{gap:.2f}% above average")
        return Signal.flat(f"{gap:.2f}% from average")
