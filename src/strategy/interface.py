"""Strategy contract: market state in, signal out. No broker or network calls.

A strategy says which position it wants (long, flat, or short). The engine
works out the orders, and the risk manager can still refuse them. Returning
the same target twice never places a second order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from ..data.models import Bar


class Target(str, Enum):
    LONG = "long"
    FLAT = "flat"
    SHORT = "short"


@dataclass(frozen=True)
class Signal:
    target: Target
    reason: str = ""

    @classmethod
    def long(cls, reason: str = "") -> "Signal":
        return cls(Target.LONG, reason)

    @classmethod
    def flat(cls, reason: str = "") -> "Signal":
        return cls(Target.FLAT, reason)

    @classmethod
    def short(cls, reason: str = "") -> "Signal":
        return cls(Target.SHORT, reason)


@dataclass(frozen=True)
class PositionView:
    """What a strategy may know about the current position."""

    side: Target = Target.FLAT
    qty: float = 0.0
    entry_price: float | None = None
    bars_held: int = 0


@dataclass(frozen=True)
class Param:
    """One tunable value. The settings page builds its form from these."""

    kind: str  # "int" | "float" | "bool" | "choice"
    default: Any
    label: str = ""
    help: str = ""
    min: float | None = None
    max: float | None = None
    choices: tuple = field(default_factory=tuple)

    def coerce(self, name: str, value: Any) -> Any:
        try:
            if self.kind == "int":
                value = int(value)
            elif self.kind == "float":
                value = float(value)
            elif self.kind == "bool":
                value = value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}
            elif self.kind == "choice":
                value = str(value)
                if value not in self.choices:
                    raise ValueError(f"must be one of {', '.join(self.choices)}")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}: {exc}") from None
        if self.min is not None and value < self.min:
            raise ValueError(f"{name}: must be at least {self.min}")
        if self.max is not None and value > self.max:
            raise ValueError(f"{name}: must be at most {self.max}")
        return value

    def to_dict(self) -> dict:
        return {"kind": self.kind, "default": self.default, "label": self.label, "help": self.help,
                "min": self.min, "max": self.max, "choices": list(self.choices)}


class Strategy:
    """Subclass this. Declare `params`, then implement `on_bar`."""

    name = "base"
    description = ""
    params: dict[str, Param] = {}

    def __init__(self, **values: Any):
        unknown = set(values) - set(self.params)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameter(s) {', '.join(sorted(unknown))}")
        self.p = {key: spec.coerce(key, values.get(key, spec.default)) for key, spec in self.params.items()}
        self.validate()

    def validate(self) -> None:
        """Override to check relationships between parameters."""

    def warmup_bars(self) -> int:
        """Closed bars needed before the first signal."""
        return 1

    def on_bar(self, bars: Sequence[Bar], position: PositionView) -> Signal:
        raise NotImplementedError
