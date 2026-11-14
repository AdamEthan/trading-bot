"""Paper broker: fills every order at the live exchange price, plus slippage and fees.

No money moves. The numbers are a simulation of what would have happened if
the order had executed at the quoted price, worsened by `slippage_bps`.
"""
from __future__ import annotations

from ..data.models import Quote, utc_iso
from .base import Broker, Fill

QTY_DP = 8
USD_DP = 8


class PaperBroker(Broker):
    def __init__(self, starting_cash: float, fee_bps: float, slippage_bps: float):
        self.starting_cash = float(starting_cash)
        self.fee_bps = float(fee_bps)
        self.slippage_bps = float(slippage_bps)
        self._cash = float(starting_cash)
        self._qty = 0.0
        self.avg_price = 0.0
        self.realized = 0.0
        self.fees = 0.0
        self.fills: list[Fill] = []

    @property
    def position(self) -> float:
        return self._qty

    @property
    def cash(self) -> float:
        return self._cash

    def equity(self, mark: float) -> float:
        return self._cash + self._qty * mark

    def unrealized(self, mark: float) -> float:
        return (mark - self.avg_price) * self._qty if self._qty else 0.0

    def submit(self, order_id: str, side: str, qty: float, quote: Quote, reason: str, now: float) -> Fill:
        if side not in ("buy", "sell"):
            raise ValueError(f"bad side {side!r}")
        qty = round(qty, QTY_DP)
        if qty <= 0:
            raise ValueError("quantity must be positive")
        sign = 1 if side == "buy" else -1
        price = round(quote.price * (1 + sign * self.slippage_bps / 10_000), 2)
        fee = round(qty * price * self.fee_bps / 10_000, USD_DP)

        signed = sign * qty
        realized = 0.0
        if self._qty and (self._qty > 0) != (signed > 0):
            closing = min(abs(signed), abs(self._qty))
            realized = (price - self.avg_price) * closing * (1 if self._qty > 0 else -1)
        new_qty = round(self._qty + signed, QTY_DP)
        if new_qty == 0:
            new_avg = 0.0
        elif self._qty == 0 or (self._qty > 0) != (new_qty > 0):
            new_avg = price  # opened fresh, or flipped through zero
        elif (self._qty > 0) == (signed > 0):
            new_avg = (self.avg_price * abs(self._qty) + price * qty) / abs(new_qty)
        else:
            new_avg = self.avg_price  # partial close keeps the entry price

        self._cash = round(self._cash - signed * price - fee, USD_DP)
        self._qty, self.avg_price = new_qty, new_avg
        self.realized += realized
        self.fees += fee
        fill = Fill(
            fill_id=f"{order_id}-F1", order_id=order_id, time=utc_iso(now), side=side, qty=qty,
            market_price=quote.price, price=price, fee=fee, slippage_bps=self.slippage_bps,
            fee_bps=self.fee_bps, realized_pnl=round(realized, USD_DP), position_after=new_qty,
            avg_price_after=round(new_avg, 2), cash_after=self._cash, quote_time=utc_iso(quote.time),
            quote_source=quote.source, reason=reason,
        )
        self.fills.append(fill)
        return fill
