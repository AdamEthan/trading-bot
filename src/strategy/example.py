"""Dummy strategy used to exercise the pipeline. Not a trading rule."""
from __future__ import annotations

from .interface import Param, PositionView, Signal, Strategy, Target


class ExampleStrategy(Strategy):
    name = "example"
    description = "Pipeline test only: alternates long and flat every N bars. Not a trading rule."
    params = {"hold_bars": Param("int", 5, "Bars per phase", "Switch between long and flat after this many bars.", min=1)}

    def on_bar(self, bars, position: PositionView) -> Signal:
        phase = (bars[-1].start // bars[-1].seconds // self.p["hold_bars"]) % 2
        if phase == 0:
            return Signal.long("example: long phase")
        return Signal.flat("example: flat phase")
