"""Shared broker interface for connect, stream, place, and cancel."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

from ..data.models import Quote, utc_iso

FILL_FIELDS = [
    "fill_id", "order_id", "time", "side", "qty", "market_price", "price", "fee",
    "slippage_bps", "fee_bps", "realized_pnl", "position_after", "avg_price_after", "cash_after",
    "quote_time", "quote_source", "reason",
]


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    time: str            # when the bot filled the order (UTC)
    side: str            # "buy" | "sell"
    qty: float           # BTC, always positive
    market_price: float  # the exchange price the fill was based on
    price: float         # market_price adjusted for slippage
    fee: float           # USD
    slippage_bps: float
    fee_bps: float
    realized_pnl: float  # USD from the closed part, before fees
    position_after: float
    avg_price_after: float
    cash_after: float
    quote_time: str      # exchange timestamp of market_price (UTC)
    quote_source: str
    reason: str

    def row(self) -> dict:
        return asdict(self)


class Broker(ABC):
    @property
    @abstractmethod
    def position(self) -> float: ...

    @property
    @abstractmethod
    def cash(self) -> float: ...

    @abstractmethod
    def submit(self, order_id: str, side: str, qty: float, quote: Quote, reason: str, now: float) -> Fill: ...


__all__ = ["Broker", "Fill", "FILL_FIELDS", "utc_iso"]
