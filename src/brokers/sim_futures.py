"""Simulated futures broker for backtests: whole contracts, ticks, per-contract commission.

P&L is points x point value x contracts. Commission is charged per contract
per side (an "all-in" rate: broker + exchange + clearing + NFA fees). Market
and stop orders fill `slippage_ticks` worse than the price they were based
on; limit (target) orders fill at their price.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

FUTURES_FILL_FIELDS = [
    "fill_id", "order_id", "time", "symbol", "side", "qty", "basis", "market_price", "price",
    "slippage_ticks", "commission", "realized_pnl", "position_after", "avg_price_after", "cash_after",
    "source_time", "decided_at", "reason",
]

# Contract specs: tick size and dollars per 1.00 point. Check the exchange's spec page for others.
SPECS = {
    "MNQ": (0.25, 2.0), "NQ": (0.25, 20.0),
    "MES": (0.25, 5.0), "ES": (0.25, 50.0),
    "M2K": (0.10, 5.0), "RTY": (0.10, 50.0),
    "MYM": (1.0, 0.5), "YM": (1.0, 5.0),
    "MGC": (0.10, 10.0), "GC": (0.10, 100.0),
    "MCL": (0.01, 100.0), "CL": (0.01, 1000.0),
}


@dataclass(frozen=True)
class Instrument:
    root: str
    tick_size: float
    point_value: float

    @property
    def tick_value(self) -> float:
        return self.tick_size * self.point_value

    def round_price(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 10)


def instrument_for(root: str, tick_size: float = 0, point_value: float = 0) -> Instrument:
    tick, value = SPECS.get(root, (0.0, 0.0))
    tick, value = tick_size or tick, point_value or value
    if not tick or not value:
        raise ValueError(f"No built-in contract spec for {root!r}; set instrument.tick_size and "
                         "instrument.point_value in the backtest config")
    return Instrument(root, float(tick), float(value))


@dataclass(frozen=True)
class FuturesFill:
    fill_id: str
    order_id: str
    time: str             # UTC time of the fill
    symbol: str           # contract, e.g. MNQZ6
    side: str             # buy | sell
    qty: int              # contracts, always positive
    basis: str            # which price of the source candle it used: open | close | stop | target
    market_price: float   # that price, before slippage
    price: float          # fill price after slippage
    slippage_ticks: float
    commission: float     # USD for this fill
    realized_pnl: float   # USD from the closed part, before commission
    position_after: int
    avg_price_after: float
    cash_after: float
    source_time: str      # start of the source candle the price came from (UTC)
    decided_at: str       # when the decision was made (bar close, or the stop/target candle)
    reason: str

    def row(self) -> dict:
        return asdict(self)


class SimFuturesBroker:
    def __init__(self, instrument: Instrument, starting_cash: float, commission_per_contract: float,
                 slippage_ticks: float):
        self.inst = instrument
        self.starting_cash = float(starting_cash)
        self.commission_per_contract = float(commission_per_contract)
        self.slippage_ticks = float(slippage_ticks)
        self.cash = float(starting_cash)  # starting cash + realized P&L - commissions
        self.position = 0
        self.avg_price = 0.0
        self.symbol = ""
        self.realized = 0.0
        self.commissions = 0.0
        self.fills: list[FuturesFill] = []

    def unrealized(self, mark: float) -> float:
        return (mark - self.avg_price) * self.position * self.inst.point_value if self.position else 0.0

    def equity(self, mark: float | None) -> float:
        return self.cash + (self.unrealized(mark) if mark is not None else 0.0)

    def fill_price(self, side: str, market_price: float, basis: str) -> float:
        if basis == "target":
            return market_price
        sign = 1 if side == "buy" else -1
        return self.inst.round_price(market_price + sign * self.slippage_ticks * self.inst.tick_size)

    def submit(self, *, order_id: str, symbol: str, side: str, qty: int, market_price: float, basis: str,
               time: str, source_time: str, decided_at: str, reason: str) -> FuturesFill:
        if side not in ("buy", "sell"):
            raise ValueError(f"bad side {side!r}")
        if qty <= 0 or int(qty) != qty:
            raise ValueError("quantity must be a positive whole number of contracts")
        if self.position and symbol != self.symbol:
            raise ValueError(f"holding {self.symbol}; cannot trade {symbol} before flattening")
        qty = int(qty)
        price = self.fill_price(side, market_price, basis)
        signed = qty if side == "buy" else -qty
        realized = 0.0
        if self.position and (self.position > 0) != (signed > 0):
            closing = min(qty, abs(self.position))
            realized = (price - self.avg_price) * closing * (1 if self.position > 0 else -1) * self.inst.point_value
        new_pos = self.position + signed
        if new_pos == 0:
            new_avg = 0.0
        elif self.position == 0 or (self.position > 0) != (new_pos > 0):
            new_avg = price
        elif (self.position > 0) == (signed > 0):
            new_avg = (self.avg_price * abs(self.position) + price * qty) / abs(new_pos)
        else:
            new_avg = self.avg_price
        commission = round(qty * self.commission_per_contract, 2)
        self.cash = round(self.cash + realized - commission, 2)
        self.realized = round(self.realized + realized, 2)
        self.commissions = round(self.commissions + commission, 2)
        self.position, self.avg_price, self.symbol = new_pos, new_avg, symbol
        fill = FuturesFill(
            fill_id=f"{order_id}-F1", order_id=order_id, time=time, symbol=symbol, side=side, qty=qty,
            basis=basis, market_price=market_price, price=price, slippage_ticks=self.slippage_ticks,
            commission=commission, realized_pnl=round(realized, 2), position_after=new_pos,
            avg_price_after=round(new_avg, 6), cash_after=self.cash, source_time=source_time,
            decided_at=decided_at, reason=reason,
        )
        self.fills.append(fill)
        return fill
