"""Run a strategy over historical futures data, one source candle at a time.

The same Strategy classes the paper bot uses decide on each closed bar. The
replay works at the file's own resolution (one second for ohlcv-1s), so:

  * a decision made when a bar closes fills at the open of the next traded
    second, never at a price the strategy already saw;
  * stops and targets are checked second by second; if one second reaches
    both, the stop is assumed to come first;
  * market and stop fills pay `slippage_ticks`; every fill pays commission.

Each run writes to logs/backtests/<run id>/ (git-ignored):

  run.json        config, data file hash, contract per trade date, code commit
  trades.csv      every fill, with the source candle and price it used
  round_trips.csv one row per entry and its exit
  daily.csv       one row per session
  equity.csv      account value at every bar close
  events.jsonl    sessions, rolls, refusals, stops, halts
  summary.json    totals and statistics
  report.html     the same, as a page with an equity chart

`python run.py verify logs/backtests/<run id>` rechecks it against the data.
"""
from __future__ import annotations

import csv
import json
import math
import platform
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..brokers.sim_futures import FUTURES_FILL_FIELDS, FuturesFill, SimFuturesBroker, instrument_for
from ..data import databento
from ..data.models import Bar, utc_iso
from ..risk.limits import RiskManager
from ..strategy.interface import PositionView, Signal, Strategy, Target
from ..strategy.registry import build_strategy, file_fingerprint
from ..utils.logging import write_json_atomic

HISTORY_LIMIT = 2000
LATE_FILL_SECONDS = 60  # at session end, use the last price if the next one is further away than this

ROUND_TRIP_FIELDS = ["trade", "symbol", "side", "qty", "entry_time", "entry_price", "exit_time", "exit_price",
                     "points", "gross_pnl", "commission", "net_pnl", "bars_held", "exit_reason", "session"]
# net_pnl is the equity change since the previous session ended, so positions held
# overnight count on the day they are closed; gross_pnl and commission are for
# round trips that started in that session.
DAILY_FIELDS = ["session", "symbol", "trades", "wins", "losses", "gross_pnl", "commission", "net_pnl",
                "equity_end", "halted"]
EQUITY_FIELDS = ["time", "symbol", "close", "position", "equity", "drawdown"]


def side_of(qty: int) -> Target:
    return Target.LONG if qty > 0 else Target.SHORT if qty < 0 else Target.FLAT


@dataclass
class Session:
    day: date
    start: int
    end: int
    start_equity: float
    entries: int = 0
    risk: RiskManager | None = None
    symbol: str = ""
    last_refusal: str = ""


@dataclass
class OpenTrade:
    side: str
    qty: int
    entry_time: str
    entry_price: float
    entry_commission: float
    entry_bar: int
    session: str


@dataclass
class Pending:
    target: Target
    reason: str
    decided_at: int


@dataclass
class Result:
    run_dir: Path
    summary: dict
    fills: list[FuturesFill] = field(default_factory=list)


class SessionClock:
    """Trading windows (start/end Unix times) in a time zone, including overnight ones."""

    def __init__(self, tz: str, start: str, end: str):
        self.tz = ZoneInfo(tz)
        self.start = tuple(map(int, start.split(":")))
        self.end = tuple(map(int, end.split(":")))
        self.overnight = self.end <= self.start

    def window(self, d: date) -> tuple[int, int]:
        s = datetime(d.year, d.month, d.day, *self.start, tzinfo=self.tz)
        e_day = d + timedelta(days=1) if self.overnight else d
        e = datetime(e_day.year, e_day.month, e_day.day, *self.end, tzinfo=self.tz)
        return int(s.timestamp()), int(e.timestamp())

    def next_window(self, ts: int) -> tuple[date, int, int]:
        """The first window that has not ended by `ts`."""
        d = datetime.fromtimestamp(ts, self.tz).date() - timedelta(days=1)
        while True:
            s, e = self.window(d)
            if e > ts:
                return d, s, e
            d += timedelta(days=1)


class Backtest:
    def __init__(self, cfg: dict, data_path: Path, run_dir: Path, *, root_dir: Path, quiet: bool = False):
        self.cfg = cfg
        self.data_path = Path(data_path)
        self.run_dir = Path(run_dir)
        self.run_id = self.run_dir.name
        self.root_dir = root_dir
        self.quiet = quiet
        d = cfg["data"]
        self.root = d["root"]
        self.inst = instrument_for(self.root, cfg["instrument"]["tick_size"], cfg["instrument"]["point_value"])
        self.trade_from = date.fromisoformat(d["start"]) if d["start"] else None
        self.trade_to = date.fromisoformat(d["end"]) if d["end"] else None
        self.bar_seconds = int(cfg["bar_seconds"])
        self.src_seconds = databento.source_seconds(self.data_path)
        if self.bar_seconds % self.src_seconds:
            raise ValueError(f"bar_seconds {self.bar_seconds} is not a multiple of the file's "
                             f"{self.src_seconds}-second candles")
        self.strategy: Strategy = build_strategy(cfg["strategy"], root_dir)
        a = cfg["account"]
        self.contracts = int(a["contracts"])
        self.broker = SimFuturesBroker(self.inst, a["starting_cash"], a["commission_per_contract"], a["slippage_ticks"])
        self.clock = SessionClock(cfg["session"]["timezone"], cfg["session"]["start"], cfg["session"]["end"])
        ex = cfg["exits"]
        self.stop_pts, self.take_pts = ex["stop_loss_points"], ex["take_profit_points"]
        self.target_through = ex["target_fill"] == "through"

        self.history: list[Bar] = []
        self.bar: list | None = None  # [start, open, high, low, close, volume] being built
        self.symbol = ""
        self.last_row: databento.Row | None = None
        self.pending: Pending | None = None
        self.latch: Target | None = None
        self.session: Session | None = None
        self.window: tuple[date, int, int] | None = None
        self.open_trade: OpenTrade | None = None
        self.order_seq = 0
        self.peak = self.broker.starting_cash
        self.max_dd = 0.0
        self.max_dd_time = ""
        self.round_trips: list[dict] = []
        self.daily: list[dict] = []
        self.last_day_equity = self.broker.starting_cash  # daily net = change since the previous session ended
        self.counts = {"signals": 0, "refused": 0, "stops": 0, "targets": 0, "rolls": 0, "halts": 0}
        self._files: dict[str, tuple] = {}

    # ---------- output ----------
    def _open_outputs(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        for name, fields in (("trades.csv", FUTURES_FILL_FIELDS), ("round_trips.csv", ROUND_TRIP_FIELDS),
                             ("daily.csv", DAILY_FIELDS), ("equity.csv", EQUITY_FIELDS)):
            fh = (self.run_dir / name).open("w", newline="", encoding="utf-8")
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            self._files[name] = (fh, writer)
        self._events = (self.run_dir / "events.jsonl").open("w", encoding="utf-8")

    def _close_outputs(self) -> None:
        for fh, _ in self._files.values():
            fh.close()
        self._events.close()

    def _row(self, name: str, row: dict) -> None:
        self._files[name][1].writerow(row)

    def event(self, kind: str, ts: int, message: str, **data) -> None:
        self._events.write(json.dumps({"time": utc_iso(ts), "kind": kind, "message": message, **data},
                                      default=str) + "\n")

    def say(self, text: str) -> None:
        if not self.quiet:
            print(text, flush=True)

    # ---------- helpers ----------
    def mark(self) -> float | None:
        return self.last_row.close if self.last_row else None

    def in_trading_dates(self, d: date) -> bool:
        return (self.trade_from is None or d >= self.trade_from) and (self.trade_to is None or d <= self.trade_to)

    def position_view(self) -> PositionView:
        qty = self.broker.position
        held = 0
        if self.open_trade and self.history:
            held = max(0, (self.history[-1].start - self.open_trade.entry_bar) // self.bar_seconds)
        return PositionView(side_of(qty), float(qty), self.broker.avg_price or None, held)

    def day_pnl(self) -> float:
        return self.broker.equity(self.mark()) - self.session.start_equity if self.session else 0.0

    # ---------- orders ----------
    def fill(self, side: str, qty: int, market_price: float, basis: str, *, at: int, source: int,
             decided: int, reason: str) -> FuturesFill:
        self.order_seq += 1
        f = self.broker.submit(order_id=f"{self.run_id}-{self.order_seq:05d}", symbol=self.symbol, side=side,
                               qty=qty, market_price=market_price, basis=basis, time=utc_iso(at),
                               source_time=utc_iso(source), decided_at=utc_iso(decided), reason=reason)
        self._row("trades.csv", f.row())
        self.track_round_trip(f)
        return f

    def track_round_trip(self, f: FuturesFill) -> None:
        if self.open_trade is None:
            self.open_trade = OpenTrade("long" if f.side == "buy" else "short", f.qty, f.time, f.price,
                                        f.commission, self.history[-1].start if self.history else 0,
                                        self.session.day.isoformat() if self.session else "")
            return
        t = self.open_trade
        sign = 1 if t.side == "long" else -1
        points = (f.price - t.entry_price) * sign
        gross = f.realized_pnl
        commission = round(t.entry_commission + f.commission, 2)
        held = ((self.history[-1].start - t.entry_bar) // self.bar_seconds) if self.history else 0
        rt = {"trade": len(self.round_trips) + 1, "symbol": f.symbol, "side": t.side, "qty": t.qty,
              "entry_time": t.entry_time, "entry_price": t.entry_price, "exit_time": f.time,
              "exit_price": f.price, "points": round(points, 6), "gross_pnl": gross,
              "commission": commission, "net_pnl": round(gross - commission, 2), "bars_held": max(0, held),
              "exit_reason": f.reason, "session": t.session}
        self.round_trips.append(rt)
        self._row("round_trips.csv", rt)
        self.open_trade = None

    def move_to(self, target: Target, reason: str, *, price: float, basis: str, at: int, source: int,
                decided: int) -> None:
        """Exit and/or enter at `price` to reach `target`, through the risk manager."""
        pos = self.broker.position
        current = side_of(pos)
        if current == target:
            return
        plans = []
        if current != Target.FLAT:
            plans.append(("sell" if pos > 0 else "buy", abs(pos), "exit"))
        if target != Target.FLAT:
            plans.append(("buy" if target == Target.LONG else "sell", self.contracts, "entry"))
        for side, qty, purpose in plans:
            if self.session is None:  # outside a session only exits happen, and exits are always allowed
                if purpose == "exit":
                    self.fill(side, qty, price, basis, at=at, source=source, decided=decided, reason=f"exit: {reason}")
                continue
            decision = self.session.risk.check(side=side, qty=qty, price=price, position=self.broker.position,
                                               trades_today=self.session.entries, day_pnl=self.day_pnl(),
                                               data_age=0.0)
            if not decision.ok:
                self.counts["refused"] += 1
                if decision.reason != self.session.last_refusal:  # log each new reason once per session
                    self.session.last_refusal = decision.reason
                    self.event("order_refused", at, f"{purpose} {side} {qty} refused: {decision.reason} "
                                                    "(repeats this session are counted, not logged)")
                break
            self.fill(side, qty, price, basis, at=at, source=source, decided=decided, reason=f"{purpose}: {reason}")
            if purpose == "entry":
                self.session.entries += 1

    def flatten(self, reason: str, *, price: float, basis: str, at: int, source: int, decided: int) -> None:
        if self.broker.position:
            self.move_to(Target.FLAT, reason, price=price, basis=basis, at=at, source=source, decided=decided)

    def flatten_late(self, row: databento.Row | None, cutoff: int, reason: str) -> None:
        """Close out at `cutoff`: at the next print if it comes soon after, else at the last price before."""
        if not self.broker.position:
            return
        last = self.last_row
        if row is not None and row.symbol == self.symbol and row.ts - cutoff <= LATE_FILL_SECONDS:
            self.flatten(reason, price=row.open, basis="open", at=row.ts, source=row.ts, decided=cutoff)
        elif last is not None:
            self.flatten(reason, price=last.close, basis="close", at=last.ts + self.src_seconds, source=last.ts,
                         decided=cutoff)

    # ---------- bars and signals ----------
    def close_bar(self) -> None:
        start, o, h, l, c, v = self.bar
        bar = Bar(start, self.bar_seconds, o, h, l, c, v)
        self.bar = None
        self.history.append(bar)
        if len(self.history) > 2 * HISTORY_LIMIT:
            self.history = self.history[-HISTORY_LIMIT:]
        equity = self.broker.equity(c)
        self.peak = max(self.peak, equity)
        dd = self.peak - equity
        if dd > self.max_dd:
            self.max_dd, self.max_dd_time = dd, utc_iso(bar.end)
        self._row("equity.csv", {"time": utc_iso(bar.end), "symbol": self.symbol, "close": c,
                                 "position": self.broker.position, "equity": round(equity, 2),
                                 "drawdown": round(dd, 2)})
        s = self.session
        if s is None or s.risk.halted or not (s.start <= bar.end < s.end):
            return
        if len(self.history) < self.strategy.warmup_bars():
            return
        try:
            signal = self.strategy.on_bar(self.history, self.position_view())
            if not isinstance(signal, Signal):
                raise TypeError(f"on_bar returned {signal!r}, expected a Signal")
        except Exception as exc:
            raise RuntimeError(f"Strategy error on the bar starting {utc_iso(bar.start)}: {exc!r}") from exc
        self.counts["signals"] += 1
        target = signal.target
        if target == Target.SHORT and not s.risk.allow_short:
            target = Target.FLAT
        if self.latch is not None:
            if target == self.latch:
                target = Target.FLAT  # stopped out: stay out until the strategy changes its mind
            else:
                self.latch = None
        if target != side_of(self.broker.position):
            self.pending = Pending(target, signal.reason, bar.end)

    # ---------- per source candle ----------
    def check_exits(self, r: databento.Row) -> None:
        pos = self.broker.position
        if not pos or not (self.stop_pts or self.take_pts):
            return
        avg, tick = self.broker.avg_price, self.inst.tick_size
        long = pos > 0
        stop = (avg - self.stop_pts if long else avg + self.stop_pts) if self.stop_pts else None
        take = (avg + self.take_pts if long else avg - self.take_pts) if self.take_pts else None
        if stop is not None:
            stop = self.inst.round_price(stop)
            hit = r.low <= stop if long else r.high >= stop
            if hit:
                price = min(stop, r.open) if long else max(stop, r.open)  # a gap fills at the open, worse
                self.counts["stops"] += 1
                self.latch = side_of(pos)
                self.event("stop_loss", r.ts, f"stop {stop} hit", stop=stop, low=r.low, high=r.high)
                self.flatten(f"stop loss {self.stop_pts:g} pts", price=price, basis="stop", at=r.ts, source=r.ts,
                             decided=r.ts)
                return
        if take is not None:
            take = self.inst.round_price(take)
            extra = tick if self.target_through else 0.0
            hit = r.high >= take + extra if long else r.low <= take - extra
            if hit:
                price = max(take, r.open) if long else min(take, r.open)  # a gap fills at the open, better
                self.counts["targets"] += 1
                self.latch = side_of(pos)
                self.event("take_profit", r.ts, f"target {take} reached", target=take, low=r.low, high=r.high)
                self.flatten(f"take profit {self.take_pts:g} pts", price=price, basis="target", at=r.ts,
                             source=r.ts, decided=r.ts)

    def check_loss_limit(self, r: databento.Row) -> None:
        s = self.session
        if s is None or s.risk.halted or not s.risk.kill_switch:
            return
        pnl = self.day_pnl()
        if s.risk.loss_limit_hit(pnl):
            s.risk.halt(f"session loss ${-pnl:,.2f} hit the ${s.risk.max_daily_loss_usd:,.2f} limit")
            self.counts["halts"] += 1
            self.pending = None
            self.event("halt", r.ts, s.risk.halt_reason)
            self.flatten("daily loss limit", price=r.close, basis="close", at=r.ts + self.src_seconds, source=r.ts,
                         decided=r.ts + self.src_seconds)

    def open_session(self, d: date, start: int, end: int) -> None:
        risk = RiskManager({"max_position_qty": self.cfg["risk"]["max_contracts"],
                            "max_daily_loss_usd": self.cfg["risk"]["max_daily_loss_usd"],
                            "max_trades_per_day": self.cfg["risk"]["max_trades_per_day"],
                            "allow_short": self.cfg["risk"]["allow_short"],
                            "kill_switch": self.cfg["risk"]["kill_switch"]})
        self.session = Session(d, start, end, self.broker.equity(self.mark()), risk=risk, symbol=self.symbol)
        self.latch = None
        self.event("session_start", start, f"session {d} on {self.symbol}", start=utc_iso(start), end=utc_iso(end))

    def close_session(self, row: databento.Row | None) -> None:
        s = self.session
        if self.cfg["session"]["flatten_at_end"] or s.risk.halted:
            self.flatten_late(row, s.end, "end of session")
        self.pending = None
        trips = [t for t in self.round_trips if t["session"] == s.day.isoformat()]
        gross = round(sum(t["gross_pnl"] for t in trips), 2)
        comm = round(sum(t["commission"] for t in trips), 2)
        equity = self.broker.equity(self.mark())
        day = {"session": s.day.isoformat(), "symbol": s.symbol, "trades": len(trips),
               "wins": sum(1 for t in trips if t["net_pnl"] > 0), "losses": sum(1 for t in trips if t["net_pnl"] <= 0),
               "gross_pnl": gross, "commission": comm, "net_pnl": round(equity - self.last_day_equity, 2),
               "equity_end": round(equity, 2), "halted": s.risk.halt_reason}
        self.daily.append(day)
        self.last_day_equity = equity
        self._row("daily.csv", day)
        self.event("session_end", s.end, f"session {s.day}: net ${day['net_pnl']:+,.2f}, {len(trips)} trades")
        self.say(f"  {s.day}  {s.symbol:7s} {len(trips):3d} trades  net ${day['net_pnl']:+10,.2f}  "
                 f"equity ${equity:,.2f}" + (f"  (halted: {s.risk.halt_reason})" if s.risk.halt_reason else ""))
        self.session = None

    def roll(self, row: databento.Row) -> None:
        """New contract: close the old one out, restart the indicators on the new prices."""
        old = self.symbol
        if self.broker.position:
            self.flatten_late(None, self.last_row.ts + self.src_seconds, f"roll {old} -> {row.symbol}")
        self.counts["rolls"] += 1 if old else 0
        if old:
            self.event("roll", row.ts, f"roll {old} -> {row.symbol}; indicators restart on the new contract")
            self.say(f"  roll {old} -> {row.symbol}")
        self.symbol = row.symbol
        self.history, self.bar, self.pending, self.last_row = [], None, None, None
        if self.session:
            self.session.symbol = row.symbol

    def on_row(self, r: databento.Row) -> None:
        if r.symbol != self.symbol:
            if self.bar is not None:
                self.close_bar()
            self.roll(r)
        # 1. session boundaries (first, so the bar that closes at the session start can trade)
        if self.session is not None and r.ts >= self.session.end:
            self.close_session(r)
        if self.session is None:
            if self.window is None or r.ts >= self.window[2]:
                self.window = self.clock.next_window(r.ts)
            d, start, end = self.window
            if start <= r.ts < end and self.in_trading_dates(d):
                self.open_session(d, start, end)
                self.window = None
        # 2. the bar in progress ended before this candle: close it and let the strategy decide
        bar_start = r.ts - r.ts % self.bar_seconds
        if self.bar is not None and bar_start != self.bar[0]:
            self.close_bar()
        # 3. a decision fills at this candle's open: the first price after the bar closed
        if self.pending is not None:
            p, self.pending = self.pending, None
            if self.session is not None:
                self.move_to(p.target, p.reason, price=r.open, basis="open", at=r.ts, source=r.ts,
                             decided=p.decided_at)
        self.last_row = r
        # 4. stops, targets and the loss limit inside this candle
        if self.session is not None:
            self.check_exits(r)
            self.check_loss_limit(r)
        # 5. add the candle to the bar being built
        if self.bar is None:
            self.bar = [bar_start, r.open, r.high, r.low, r.close, r.volume]
        else:
            b = self.bar
            b[2] = max(b[2], r.high)
            b[3] = min(b[3], r.low)
            b[4] = r.close
            b[5] += r.volume

    # ---------- main ----------
    def run(self) -> Result:
        t0 = time.time()
        self.say(f"Reading {self.data_path.name} ...")
        plan = databento.plan_contracts(self.data_path, self.root, self.cfg["data"]["contract"] or None)
        data_hash = databento.sha256_file(self.data_path)
        self._open_outputs()
        write_json_atomic(self.run_dir / "run.json", {
            "run_id": self.run_id, "mode": "backtest", "created": utc_iso(time.time()),
            "code_commit": _git_commit(self.root_dir), "python": platform.python_version(),
            "data_file": self.data_path.name, "data_sha256": data_hash, "source_seconds": self.src_seconds,
            "data_metadata": databento.read_metadata(self.data_path).get("query", {}),
            "instrument": {"root": self.inst.root, "tick_size": self.inst.tick_size,
                           "point_value": self.inst.point_value},
            "strategy_class": type(self.strategy).__name__, "strategy_params": self.strategy.p,
            "strategy_file_sha256": file_fingerprint(self.cfg["strategy"], self.root_dir),
            "contracts": plan.to_dict(), "config": self.cfg,
        })
        self.say(f"{plan.rows:,} rows, {utc_iso(plan.first_ts)} to {utc_iso(plan.last_ts)}. "
                 f"Strategy {self.strategy.name} {self.strategy.p}, {self.bar_seconds}s bars, "
                 f"session {self.cfg['session']['start']}-{self.cfg['session']['end']} {self.cfg['session']['timezone']}")
        wanted = set(plan.by_date.values())
        td = databento.TradeDates()
        try:
            for r in databento.iter_rows(self.data_path, wanted):
                if plan.symbol_for(td(r.ts)) == r.symbol:
                    self.on_row(r)
            if self.bar is not None:
                self.close_bar()
            if self.session is not None:
                self.close_session(None)
            if self.broker.position and self.last_row:
                self.flatten_late(None, self.last_row.ts + self.src_seconds, "end of data")
        finally:
            self._close_outputs()
        summary = self.summary(plan, time.time() - t0)
        write_json_atomic(self.run_dir / "summary.json", summary)
        from .report import write_report
        write_report(self.run_dir)
        return Result(self.run_dir, summary, self.broker.fills)

    def summary(self, plan, seconds: float) -> dict:
        trips = self.round_trips
        wins = [t["net_pnl"] for t in trips if t["net_pnl"] > 0]
        losses = [t["net_pnl"] for t in trips if t["net_pnl"] <= 0]
        days = [d["net_pnl"] for d in self.daily]
        mean = sum(days) / len(days) if days else 0.0
        sd = math.sqrt(sum((x - mean) ** 2 for x in days) / (len(days) - 1)) if len(days) > 1 else 0.0
        b = self.broker
        return {
            "run_id": self.run_id, "mode": "backtest", "root": self.root, "strategy": self.strategy.name,
            "strategy_params": self.strategy.p, "bar_seconds": self.bar_seconds,
            "data_from": utc_iso(plan.first_ts), "data_to": utc_iso(plan.last_ts),
            "first_session": self.daily[0]["session"] if self.daily else None,
            "last_session": self.daily[-1]["session"] if self.daily else None,
            "contracts_traded": sorted({t["symbol"] for t in trips}),
            "starting_cash": b.starting_cash, "final_equity": round(b.cash, 2),
            "net_pnl": round(b.cash - b.starting_cash, 2), "gross_pnl": b.realized, "commissions": b.commissions,
            "fills": len(b.fills), "round_trips": len(trips), "winners": len(wins), "losers": len(losses),
            "win_rate": round(len(wins) / len(trips), 4) if trips else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
            "expectancy_per_trade": round(sum(t["net_pnl"] for t in trips) / len(trips), 2) if trips else None,
            "avg_points_per_trade": round(sum(t["points"] for t in trips) / len(trips), 3) if trips else None,
            "largest_win": max(wins) if wins else None, "largest_loss": min(losses) if losses else None,
            "max_drawdown": round(self.max_dd, 2), "max_drawdown_at": self.max_dd_time,
            "sessions": len(days), "winning_sessions": sum(1 for x in days if x > 0),
            "best_session": max(days) if days else None, "worst_session": min(days) if days else None,
            "sharpe_daily_annualized": round(mean / sd * math.sqrt(252), 2) if sd else None,
            "final_position": b.position, "counts": self.counts, "seconds_taken": round(seconds, 1),
        }


def _git_commit(root: Path) -> str:
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def format_summary(s: dict) -> str:
    def money(x):
        return "-" if x is None else f"${x:+,.2f}"

    def num(x, fmt="{:,.2f}"):
        return "-" if x is None else fmt.format(x)

    lines = [
        f"Backtest {s['run_id']}: {s['strategy']} {s['strategy_params']} on {s['root']} "
        f"({', '.join(s['contracts_traded']) or 'no trades'}), {s['bar_seconds']}s bars",
        f"  Data             {s['data_from']} to {s['data_to']}; sessions traded {s['first_session'] or '-'} to "
        f"{s['last_session'] or '-'}",
        f"  Net P&L          {money(s['net_pnl'])}   (gross {money(s['gross_pnl'])}, commissions ${s['commissions']:,.2f})",
        f"  Final equity     ${s['final_equity']:,.2f} from ${s['starting_cash']:,.2f}",
        f"  Trades           {s['round_trips']} round trips, win rate {num(s['win_rate'], '{:.1%}')}, "
        f"profit factor {num(s['profit_factor'], '{:.2f}')}",
        f"  Per trade        expectancy {money(s['expectancy_per_trade'])}, avg win {money(s['avg_win'])}, "
        f"avg loss {money(s['avg_loss'])}, avg {num(s['avg_points_per_trade'], '{:+.2f}')} pts",
        f"  Max drawdown     ${s['max_drawdown']:,.2f} (at {s['max_drawdown_at'] or '-'})",
        f"  Sessions         {s['sessions']} ({s['winning_sessions']} up), best {money(s['best_session'])}, "
        f"worst {money(s['worst_session'])}, Sharpe (daily, annualized) {num(s['sharpe_daily_annualized'])}",
        f"  Events           {s['counts']}",
    ]
    return "\n".join(lines)
